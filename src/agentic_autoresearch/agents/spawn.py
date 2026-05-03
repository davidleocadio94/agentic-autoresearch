"""Spawn `claude -p` headless agents and consume stream-json output.

Every agent is the same binary, same flags, different prompt + deadline.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path


@dataclass
class AgentResult:
    exit_code: int
    duration_seconds: int
    final_message: str | None
    last_event: dict | None
    cost_usd: float
    timed_out: bool
    log_path: Path
    events: list[dict]


def claude_binary() -> str:
    """Resolve the claude CLI; respect $CLAUDE_BINARY override."""
    bin_ = os.environ.get("CLAUDE_BINARY") or shutil.which("claude")
    if not bin_:
        raise FileNotFoundError("`claude` CLI not found on PATH (set $CLAUDE_BINARY)")
    return bin_


def run_agent(
    prompt: str,
    *,
    cwd: Path,
    log_path: Path,
    deadline_seconds: int,
    max_turns: int,
    on_event: Callable[[dict], None] | None = None,
) -> AgentResult:
    """Spawn `claude -p`, stream-json on stdout, kill on deadline.

    `on_event(evt)` fires for every parsed JSON event (used for live
    dashboard + watchdog progress detection).
    """
    log_path.parent.mkdir(parents=True, exist_ok=True)
    bin_ = claude_binary()
    cmd = [
        bin_,
        "-p",
        prompt,
        "--dangerously-skip-permissions",
        "--output-format",
        "stream-json",
        "--verbose",
        "--max-turns",
        str(max_turns),
    ]
    started = time.monotonic()
    proc = subprocess.Popen(
        cmd,
        cwd=str(cwd),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        start_new_session=True,  # new pgrp so we can SIGTERM the whole tree
    )
    events: list[dict] = []
    final_message: str | None = None
    last_event: dict | None = None
    cost_usd = 0.0
    timed_out = False

    log_fh = log_path.open("w")
    try:
        # Watchdog thread enforces hard deadline.
        def _watchdog():
            nonlocal timed_out
            time.sleep(deadline_seconds)
            if proc.poll() is None:
                timed_out = True
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                    time.sleep(5)
                    if proc.poll() is None:
                        os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass

        threading.Thread(target=_watchdog, daemon=True).start()

        assert proc.stdout is not None
        for line in proc.stdout:
            log_fh.write(line)
            log_fh.flush()
            line = line.strip()
            if not line:
                continue
            try:
                evt = json.loads(line)
            except json.JSONDecodeError:
                continue
            events.append(evt)
            last_event = evt
            if on_event:
                try:
                    on_event(evt)
                except Exception:
                    pass
            kind = evt.get("type")
            if kind == "result":
                cost_usd = float(evt.get("total_cost_usd") or evt.get("cost_usd") or 0.0)
                final_message = evt.get("result") or evt.get("message")
            elif kind == "assistant":
                msg = evt.get("message") or {}
                content = msg.get("content")
                if isinstance(content, list):
                    for piece in content:
                        if piece.get("type") == "text":
                            final_message = piece.get("text", final_message)
                elif isinstance(content, str):
                    final_message = content
        rc = proc.wait()
    finally:
        log_fh.close()
    return AgentResult(
        exit_code=rc,
        duration_seconds=int(time.monotonic() - started),
        final_message=final_message,
        last_event=last_event,
        cost_usd=cost_usd,
        timed_out=timed_out,
        log_path=log_path,
        events=events,
    )


def parse_json_block(text: str) -> dict | None:
    """Extract a fenced ```json ...``` block or the whole string."""
    if not text:
        return None
    s = text.strip()
    if s.startswith("```"):
        # strip fence
        lines = [ln for ln in s.splitlines() if not ln.startswith("```")]
        s = "\n".join(lines).strip()
    try:
        v = json.loads(s)
        return v if isinstance(v, dict) else None
    except json.JSONDecodeError:
        # try to find a {...} block
        start = s.find("{")
        end = s.rfind("}")
        if start != -1 and end > start:
            try:
                v = json.loads(s[start : end + 1])
                return v if isinstance(v, dict) else None
            except json.JSONDecodeError:
                return None
    return None


def stream_events_from_log(log_path: Path) -> Iterator[dict]:
    """For replay in the dashboard."""
    if not log_path.exists():
        return
    with log_path.open() as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue
