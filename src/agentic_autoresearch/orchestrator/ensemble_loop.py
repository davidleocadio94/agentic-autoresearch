"""Ensemble-eval loop entrypoint (image-gen-shaped problems).

The original orchestrator/loop.py is OMR-shaped (deterministic eval,
fast actor, source-code edits). This file is the sibling for comfyui-
character-shaped problems: pod-side rendering, ensemble eval on the
pod, scores.json + presigned URLs back to the Mac.

Minimum viable for the FIRST RUN. Cuts I'm consciously taking:
  - planner is single-pass (no plateau detection across runs yet)
  - reflector's tool_calls JSON is captured but world-model writes are
    done via direct WorldModel calls based on score deltas (the
    tool_calls list is logged for inspection but not executed verbatim
    — full MCP-style dispatch is next session)
  - forced_first_iterations cycles through the seed.jsonl entries in
    order; index = (iter_num - 1) mod 3
  - pod is reused across iters within a run (don't start/stop per iter)
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import signal
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from agentic_autoresearch.agents.prompts import prompts_for_spec
from agentic_autoresearch.agents.runpod import (
    PodHandle,
    poll_pod,
    start_pod,
    stop_pod,
)
from agentic_autoresearch.agents.spawn import parse_json_block, run_agent
from agentic_autoresearch.credentials import env_for, load as load_creds
from agentic_autoresearch.memory.schema import init_db
from agentic_autoresearch.memory.world_model import WorldModel
from agentic_autoresearch.paths import iter_dir, project_dir
from agentic_autoresearch.spec import ProblemSpec, parse_spec


@dataclass
class EnsembleLoopOptions:
    max_hours: float | None = None
    max_iters: int | None = None
    target_composite: float | None = None
    target_arcface: float | None = None
    stop_pod_after: bool = True


def run_ensemble_loop(spec_path: Path, opts: EnsembleLoopOptions | None = None) -> str:
    """Entry. Returns run_id (a hex string)."""
    opts = opts or EnsembleLoopOptions()
    spec = parse_spec(spec_path)
    fm = spec.raw_front_matter or {}
    project = fm.get("name") or spec.name

    init_db(project=project)

    creds = load_creds()
    creds.require("anthropic", "runpod")

    target = fm.get("target") or {}
    target_composite = opts.target_composite or float(target.get("mean_composite", 8.5))
    target_arcface = opts.target_arcface or float(target.get("arcface_mean", 0.75))
    budget = fm.get("budget") or {}
    max_iters = opts.max_iters or 30
    max_hours = opts.max_hours or float(budget.get("pod_hours", 30))
    deadline = time.monotonic() + max_hours * 3600

    forced = fm.get("forced_first_iterations") or []
    seeds_per = int(fm.get("replicates_per_config", 3))

    run_id = uuid.uuid4().hex[:12]
    print(f"[ensemble] run_id={run_id} project={project}")
    print(f"[ensemble] target: composite≥{target_composite} arcface≥{target_arcface}")
    print(f"[ensemble] forced first iters: {forced}")
    print(f"[ensemble] seeds per config: {seeds_per}")

    pod: PodHandle | None = None
    try:
        # Start ONE pod for the entire run. Re-used across iters.
        print(f"[ensemble] starting pod (gpus={creds.runpod.gpus}, dc={creds.runpod.datacenter})")
        pod = start_pod(
            api_key=creds.runpod.api_key,
            volume_id=creds.runpod.volume_id,
            datacenter=creds.runpod.datacenter,
            gpus=creds.runpod.gpus,
            ports=["22/tcp", "8188/http"],
            project_dir=project_dir(project),
            ready_timeout=600.0,
            ready_poll_interval=10.0,
            name=f"aar-{project}-{run_id[:6]}",
        )
        print(f"[ensemble] pod ready: {pod.pod_id} gpu={pod.gpu} ssh={pod.ssh}")

        # Ship pod_runtime.py + the experiment repo onto the pod.
        _ship_pod_runtime(pod, spec)

        # Start ComfyUI on the pod (background).
        _start_comfyui(pod)

        # Run iterations.
        for iter_num in range(1, max_iters + 1):
            if time.monotonic() > deadline:
                print(f"[ensemble] max-hours reached")
                break

            # Pick category: forced for first N, then planner-free
            category, parent_name, workflow_template = _pick_iter(
                iter_num, forced, spec, project
            )
            seeds = [_rand_seed() for _ in range(seeds_per)]

            print(f"\n[ensemble] === ITER {iter_num} ===")
            print(f"  category: {category}  parent: {parent_name}")
            print(f"  workflow: {workflow_template}  seeds: {seeds}")

            iter_local = iter_dir(project, iter_num)
            iter_volume = Path(f"/runpod-volume/runs/{run_id}/iter_{iter_num:04d}")

            ok = _run_one_ensemble_iter(
                spec=spec, project=project, run_id=run_id, iter_num=iter_num,
                pod=pod, creds=creds,
                category=category, parent_name=parent_name,
                workflow_template=workflow_template,
                seeds=seeds,
                iter_local=iter_local,
                iter_volume=iter_volume,
            )
            print(f"[ensemble] iter {iter_num}: {'OK' if ok else 'FAILED'}")

            # Check target after each iter.
            best = _best_so_far(project)
            if best and best.get("composite") >= target_composite \
                    and best.get("arcface") >= target_arcface:
                print(f"[ensemble] TARGET REACHED: composite={best['composite']} "
                      f"arcface={best['arcface']}")
                break

    finally:
        if pod and opts.stop_pod_after:
            try:
                print(f"[ensemble] stopping pod {pod.pod_id}")
                stop_pod(creds.runpod.api_key, pod.pod_id, project_dir(project))
            except Exception as e:
                print(f"[ensemble] failed to stop pod: {e}")

    return run_id


# ─── per-iter execution ──────────────────────────────────────────────


def _run_one_ensemble_iter(
    spec: ProblemSpec, project: str, run_id: str, iter_num: int,
    pod: PodHandle, creds,
    category: str, parent_name: str | None,
    workflow_template: str, seeds: list[int],
    iter_local: Path, iter_volume: Path,
) -> bool:
    """Submit workflow on pod via pod_runtime.py, wait for results."""
    # Submit on the pod (one ssh command, daemonized).
    # The actor's normal job is to BUILD the workflow_template's path
    # from the planner's config_changes. For forced iters, we just use
    # the workflow file as-is — no per-iter config changes.

    remote_workflow = f"/runpod-volume/aar/comfyui-experiments/workflows/{workflow_template}"
    remote_prompts = "/runpod-volume/aar/comfyui-experiments/eval/prompts.json"
    remote_identity = "/runpod-volume/aar/comfyui-experiments/eval/references/identity.png"
    remote_visible = "/runpod-volume/aar/comfyui-experiments/eval/references/visible"
    remote_held_out = "/runpod-volume/aar/comfyui-experiments/eval/references/held_out"
    remote_rubric = "/runpod-volume/aar/comfyui-experiments/eval/rubric.md"
    remote_output = f"/runpod-volume/runs/{run_id}/iter_{iter_num:04d}_output"

    seeds_arg = " ".join(str(s) for s in seeds)
    anthropic_key = creds.anthropic.api_key

    # S3 env vars for pod_runtime to upload images and generate presigned URLs.
    # When not configured, pod_runtime soft-fails and the loop falls back to
    # SCP-on-demand (slower but works).
    s3_env_prefix = ""
    if creds.runpod_s3 is not None:
        from agentic_autoresearch.agents.s3 import endpoint_for
        s3_endpoint = endpoint_for(creds.runpod.datacenter)
        s3_region = creds.runpod.datacenter.lower()
        # iter-level key prefix; reflector will use this verbatim.
        s3_key_prefix = f"runs/{run_id}/iter_{iter_num:04d}/"
        s3_env_prefix = (
            f"AAR_S3_ENDPOINT={s3_endpoint} "
            f"AAR_S3_BUCKET={creds.runpod.volume_id} "
            f"AAR_S3_ACCESS_KEY={creds.runpod_s3.access_key} "
            f"AAR_S3_SECRET_KEY={creds.runpod_s3.secret_key} "
            f"AAR_S3_REGION={s3_region} "
            f"AAR_S3_KEY_PREFIX={s3_key_prefix} "
        )

    # Launch pod_runtime DETACHED on the pod (setsid + redirect all fds), then
    # poll the iter log for completion sentinel. This avoids ssh sessions
    # that hang for an hour after pod_runtime exits.
    iter_log = f"/runpod-volume/runs/{run_id}/iter_{iter_num:04d}.log"
    rc_file = f"/runpod-volume/runs/{run_id}/iter_{iter_num:04d}.rc"
    launch_cmd = (
        f"mkdir -p {remote_output} {iter_volume}; "
        f"rm -f {rc_file}; "
        f"(setsid nohup bash -c '"
        f"  cd /runpod-volume/aar/agentic-autoresearch && "
        f"  {s3_env_prefix}"
        f"  PYTHONPATH=/runpod-volume/aar/agentic-autoresearch/src:"
        f"/runpod-volume/aar/comfyui-experiments:$PYTHONPATH "
        f"  python3.11 -m agentic_autoresearch.agents.pod_runtime "
        f"  --workflow {remote_workflow} "
        f"  --prompts {remote_prompts} "
        f"  --seeds {seeds_arg} "
        f"  --identity {remote_identity} "
        f"  --visible-refs {remote_visible} "
        f"  --held-out-refs {remote_held_out} "
        f"  --rubric {remote_rubric} "
        f"  --output {remote_output} "
        f"  --volume-runs-root {iter_volume} "
        f"  --anthropic-api-key {anthropic_key} "
        f"  ; echo $? > {rc_file}"
        f"' </dev/null >{iter_log} 2>&1 &) && echo launched"
    )
    print(f"[iter {iter_num}] launching pod_runtime (detached)")
    rc = _ssh(pod, launch_cmd, timeout=30)
    if rc != 0:
        print(f"[iter {iter_num}] failed to launch pod_runtime (ssh rc={rc})")
        return False

    # Poll for the .rc sentinel every 60s, up to 4h.
    print(f"[iter {iter_num}] polling for completion")
    deadline = time.monotonic() + 4 * 3600
    last_progress_log = 0.0
    while time.monotonic() < deadline:
        # Fast check via ssh test -f
        check = _ssh(pod, f"test -f {rc_file} && cat {rc_file}", timeout=15)
        if check == 0:
            # cat returned 0 but exit code is in stdout (we couldn't capture
            # via _ssh which uses subprocess.call). Read with separate command.
            import subprocess
            rc_out = subprocess.run(
                ["ssh"] + _ssh_args(pod) + [f"root@{pod.public_ip}", f"cat {rc_file}"],
                capture_output=True, text=True, timeout=15,
            )
            exit_code = int(rc_out.stdout.strip() or "1")
            print(f"[iter {iter_num}] pod_runtime exit={exit_code}")
            if exit_code != 0:
                print(f"[iter {iter_num}] tailing iter log:")
                _ssh(pod, f"tail -60 {iter_log} || true", timeout=30)
                return False
            break
        # Progress beat every 60s
        if time.monotonic() - last_progress_log > 60:
            _ssh(pod, f"tail -1 {iter_log} 2>/dev/null || true", timeout=15)
            last_progress_log = time.monotonic()
        time.sleep(60)
    else:
        print(f"[iter {iter_num}] timed out after 4h waiting for {rc_file}")
        return False

    # Pull scores.json + workflow.json back to the Mac.
    scp_rc = _scp_from_pod(pod,
        f"{remote_output}/scores.json", iter_local / "scores.json")
    scp2_rc = _scp_from_pod(pod,
        f"{remote_output}/workflow.json", iter_local / "workflow.json")
    if scp_rc != 0 or scp2_rc != 0:
        print(f"[iter {iter_num}] failed to pull results")
        return False

    # Save the config in the world model.
    scores = json.loads((iter_local / "scores.json").read_text())
    composite_mean = scores.get("per_signal", {}).get("composite", {}).get("mean", 0)
    composite_std = scores.get("per_signal", {}).get("composite", {}).get("std", 0)
    arcface_mean = scores.get("per_signal", {}).get("arcface_cosine", {}).get("mean", 0)

    cfg_name = f"{workflow_template.replace('.json','')}-iter{iter_num:03d}"
    iter_id = f"{run_id}:iter_{iter_num:04d}"

    # Pull winner.webp from the pod's volume for the reflector to read.
    # RunPod's S3 presigned URLs return 401 (their facade ignores
    # query-string auth); SCP is the reliable path while the pod is alive.
    winner_local = iter_local / "winner.webp"
    winner_remote = str(iter_volume / "winner.webp")
    rc = _scp_from_pod(pod, winner_remote, winner_local)
    if rc != 0:
        print(f"[iter {iter_num}] could not pull winner.webp (rc={rc}); reflector may degrade")
    with WorldModel(project) as wm:
        wm.save_configuration(
            name=cfg_name,
            workflow_json=(iter_local / "workflow.json").read_text(),
            param_dict={"workflow_template": workflow_template, "seeds": seeds},
            best_score=composite_mean,
            best_score_std=composite_std,
            composite_mean=composite_mean,
            arcface_mean=arcface_mean,
            replicates=len(seeds),
            provenance_iter_id=None,
            sample_image_path=f"runs/{run_id}/iter_{iter_num:04d}/winner.webp",
            sample_image_embedding=None,
        )
        print(f"[iter {iter_num}] saved config '{cfg_name}' "
              f"composite={composite_mean:.3f}±{composite_std:.3f} arcface={arcface_mean:.3f}")

    # Local reflector: claude -p (subscription-auth'd) looks at the winner
    # via S3 presigned URL, writes structured beliefs to the world model.
    from agentic_autoresearch.agents.reflector import (
        dispatch_tool_calls,
        reflect,
    )
    # Load the matching seed.jsonl entry for the workflow used.
    stack_entry: dict = {}
    seed_path = spec.repo_path / "references" / "seed.jsonl"
    if seed_path.exists():
        for line in seed_path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
                if Path(e.get("workflow_template", "")).name == workflow_template:
                    stack_entry = e
                    break
            except json.JSONDecodeError:
                continue

    reflector_log = iter_local / "reflector.log"
    print(f"[iter {iter_num}] running local reflector (claude -p, subscription)")
    reflection = reflect(
        project=project,
        iter_num=iter_num,
        iter_id=iter_id,
        category=category,
        parent_config=parent_name,
        stack_entry=stack_entry,
        params={"workflow_template": workflow_template, "seeds": seeds,
                **(stack_entry.get("params") or {})},
        scores=scores,
        target_composite=8.5,
        winner_local_path=str(winner_local),
        log_path=reflector_log,
    )
    if reflection is None:
        print(f"[iter {iter_num}] reflector returned no result (continuing)")
    else:
        (iter_local / "reflection.json").write_text(json.dumps(reflection, indent=2))
        n = dispatch_tool_calls(project=project, reflection=reflection, iter_id=iter_id)
        print(f"[iter {iter_num}] reflector → {n} world-model write(s); "
              f"next_direction: {reflection.get('next_direction')!r}")
    return True


# ─── pod setup helpers ──────────────────────────────────────────────


def _ship_pod_runtime(pod: PodHandle, spec: ProblemSpec) -> None:
    """Install rsync (if missing), then rsync framework + experiment repo
    onto the pod's volume."""
    # parents[0]=orchestrator  [1]=agentic_autoresearch  [2]=src  [3]=repo root
    framework = Path(__file__).resolve().parents[3]
    experiment = spec.repo_path
    print(f"[ship] framework root: {framework}")
    print(f"[ship] experiment root: {experiment}")

    # The runpod/base image doesn't ship rsync — apt-install first.
    print(f"[ship] ensuring rsync on pod + creating target dirs")
    rc = _ssh(pod, (
        "set -e; "
        "which rsync || (apt-get update -qq && apt-get install -y -qq rsync); "
        "mkdir -p /runpod-volume/aar/agentic-autoresearch "
        "         /runpod-volume/aar/comfyui-experiments"
    ))
    if rc != 0:
        raise RuntimeError(f"failed to install rsync / mkdir on pod (exit {rc})")

    print(f"[ship] rsyncing framework → pod ({framework})")
    rc = _rsync_to_pod(pod, framework, "/runpod-volume/aar/agentic-autoresearch/")
    if rc != 0:
        raise RuntimeError(f"rsync framework failed (exit {rc})")
    print(f"[ship] rsyncing experiment → pod ({experiment})")
    rc = _rsync_to_pod(pod, experiment, "/runpod-volume/aar/comfyui-experiments/")
    if rc != 0:
        raise RuntimeError(f"rsync experiment failed (exit {rc})")

    # CRITICAL: this image has python3 → 3.10 AND python3.11. ComfyUI runs
    # under 3.11. Install ALL deps into 3.11 so ComfyUI's nodes can import
    # them. Use python3.11 -m pip explicitly.
    PIP = "python3.11 -m pip install --quiet"
    print(f"[ship] pip(3.11) install ComfyUI requirements + custom-node deps")
    rc = _ssh(pod,
        f"{PIP} -r /runpod-volume/ComfyUI/requirements.txt && "
        f"{PIP} facexlib insightface onnxruntime-gpu timm einops ftfy",
        timeout=600,
    )
    if rc != 0:
        print(f"[ship] WARNING: ComfyUI pip returned {rc}")

    # Then framework runtime deps (eval pipeline). Also into 3.11 so the
    # pod_runtime invocation can import everything. boto3 is needed for the
    # S3-API publish step.
    print(f"[ship] pip(3.11) install eval pipeline deps")
    rc = _ssh(pod,
        f"{PIP} boto3 pyyaml pillow open_clip_torch mediapipe huggingface_hub",
        timeout=600,
    )
    if rc != 0:
        print(f"[ship] WARNING: eval pip returned {rc}; continuing anyway")

    # Copy reference images into ComfyUI/input/ so LoadImage nodes can find
    # them by basename (identity.png, brooke_*.png).
    print(f"[ship] copying references into ComfyUI/input/")
    _ssh(pod,
        "mkdir -p /runpod-volume/ComfyUI/input && "
        "cp /runpod-volume/aar/comfyui-experiments/eval/references/identity.png "
        "   /runpod-volume/ComfyUI/input/ && "
        "cp /runpod-volume/aar/comfyui-experiments/eval/references/visible/*.png "
        "   /runpod-volume/ComfyUI/input/ 2>/dev/null || true && "
        "cp /runpod-volume/aar/comfyui-experiments/eval/references/held_out/*.png "
        "   /runpod-volume/ComfyUI/input/ 2>/dev/null || true && "
        "ls /runpod-volume/ComfyUI/input/")


