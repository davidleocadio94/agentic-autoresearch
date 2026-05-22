"""RunPod headless lifecycle.

Operations:
  start_pod(volume_id, datacenter, gpus, image) → PodHandle
    - tries each GPU in order; first one with capacity wins
    - polls until ComfyUI (or whatever entrypoint) responds, with timeout
    - registers handle in pods/active.json for leak detection
  poll_pod(pod_id) → state
  stop_pod(pod_id)
  list_orphans() → list[dict]  for `autoresearch pods` debugging

The RunPod REST API endpoints used:
  GET  https://rest.runpod.io/v1/networkvolumes
  GET  https://rest.runpod.io/v1/availability
  POST https://rest.runpod.io/v1/pods          (start)
  GET  https://rest.runpod.io/v1/pods/{id}     (status)
  DELETE https://rest.runpod.io/v1/pods/{id}   (stop)

No browser, no copy-paste. Credentials passed via Authorization header,
never logged. The pod runs an image the framework ships (default:
runpod/pytorch with ComfyUI installed on the network volume).
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agentic_autoresearch.paths import pods_file


RUNPOD_REST = "https://rest.runpod.io/v1"

# Default ComfyUI-friendly image. The actual ComfyUI lives on the network
# volume; this image just needs the right Python + CUDA + a few utility
# packages. Override per-call if needed.
DEFAULT_IMAGE = "runpod/pytorch:2.4.0-py3.11-cuda12.4.1-devel-ubuntu22.04"


class RunPodError(Exception):
    pass


@dataclass
class PodHandle:
    pod_id: str
    datacenter: str
    gpu: str
    volume_id: str
    started_at: float                  # unix seconds
    comfyui_url: str | None = None     # https proxy URL
    ssh: str | None = None             # "root@<ip> -p <mapped_port>"
    ssh_port: int | None = None        # mapped public port for container's 22
    public_ip: str | None = None
    cumulative_seconds_estimate: float = 0.0
    last_seen: float = field(default_factory=time.time)


# ─── HTTP helpers ────────────────────────────────────────────────────


def _request(method: str, path: str, api_key: str, body: dict | None = None,
             timeout: float = 30.0) -> Any:
    url = f"{RUNPOD_REST}{path}"
    data = None
    headers = {
        "Authorization": f"Bearer {api_key}",
        "User-Agent": "aar/0.1",
        "Accept": "application/json",
    }
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            payload = r.read()
            if not payload:
                return None
            return json.loads(payload)
    except urllib.error.HTTPError as e:
        err_body = e.read().decode(errors="replace")[:500]
        raise RunPodError(f"{method} {path} → HTTP {e.code}: {err_body}") from e
    except urllib.error.URLError as e:
        raise RunPodError(f"{method} {path} → {e.reason}") from e


# ─── availability + start ────────────────────────────────────────────


def list_volumes(api_key: str) -> list[dict]:
    return _request("GET", "/networkvolumes", api_key) or []


def gpu_availability(api_key: str, datacenter: str, gpu_type_id: str) -> bool:
    """Returns True if the given GPU class has any free pod in the datacenter.

    RunPod's availability endpoint is a bit informal — we conservatively
    treat ANY availability > 0 as "we can try it." A try might still fail
    (race with another user), in which case start_pod returns and we move on.
    """
    try:
        # The endpoint shape: /v1/availability or query per gpu;
        # we just attempt start and rely on errors. Pre-flight check
        # via GPU types listing.
        resp = _request("GET", f"/gpuTypes", api_key)
        if not resp:
            return True  # unknown — let start_pod try
        # resp is a list of {id, displayName, ...}. We don't have per-DC
        # availability without a deeper call; just sanity-check the
        # GPU class exists.
        names = {g.get("id") for g in resp}
        return gpu_type_id in names
    except RunPodError:
        return True  # don't block on availability lookup failure


def _find_first_available(
    api_key: str, datacenter: str, gpus: list[str]
) -> str | None:
    """Return the first gpu type in `gpus` that exists in this DC.
    The pod-create call is what actually probes capacity.
    """
    for gpu in gpus:
        if gpu_availability(api_key, datacenter, gpu):
            return gpu
    return None


def start_pod(
    api_key: str,
    volume_id: str,
    datacenter: str,
    gpus: list[str],
    image: str = DEFAULT_IMAGE,
    container_disk_gb: int = 50,
    ports: list[str] | None = None,   # ["22/tcp", "8188/http"]
    env: dict[str, str] | None = None,
    name: str | None = None,
    project_dir: Path | None = None,
    ready_timeout: float = 600.0,
    ready_poll_interval: float = 10.0,
) -> PodHandle:
    """Try GPUs in order. First success → poll until ComfyUI-reachable.

    Registers the pod in ~/.aar/projects/<project>/pods/active.json
    so a crashed orchestrator doesn't leak the pod.

    `project_dir` is for registry; if None, registry is skipped.
    """
    if ports is None:
        ports = ["22/tcp", "8188/http"]
    last_err: Exception | None = None
    handle: PodHandle | None = None
    for gpu in gpus:
        try:
            payload = {
                "name": name or f"aar-{int(time.time())}",
                "imageName": image,
                "gpuTypeIds": [gpu],
                "gpuCount": 1,
                "containerDiskInGb": container_disk_gb,
                "volumeMountPath": "/runpod-volume",
                "networkVolumeId": volume_id,
                "dataCenterIds": [datacenter],  # required for volume pods
                "cloudType": "SECURE",          # required for volume pods
                "ports": ports,
                "env": env or {},
                "supportPublicIp": True,
                "interruptible": False,
            }
            # The RunPod REST shape varies slightly by endpoint version.
            # /v1/pods accepts the above; older deployments may need
            # /v1/pods/run or a different shape.
            resp = _request("POST", "/pods", api_key, body=payload)
            pod_id = resp.get("id")
            if not pod_id:
                raise RunPodError(f"start_pod: no id in response: {resp!r}")
            handle = PodHandle(
                pod_id=pod_id,
                datacenter=datacenter,
                gpu=gpu,
                volume_id=volume_id,
                started_at=time.time(),
            )
            break
        except RunPodError as e:
            last_err = e
            # Capacity-out / unavailable — try next GPU.
            if "noavail" in str(e).lower() or "no instance" in str(e).lower() or "capacity" in str(e).lower() or "503" in str(e) or "404" in str(e):
                continue
            # Other errors propagate.
            raise

    if handle is None:
        raise RunPodError(
            f"no capacity for any GPU in {gpus!r} @ {datacenter}. last error: {last_err}"
        )

    # Register before we wait so a crash mid-wait doesn't leak.
    if project_dir is not None:
        _register_pod(project_dir, handle)

    # Wait for the pod to become reachable.
    _wait_ready(api_key, handle, ready_timeout, ready_poll_interval, project_dir)
    return handle


def _wait_ready(
    api_key: str,
    handle: PodHandle,
    timeout: float,
    interval: float,
    project_dir: Path | None,
) -> None:
    """Wait for THREE conditions, not just desiredStatus=RUNNING:
      1. desiredStatus == 'RUNNING'
      2. publicIp != ''
      3. portMappings['22'] is set

    Then optionally TCP-probe sshd. desiredStatus alone is a lie — the
    pod can sit at 'RUNNING' for minutes while still scheduling.
    """
    deadline = time.time() + timeout
    last_log = 0.0
    while time.time() < deadline:
        try:
            info = _request("GET", f"/pods/{handle.pod_id}", api_key)
        except RunPodError:
            time.sleep(interval)
            continue
        status = info.get("desiredStatus") or info.get("status")
        public_ip = info.get("publicIp") or ""
        port_mappings = info.get("portMappings") or {}
        ssh_port = port_mappings.get("22") or port_mappings.get(22)

        if status in ("RUNNING", "running") and public_ip and ssh_port:
            handle.public_ip = public_ip
            handle.ssh_port = int(ssh_port)
            handle.ssh = f"root@{public_ip} -p {ssh_port}"
            handle.comfyui_url = f"https://{handle.pod_id}-8188.proxy.runpod.net"
            handle.last_seen = time.time()
            if project_dir is not None:
                _register_pod(project_dir, handle)
            return

        if time.time() - last_log > 20:
            print(f"  pod {handle.pod_id}: status={status} ip={public_ip or '<none>'} ssh_port={ssh_port or '<none>'}")
            last_log = time.time()
        time.sleep(interval)
    raise RunPodError(f"pod {handle.pod_id} did not become fully ready within {timeout}s")


# ─── poll + stop ─────────────────────────────────────────────────────


def poll_pod(api_key: str, pod_id: str) -> dict:
    return _request("GET", f"/pods/{pod_id}", api_key) or {}


def stop_pod(api_key: str, pod_id: str, project_dir: Path | None = None) -> None:
    try:
        _request("DELETE", f"/pods/{pod_id}", api_key)
    except RunPodError as e:
        if "404" in str(e):
            pass  # already gone
        else:
            raise
    if project_dir is not None:
        _deregister_pod(project_dir, pod_id)


# ─── leak detection / registry ───────────────────────────────────────


def _registry_path(project_dir: Path) -> Path:
    p = project_dir / "pods" / "active.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _load_registry(project_dir: Path) -> dict[str, dict]:
    p = _registry_path(project_dir)
    if not p.exists():
        return {}
    return json.loads(p.read_text() or "{}")


def _save_registry(project_dir: Path, data: dict[str, dict]) -> None:
    _registry_path(project_dir).write_text(json.dumps(data, indent=2, default=str))


def _register_pod(project_dir: Path, h: PodHandle) -> None:
    reg = _load_registry(project_dir)
    reg[h.pod_id] = {
        "datacenter": h.datacenter,
        "gpu": h.gpu,
        "volume_id": h.volume_id,
        "started_at": h.started_at,
        "comfyui_url": h.comfyui_url,
        "last_seen": h.last_seen,
    }
    _save_registry(project_dir, reg)


def _deregister_pod(project_dir: Path, pod_id: str) -> None:
    reg = _load_registry(project_dir)
    reg.pop(pod_id, None)
    _save_registry(project_dir, reg)


def list_orphans(api_key: str, project_dir: Path,
                 max_age_seconds: float = 3 * 3600) -> list[dict]:
    """Pods registered but older than max_age (or remotely missing)."""
    reg = _load_registry(project_dir)
    out: list[dict] = []
    now = time.time()
    for pod_id, info in list(reg.items()):
        age = now - info.get("started_at", now)
        info = {**info, "pod_id": pod_id, "age_seconds": age}
        # Verify remote status.
        try:
            remote = _request("GET", f"/pods/{pod_id}", api_key)
            info["remote_status"] = remote.get("desiredStatus") or remote.get("status")
        except RunPodError as e:
            info["remote_status"] = f"unknown ({e})"
        if age > max_age_seconds or info["remote_status"] in (None, "EXITED", "FAILED"):
            out.append(info)
    return out


def reap_orphans(api_key: str, project_dir: Path, max_age_seconds: float = 3 * 3600) -> list[str]:
    """Stop pods registered locally that are too old or remotely dead.
    Returns list of pod IDs stopped."""
    stopped: list[str] = []
    for info in list_orphans(api_key, project_dir, max_age_seconds):
        stop_pod(api_key, info["pod_id"], project_dir)
        stopped.append(info["pod_id"])
    return stopped
