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
    pod: PodHandle, creds, workflow_template: str, seeds: list[int],
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

    cmd = (
        f"mkdir -p {remote_output} {iter_volume}; "
        f"cd /runpod-volume/aar/agentic-autoresearch && "
        f"PYTHONPATH=/runpod-volume/aar/comfyui-experiments:$PYTHONPATH "
        f"python3 -m agentic_autoresearch.agents.pod_runtime "
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
        f"> /runpod-volume/runs/{run_id}/iter_{iter_num:04d}.log 2>&1"
    )
    print(f"[iter {iter_num}] submitting pod_runtime via ssh")
    rc = _ssh(pod, cmd, timeout=4 * 3600)
    if rc != 0:
        print(f"[iter {iter_num}] pod_runtime exit={rc}")
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

    with WorldModel(project) as wm:
        cfg_name = f"{workflow_template.replace('.json','')}-iter{iter_num:03d}"
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
    return True


# ─── pod setup helpers ──────────────────────────────────────────────


def _ship_pod_runtime(pod: PodHandle, spec: ProblemSpec) -> None:
    """rsync the framework + experiment repo onto the pod's volume."""
    framework = Path(__file__).resolve().parents[2]  # agentic-autoresearch root
    experiment = spec.repo_path

    print(f"[ship] rsyncing framework → pod: {framework}")
    _rsync_to_pod(pod, framework, "/runpod-volume/aar/agentic-autoresearch/")
    print(f"[ship] rsyncing experiment → pod: {experiment}")
    _rsync_to_pod(pod, experiment, "/runpod-volume/aar/comfyui-experiments/")

    # Ensure runtime deps installed
    print(f"[ship] pip install runtime deps")
    _ssh(pod, "pip install --quiet boto3 pyyaml pillow insightface onnxruntime-gpu "
              "torch torchvision open_clip_torch mediapipe anthropic huggingface_hub")


def _start_comfyui(pod: PodHandle) -> None:
    """Launch ComfyUI on the pod in the background."""
    print(f"[comfyui] starting on pod")
    _ssh(pod, (
        "cd /runpod-volume/ComfyUI && "
        "nohup python3 main.py --listen 0.0.0.0 --port 8188 "
        "> /runpod-volume/comfyui.log 2>&1 & echo started PID=$!"
    ))
    # Wait until ComfyUI's /system_stats endpoint responds.
    import urllib.request, urllib.error
    deadline = time.time() + 180
    proxy_url = pod.comfyui_url + "/system_stats"
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(proxy_url, timeout=10) as r:
                if r.status == 200:
                    print(f"[comfyui] reachable at {pod.comfyui_url}")
                    return
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError):
            time.sleep(5)
    print(f"[comfyui] WARNING: not reachable after 3 min — may still be loading models")


# ─── ssh / scp / rsync helpers ──────────────────────────────────────


def _ssh_args(pod: PodHandle) -> list[str]:
    if not pod.public_ip or not pod.ssh_port:
        raise RuntimeError(f"pod {pod.pod_id} has no ssh endpoint (ip={pod.public_ip} port={pod.ssh_port})")
    return [
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", "LogLevel=ERROR",
        "-i", str(Path.home() / ".ssh" / "id_ed25519"),
        "-p", str(pod.ssh_port),
    ]


def _ssh(pod: PodHandle, cmd: str, timeout: float = 600.0) -> int:
    full = ["ssh"] + _ssh_args(pod) + [f"root@{pod.public_ip}", cmd]
    return subprocess.call(full, timeout=timeout)


def _scp_from_pod(pod: PodHandle, remote: str, local: Path) -> int:
    local.parent.mkdir(parents=True, exist_ok=True)
    full = ["scp"] + _ssh_args(pod) + [f"root@{pod.public_ip}:{remote}", str(local)]
    return subprocess.call(full, timeout=300)


def _rsync_to_pod(pod: PodHandle, local: Path, remote: str) -> int:
    ssh_cmd = "ssh " + " ".join(_ssh_args(pod))
    full = [
        "rsync", "-az",
        "--exclude=__pycache__", "--exclude=.venv", "--exclude=.git",
        "--exclude=.aar", "--exclude=.agentic-autoresearch-worktrees",
        "-e", ssh_cmd,
        str(local).rstrip("/") + "/", f"root@{pod.public_ip}:{remote}",
    ]
    return subprocess.call(full, timeout=900)


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