def _start_comfyui(pod: PodHandle, timeout_seconds: float = 600.0) -> None:
    """Launch ComfyUI on the pod in the background. Probe localhost via
    SSH (Cloudflare proxy has its own warmup latency we don't want to fight)."""
    print(f"[comfyui] starting on pod")
    # Critical: fully detach from the SSH session. `nohup ... &` is not
    # enough — SSH still waits because stdout/stderr are tied to the
    # session's pty. Redirect ALL fds and disown.
    # Kill any prior ComfyUI on the pod (stale process across runs).
    _ssh(pod, "pkill -f 'python.*main.py.*--listen' 2>/dev/null; true", timeout=10)
    _ssh(pod,
        "cd /runpod-volume/ComfyUI && "
        "(setsid nohup python3.11 main.py --listen 0.0.0.0 --port 8188 "
        " </dev/null >/runpod-volume/comfyui.log 2>&1 &) && "
        "echo 'launched'",
        timeout=30,
    )
    deadline = time.time() + timeout_seconds
    last = 0.0
    while time.time() < deadline:
        # Probe FROM the pod itself — bypasses Cloudflare 524 nonsense.
        rc = _ssh(pod, "curl --max-time 5 -sf http://127.0.0.1:8188/system_stats > /dev/null", timeout=20)
        if rc == 0:
            print(f"[comfyui] reachable on pod localhost:8188 (proxy: {pod.comfyui_url})")
            return
        if time.time() - last > 30:
            print(f"[comfyui] still booting ({int(time.time() - (deadline - timeout_seconds))}s elapsed)")
            last = time.time()
        time.sleep(10)
    # Don't fail run — pod_runtime will fail fast with a clear error
    print(f"[comfyui] WARNING: not reachable after {int(timeout_seconds)}s. last 30 log lines:")
    _ssh(pod, "tail -30 /runpod-volume/comfyui.log || true")


