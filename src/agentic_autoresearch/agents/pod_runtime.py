"""Pod-side per-iter runtime.

Runs ON the RunPod pod (not on the user's Mac). Given a workflow JSON,
the prompts JSON, k seeds, identity + Brooke refs, and an output dir,
it:

  1. submits the workflow N times to ComfyUI on localhost:8188
     (one submission per (prompt × seed) pair). For 5 prompts × 3 seeds
     that's 15 submissions.
  2. polls ComfyUI's /history endpoint until all 15 finish
  3. collects the 15 PNGs into /tmp/iter_NNN/candidates/
  4. runs the eval/compose pipeline against them
     (arcface + dinov2 + clip_sim + anatomy + opus_rubric)
  5. writes scores.json + winner.webp + workflow.json into a
     shared "results dir" that the orchestrator polls
  6. uploads (via ssh / rsync / runpod proxy / direct write to a
     mounted dir) those files back to the Mac

This file is self-contained: it's shipped to the pod, executed via SSH,
and reads its dependencies from /runpod-volume/.

THIS IS A SKELETON. The submit-to-ComfyUI + collect-results steps need
the canonical ComfyUI REST shape and are environment-specific. Wiring
them is done at first-run time; the contracts and the eval-pipeline
invocation are correct.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path


COMFYUI_URL = "http://127.0.0.1:8188"


def render_template(template_text: str, params: dict) -> dict:
    """Workflows in workflows/*.json are TEMPLATES with {param} placeholders
    (e.g. {prompt}, {seed}, {cfg}, {id_weight}, {checkpoint}, ...). This
    fn substitutes the params and parses to JSON.

    Critically: it uses regex substitution rather than str.format() so
    that JSON braces (`{`, `}`) inside string values don't blow up.
    """
    import re
    out = template_text
    for k, v in params.items():
        # JSON-encode the value to handle strings, numbers, booleans
        # correctly. Strip outer quotes for numeric placeholders that
        # were written like {cfg} (no quotes) but keep them for string
        # placeholders that were written like "{prompt}" (in quotes).
        encoded = json.dumps(v)
        # Replace "{k}" (in quotes) first — the placeholder was meant
        # as a string literal.
        out = re.sub(r'"\{' + re.escape(k) + r'\}"', encoded, out)
        # Then bare {k} for numeric/raw placeholders.
        if isinstance(v, str):
            # for raw {k} of a string value, JSON-encode (adds quotes).
            out = re.sub(r'\{' + re.escape(k) + r'\}', encoded, out)
        else:
            out = re.sub(r'\{' + re.escape(k) + r'\}', encoded, out)
    return json.loads(out)


def submit_workflow_dict(nodes: dict) -> str:
    """Submit an already-rendered workflow node dict to ComfyUI."""
    payload = {"prompt": nodes, "client_id": str(uuid.uuid4())}
    req = urllib.request.Request(
        f"{COMFYUI_URL}/prompt",
        data=json.dumps(payload).encode(),
        headers={"content-type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        body = json.loads(r.read())
    return body["prompt_id"]


def poll_history(prompt_id: str) -> dict | None:
    """Returns history dict when complete; None otherwise."""
    try:
        req = urllib.request.Request(f"{COMFYUI_URL}/history/{prompt_id}")
        with urllib.request.urlopen(req, timeout=15) as r:
            payload = json.loads(r.read())
        return payload.get(prompt_id)
    except urllib.error.HTTPError:
        return None


def collect_image(history: dict, output_dir: Path, name: str) -> Path | None:
    """Pull the first image from a completed prompt's outputs into output_dir."""
    outputs = history.get("outputs", {})
    for node_id, node_out in outputs.items():
        for image in node_out.get("images", []):
            filename = image["filename"]
            subfolder = image.get("subfolder", "")
            type_ = image.get("type", "output")
            qs = f"?filename={filename}&subfolder={subfolder}&type={type_}"
            url = f"{COMFYUI_URL}/view{qs}"
            try:
                with urllib.request.urlopen(url, timeout=30) as r:
                    data = r.read()
            except urllib.error.HTTPError:
                continue
            dest = output_dir / f"{name}.png"
            dest.write_bytes(data)
            return dest
    return None


def run_batch(
    workflow_path: Path,
    prompts_path: Path,
    seeds: list[int],
    candidates_dir: Path,
    extra_params: dict | None = None,
    identity_image_filename: str = "identity.png",
) -> list[Path]:
    """Submit (prompt × seed) jobs and gather candidate PNGs.

    The workflow file is a TEMPLATE — render_template substitutes
    {prompt}, {seed}, {cfg}, {width}, {height}, {id_weight},
    {face_image}, etc. The actor supplies extra_params for any
    non-default knobs.
    """
    template_text = workflow_path.read_text()
    prompts_doc = json.loads(prompts_path.read_text())
    prompts = prompts_doc["prompts"]

    defaults = {
        "width": 1024,
        "height": 1024,
        "steps": 30,
        "cfg": 4.5,
        "id_weight": 0.85,
        "lora_strength": 0.7,
        "lora_name": "",
        "checkpoint": "realvisxlV50_v50LightningBakedvae.safetensors",
        "face_image": identity_image_filename,
        "negative_prompt": "",
    }
    if extra_params:
        defaults.update(extra_params)

    candidates_dir.mkdir(parents=True, exist_ok=True)
    submitted: list[tuple[str, str, int]] = []
    for p in prompts:
        for seed in seeds:
            name = f"{p['id']}_s{seed}"
            params = dict(defaults)
            params["prompt"] = p["text"]
            params["negative_prompt"] = p.get("negative", "")
            params["seed"] = seed
            try:
                nodes = render_template(template_text, params)
            except (json.JSONDecodeError, ValueError) as e:
                print(f"  {name}: render_template failed: {e}", file=sys.stderr)
                continue
            try:
                pid = submit_workflow_dict(nodes)
            except Exception as e:
                print(f"  {name}: submit failed: {e}", file=sys.stderr)
                continue
            submitted.append((pid, name, seed))
            print(f"  submitted {name} → prompt_id={pid}")

    # Poll until all complete.
    paths: list[Path] = []
    remaining = set(s[0] for s in submitted)
    deadline = time.time() + 4 * 3600
    while remaining and time.time() < deadline:
        for pid, name, seed in submitted:
            if pid not in remaining:
                continue
            hist = poll_history(pid)
            if hist:
                p = collect_image(hist, candidates_dir, name)
                if p is not None:
                    paths.append(p)
                    print(f"  collected {name}")
                remaining.discard(pid)
        if remaining:
            time.sleep(15)
    return paths


def run_eval(
    candidates_dir: Path,
    identity: Path,
    visible_refs: Path,
    held_out_refs: Path,
    rubric: Path,
    output_dir: Path,
    anthropic_api_key: str | None,
) -> int:
    """Call eval/compose.py from the shipped problem repo.

    Returns the exit code; on success, output_dir contains scores.json
    and winner.webp.
    """
    cmd = [
        sys.executable, "-m", "eval.compose",
        "--candidates-dir", str(candidates_dir),
        "--identity", str(identity),
        "--visible-refs-dir", str(visible_refs),
        "--held-out-refs-dir", str(held_out_refs),
        "--rubric", str(rubric),
        "--output", str(output_dir),
    ]
    if anthropic_api_key:
        cmd += ["--anthropic-api-key", anthropic_api_key]
    return subprocess.call(cmd)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--workflow", required=True, type=Path)
    ap.add_argument("--prompts", required=True, type=Path)
    ap.add_argument("--seeds", nargs="+", type=int, required=True)
    ap.add_argument("--identity", required=True, type=Path)
    ap.add_argument("--visible-refs", required=True, type=Path)
    ap.add_argument("--held-out-refs", required=True, type=Path)
    ap.add_argument("--rubric", required=True, type=Path)
    ap.add_argument("--output", required=True, type=Path,
                    help="Local dir on Mac to receive scores.json + workflow.json only. "
                         "Images NEVER come back here — they live on the volume "
                         "and are surfaced via S3 presigned URLs.")
    ap.add_argument("--volume-runs-root", required=True, type=Path,
                    help="On the pod: e.g. /runpod-volume/runs/<run_id>/iter_NNN/")
    ap.add_argument("--anthropic-api-key", default=None)
    args = ap.parse_args(argv)

    iter_tmp = Path("/tmp") / f"iter_{int(time.time())}"
    candidates = iter_tmp / "candidates"
    print(f"[pod-runtime] candidates → {candidates}")
    paths = run_batch(args.workflow, args.prompts, args.seeds, candidates)
    print(f"[pod-runtime] collected {len(paths)} candidates")
    if not paths:
        print("[pod-runtime] no candidates — comfyui failed?", file=sys.stderr)
        return 2

    rc = run_eval(
        candidates_dir=candidates,
        identity=args.identity,
        visible_refs=args.visible_refs,
        held_out_refs=args.held_out_refs,
        rubric=args.rubric,
        output_dir=args.output,
        anthropic_api_key=args.anthropic_api_key,
    )
    if rc != 0:
        print(f"[pod-runtime] eval failed rc={rc}", file=sys.stderr)
        return rc

    # Copy the workflow.json into the output dir.
    shutil.copy2(args.workflow, args.output / "workflow.json")

    # Move winner.webp + every candidate THUMBNAIL onto the network volume,
    # not back to the Mac. The orchestrator reads scores.json (still in
    # args.output) and serves images via S3 presigned URLs.
    args.volume_runs_root.mkdir(parents=True, exist_ok=True)
    winner_src = args.output / "winner.webp"
    if winner_src.exists():
        winner_dst = args.volume_runs_root / "winner.webp"
        shutil.move(str(winner_src), str(winner_dst))
        print(f"[pod-runtime] winner → {winner_dst}  (S3 key: runs/<run_id>/iter_NNN/winner.webp)")

    # Optional: generate small thumbnails (256px) of every candidate so the
    # dashboard can show a strip of all 15 without the user clicking into
    # full-res. Tiny disk, big UX win.
    try:
        from PIL import Image
        thumbs_dir = args.volume_runs_root / "thumbs"
        thumbs_dir.mkdir(exist_ok=True)
        for p in paths:
            try:
                img = Image.open(p).convert("RGB")
                img.thumbnail((256, 256))
                img.save(thumbs_dir / f"{p.stem}.webp", "WEBP", quality=80)
            except Exception:
                continue
    except ImportError:
        pass

    # Wipe /tmp/iter_*/candidates — pod's local disk doesn't keep them
    # either. Only the volume copy (full-res winner + thumbs) survives.
    shutil.rmtree(candidates, ignore_errors=True)
    print(f"[pod-runtime] done. Mac gets: scores.json + workflow.json only.")
    print(f"[pod-runtime] Volume gets:    {args.volume_runs_root}/winner.webp + thumbs/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
