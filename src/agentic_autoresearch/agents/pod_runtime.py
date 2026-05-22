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


def submit_workflow(workflow: dict, prompt_text: str, negative: str, seed: int,
                    prompt_node_id: str = "6", negative_node_id: str | None = None,
                    seed_node_id: str | None = None) -> str:
    """Patch the workflow with prompt/seed/negative and submit. Returns prompt_id.

    Node IDs are workflow-specific. The actor's job is to provide a
    workflow where these IDs are documented. Until then, this fn assumes
    the canonical Mickmumpitz / Apatero layouts.
    """
    wf = dict(workflow)  # shallow copy of top-level
    nodes = json.loads(json.dumps(wf.get("nodes") or wf.get("prompt") or {}))

    # Patch prompt text
    if prompt_node_id and prompt_node_id in nodes:
        try:
            nodes[prompt_node_id]["inputs"]["text"] = prompt_text
        except (KeyError, TypeError):
            pass
    if negative_node_id and negative_node_id in nodes:
        try:
            nodes[negative_node_id]["inputs"]["text"] = negative
        except (KeyError, TypeError):
            pass
    if seed_node_id and seed_node_id in nodes:
        try:
            nodes[seed_node_id]["inputs"]["seed"] = seed
        except (KeyError, TypeError):
            pass

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
) -> list[Path]:
    """Submit (prompt × seed) jobs and gather candidate PNGs."""
    workflow = json.loads(workflow_path.read_text())
    prompts_doc = json.loads(prompts_path.read_text())
    prompts = prompts_doc["prompts"]

    candidates_dir.mkdir(parents=True, exist_ok=True)
    submitted: list[tuple[str, str, int]] = []  # (prompt_id, name, seed)
    for p in prompts:
        for seed in seeds:
            name = f"{p['id']}_s{seed}"
            pid = submit_workflow(
                workflow=workflow,
                prompt_text=p["text"],
                negative=p.get("negative", ""),
                seed=seed,
            )
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
    ap.add_argument("--output", required=True, type=Path)
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

    # Copy the workflow.json into the output dir so the orchestrator gets
    # the exact config that produced winner.webp.
    shutil.copy2(args.workflow, args.output / "workflow.json")

    # Wipe /tmp/iter_*/candidates — Mac never sees them.
    shutil.rmtree(candidates, ignore_errors=True)
    print(f"[pod-runtime] done → {args.output}/{{scores.json,winner.webp,workflow.json}}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