# ─── ssh / scp / rsync helpers ──────────────────────────────────────


def _ssh_args(pod: PodHandle) -> list[str]:
    if not pod.public_ip or not pod.ssh_port:
        raise RuntimeError(f"pod {pod.pod_id} has no ssh endpoint (ip={pod.public_ip} port={pod.ssh_port})")
    return [
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", "LogLevel=ERROR",
        # Keep-alive + dead-peer detection so long iter SSH sessions
        # don't hang silently when the RunPod proxy goes stale.
        "-o", "ServerAliveInterval=30",
        "-o", "ServerAliveCountMax=4",
        "-o", "TCPKeepAlive=yes",
        "-i", str(Path.home() / ".ssh" / "id_ed25519"),
        "-p", str(pod.ssh_port),
    ]


def _ssh(pod: PodHandle, cmd: str, timeout: float = 600.0) -> int:
    full = ["ssh"] + _ssh_args(pod) + [f"root@{pod.public_ip}", cmd]
    return subprocess.call(full, timeout=timeout)


def _scp_from_pod(pod: PodHandle, remote: str, local: Path) -> int:
    local.parent.mkdir(parents=True, exist_ok=True)
    # scp uses -P (capital) for port; ssh uses -p (lowercase).
    # _ssh_args returns "-p PORT" for ssh, so swap to "-P PORT" for scp.
    args = _ssh_args(pod)
    scp_args = []
    skip_next = False
    for i, a in enumerate(args):
        if skip_next:
            skip_next = False
            continue
        if a == "-p" and i + 1 < len(args):
            scp_args.extend(["-P", args[i + 1]])
            skip_next = True
        else:
            scp_args.append(a)
    full = ["scp"] + scp_args + [f"root@{pod.public_ip}:{remote}", str(local)]
    res = subprocess.run(full, capture_output=True, text=True, timeout=300)
    if res.returncode != 0:
        print(f"  scp {remote} → {local} FAILED rc={res.returncode}")
        if res.stderr:
            print(f"  stderr: {res.stderr.strip()[:300]}")
    return res.returncode


