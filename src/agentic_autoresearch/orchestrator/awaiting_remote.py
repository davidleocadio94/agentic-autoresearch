"""Async-act helper: actor submits a remote job and exits; orchestrator
polls until results land, then continues the iteration.

The contract:
  - Actor's working directory contains `pending_job.json` after the
    actor exits. Shape:
      {
        "kind": "runpod-comfyui",       # opaque to the orchestrator
        "pod_id": "...",
        "expected_results_path": "<absolute path to local landing dir>",
        "submitted_at": iso8601,
        "expected_complete_at": iso8601,   # optional, hint only
        "timeout_seconds": 7200             # max wait before failing iter
      }
  - The remote job, upon completion, POSTs (or writes) results into the
    expected_results_path. Specifically:
        winner.webp
        scores.json
        workflow.json
  - We poll filesystem (and pod status as a sanity check) every
    `poll_interval` seconds until all three files exist OR timeout.

The orchestrator does NOT hold a claude -p subprocess open during the
wait. The actor process exited cleanly; we're just watching disk.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from agentic_autoresearch.agents.runpod import poll_pod, stop_pod
from agentic_autoresearch.credentials import load


PENDING_JOB_FILENAME = "pending_job.json"
EXPECTED_RESULTS_FILES = ("scores.json", "winner.webp", "workflow.json")


def has_pending_job(work_dir: Path) -> bool:
    return (work_dir / PENDING_JOB_FILENAME).exists()


def read_pending(work_dir: Path) -> dict:
    return json.loads((work_dir / PENDING_JOB_FILENAME).read_text())


def clear_pending(work_dir: Path) -> None:
    p = work_dir / PENDING_JOB_FILENAME
    if p.exists():
        p.unlink()


def wait_for_results(
    work_dir: Path,
    poll_interval: float = 300.0,
    on_tick: Callable[[dict], None] | None = None,
    stop_event=None,
) -> dict:
    """Block until all EXPECTED_RESULTS_FILES exist in results dir, or timeout.

    Returns dict with shape:
        {"status": "ok", "results_dir": Path}
      or {"status": "timeout"}
      or {"status": "pod_dead"}
      or {"status": "stopped"}

    `stop_event` (threading.Event) lets a SIGTERM unblock the wait.
    `on_tick` is called once per poll with the latest poll info — useful
    for dashboards.
    """
    job = read_pending(work_dir)
    results_dir = Path(job["expected_results_path"])
    timeout = float(job.get("timeout_seconds", 7200))
    deadline = time.monotonic() + timeout
    pod_id = job.get("pod_id")
    creds = load()
    api_key = creds.runpod.api_key if creds.runpod else None

    while True:
        if stop_event is not None and stop_event.is_set():
            return {"status": "stopped"}
        if time.monotonic() > deadline:
            return {"status": "timeout"}

        # Check filesystem for the expected outputs.
        if all((results_dir / f).exists() for f in EXPECTED_RESULTS_FILES):
            return {"status": "ok", "results_dir": results_dir}

        # Sanity check the pod is still alive.
        if pod_id and api_key:
            try:
                info = poll_pod(api_key, pod_id)
                status = info.get("desiredStatus") or info.get("status")
                if on_tick:
                    on_tick({"pod_status": status, "results_dir": results_dir})
                if status in ("EXITED", "FAILED", "TERMINATED"):
                    return {"status": "pod_dead", "pod_status": status}
            except Exception as e:
                # Transient API failure; just keep waiting.
                if on_tick:
                    on_tick({"pod_poll_error": str(e)})

        time.sleep(poll_interval)


def write_pending_job(
    work_dir: Path,
    kind: str,
    pod_id: str,
    expected_results_path: Path,
    timeout_seconds: int = 7200,
) -> Path:
    """Helper the actor (or a test) uses to mark an iteration as
    awaiting_remote. Writes pending_job.json in `work_dir`."""
    payload = {
        "kind": kind,
        "pod_id": pod_id,
        "expected_results_path": str(expected_results_path),
        "submitted_at": datetime.now(timezone.utc).isoformat(),
        "timeout_seconds": timeout_seconds,
    }
    p = work_dir / PENDING_JOB_FILENAME
    p.write_text(json.dumps(payload, indent=2))
    return p


def read_remote_results(results_dir: Path) -> dict:
    """Parse what the pod POSTed back into a dict shaped like the
    legacy eval_output.json the loop expects:
        {"score": float, "per_fixture": {...}, "raw": {...full scores.json...}}
    """
    scores_json = json.loads((results_dir / "scores.json").read_text())
    composite = scores_json.get("per_signal", {}).get("composite", {}).get("mean")
    return {
        "score": composite if composite is not None else 0.0,
        "per_fixture": {
            # Use per_signal as "per_fixture" so the loop's plateau /
            # confidence math works without changes.
            k: v.get("mean", 0.0)
            for k, v in scores_json.get("per_signal", {}).items()
        },
        "raw": scores_json,
    }
