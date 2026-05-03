"""Run the problem's eval and parse a normalized score.

Contract: eval writes a JSON file to a path the framework chooses.
Schema:
  {
    "score": 0.0..1.0,
    "per_fixture": { "<fixture-name>": 0.0..1.0, ... }   (optional)
  }
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path

from agentic_autoresearch.spec import EvalConfig


@dataclass(frozen=True)
class EvalResult:
    score: float | None
    per_fixture: dict[str, float]
    raw: dict
    stdout: str
    stderr: str
    exit_code: int


def default_eval_command(eval_cfg: EvalConfig, output_path: Path) -> str:
    if eval_cfg.command:
        return eval_cfg.command.replace("{output}", str(output_path))
    # default: `python <scorer> --json <output>`
    return f"python {eval_cfg.scorer} --json {output_path}"


def run_eval(repo: Path, eval_cfg: EvalConfig, output_path: Path, timeout: int = 600) -> EvalResult:
    """Run the eval as a subprocess in the problem repo."""
    cmd = default_eval_command(eval_cfg, output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    # Inherit env, then layer on macOS lib path so cairo (and similar) resolve.
    env = dict(os.environ)
    if "DYLD_FALLBACK_LIBRARY_PATH" not in env:
        env["DYLD_FALLBACK_LIBRARY_PATH"] = "/opt/homebrew/lib"
    # If the problem repo has a .venv, prepend its bin to PATH so its python
    # is the default and `python` resolves to the right interpreter.
    venv_bin = repo / ".venv" / "bin"
    if venv_bin.is_dir():
        env["PATH"] = f"{venv_bin}:{env.get('PATH', '')}"
        env["VIRTUAL_ENV"] = str(repo / ".venv")
    try:
        proc = subprocess.run(
            shlex.split(cmd),
            cwd=repo,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
            check=False,
        )
    except subprocess.TimeoutExpired as e:
        def _to_str(x) -> str:
            if not x:
                return ""
            if isinstance(x, bytes):
                return x.decode(errors="replace")
            return str(x)
        return EvalResult(
            score=None,
            per_fixture={},
            raw={},
            stdout=_to_str(e.stdout),
            stderr=_to_str(e.stderr) + f"\n[eval timed out after {timeout}s]",
            exit_code=124,
        )
    raw: dict = {}
    if output_path.exists():
        try:
            raw = json.loads(output_path.read_text())
        except json.JSONDecodeError:
            raw = {}
    score = raw.get("score")
    if isinstance(score, (int, float)):
        score = float(score)
    else:
        score = None
    per_fixture = raw.get("per_fixture") or {}
    if not isinstance(per_fixture, dict):
        per_fixture = {}
    return EvalResult(
        score=score,
        per_fixture={k: float(v) for k, v in per_fixture.items()},
        raw=raw,
        stdout=proc.stdout,
        stderr=proc.stderr,
        exit_code=proc.returncode,
    )