def _rsync_to_pod(pod: PodHandle, local: Path, remote: str) -> int:
    ssh_cmd = "ssh " + " ".join(_ssh_args(pod))
    full = [
        "rsync", "-az", "--no-perms", "--no-owner", "--no-group",
        "--exclude=__pycache__", "--exclude=.venv", "--exclude=venv",
        "--exclude=.git", "--exclude=.aar",
        "--exclude=.agentic-autoresearch-worktrees",
        "--exclude=*.pyc", "--exclude=node_modules",
        "--exclude=.pytest_cache", "--exclude=.ruff_cache",
        # ensure target dir exists on pod
        "--mkpath" if _rsync_supports_mkpath() else "",
        "-e", ssh_cmd,
        str(local).rstrip("/") + "/", f"root@{pod.public_ip}:{remote}",
    ]
    full = [arg for arg in full if arg]
    return subprocess.call(full, timeout=900)


def _rsync_supports_mkpath() -> bool:
    """rsync 3.2.3+ supports --mkpath; older versions silently fail with no
    target. Test once."""
    try:
        out = subprocess.run(["rsync", "--version"], capture_output=True, text=True, timeout=5)
        first = (out.stdout or "").splitlines()[0]
        # "rsync  version 3.2.7  protocol version 31"
        ver = first.split()[2] if len(first.split()) >= 3 else "0"
        major, minor, *_ = (int(x) for x in ver.split(".") + ["0"]) if "." in ver else (0, 0)
        return (major, minor) >= (3, 2)
    except Exception:
        return False


# ─── iter selection + world-model queries ───────────────────────────


def _pick_iter(iter_num: int, forced: list[str], spec: ProblemSpec,
               project: str) -> tuple[str, str | None, str]:
    """Returns (category, parent_name, workflow_template_filename).

    For the FIRST RUN: forced iters cycle through seed.jsonl entries
    in the order declared in spec.frontmatter. Each entry maps to a
    workflow file in workflows/<name>.json.
    """
    seed_path = spec.repo_path / "references" / "seed.jsonl"
    seeds = []
    if seed_path.exists():
        for line in seed_path.read_text().splitlines():
            line = line.strip()
            if not line: continue
            seeds.append(json.loads(line))
    seed_by_name = {s["name"]: s for s in seeds}

    if iter_num <= len(forced):
        name = forced[iter_num - 1]
        seed = seed_by_name.get(name)
        if not seed:
            raise RuntimeError(f"forced iter {iter_num}: no seed.jsonl entry named {name!r}")
        return "forced-reference-stack", None, Path(seed["workflow_template"]).name

    # Past forced: pick the best so far and re-run with new seeds.
    # (Real planner-driven dispatch comes next session.)
    best = _best_so_far(project)
    if best is None:
        # Cycle the forced list again
        name = forced[(iter_num - 1) % len(forced)]
        seed = seed_by_name[name]
        return "fallback-forced", None, Path(seed["workflow_template"]).name
    # Re-use best's workflow_template
    wf = best.get("workflow_template", forced[0])
    if not wf.endswith(".json"):
        wf = Path(wf).name
    return "replicate-best", best.get("name"), wf


def _best_so_far(project: str) -> dict | None:
    with WorldModel(project) as wm:
        c = wm.best_configuration()
        if c is None:
            return None
        return {
            "name": c.name,
            "composite": c.composite_mean or c.best_score,
            "arcface": c.arcface_mean or 0.0,
            "workflow_template": (c.param_dict or {}).get("workflow_template"),
        }


def _rand_seed() -> int:
    return secrets.randbits(31)
