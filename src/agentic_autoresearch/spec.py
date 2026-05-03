"""Parse a problem's spec.md into a typed ProblemSpec.

The spec is the only thing the user must write. Everything else
(orchestrator, memory, dashboard) is framework-provided.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass(frozen=True)
class ExitConditions:
    target_score: float = 1.0
    max_hours: float = 24.0
    max_iters: int = 200
    no_progress_iters: int = 10  # bail if N iters without improvement
    max_cost_usd: float = 50.0   # hard cap on cumulative API spend
    plateau_stop_after: int = 15 # end run when no new best for this many iters


@dataclass(frozen=True)
class Constraints:
    language: str = "python"
    manager: str = "uv"
    forbid_gui: bool = True
    max_repo_bytes: int = 2_000_000_000
    forbidden_deps: tuple[str, ...] = ()
    immutable_paths: tuple[str, ...] = ()
    allowed_extra_deps: tuple[str, ...] = ()


@dataclass(frozen=True)
class EvalConfig:
    kind: str = "custom"  # or a built-in template name
    fixtures_glob: str | None = None
    scorer: str = "eval/score.py"
    command: str | None = None  # optional shell command override


@dataclass(frozen=True)
class ProblemSpec:
    name: str
    repo_path: Path
    exit: ExitConditions
    constraints: Constraints
    eval: EvalConfig
    body: str  # the markdown after front-matter, passed to PLANNER as context
    raw_front_matter: dict = field(default_factory=dict)


def parse_spec(spec_path: Path) -> ProblemSpec:
    """Load spec.md from disk."""
    spec_path = Path(spec_path).resolve()
    if not spec_path.exists():
        raise FileNotFoundError(f"spec.md not found at {spec_path}")
    text = spec_path.read_text()
    front, body = _split_front_matter(text)
    fm = yaml.safe_load(front) if front else {}
    if not isinstance(fm, dict):
        raise ValueError(f"front-matter must be a YAML mapping; got {type(fm).__name__}")

    name = fm.get("name") or spec_path.parent.name
    return ProblemSpec(
        name=name,
        repo_path=spec_path.parent,
        exit=_exit_from(fm.get("exit") or {}),
        constraints=_constraints_from(fm.get("constraints") or {}),
        eval=_eval_from(fm.get("eval") or {}),
        body=body,
        raw_front_matter=fm,
    )


def _split_front_matter(text: str) -> tuple[str, str]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return "", text
    end = None
    for i, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            end = i
            break
    if end is None:
        return "", text
    front = "\n".join(lines[1:end])
    body = "\n".join(lines[end + 1 :]).lstrip("\n")
    return front, body


def _exit_from(d: dict) -> ExitConditions:
    return ExitConditions(
        target_score=float(d.get("target_score", 1.0)),
        max_hours=float(d.get("max_hours", 24.0)),
        max_iters=int(d.get("max_iters", 200)),
        no_progress_iters=int(d.get("no_progress_iters", 10)),
        max_cost_usd=float(d.get("max_cost_usd", 50.0)),
        plateau_stop_after=int(d.get("plateau_stop_after", 15)),
    )


def _constraints_from(d: dict) -> Constraints:
    return Constraints(
        language=str(d.get("language", "python")),
        manager=str(d.get("manager", "uv")),
        forbid_gui=bool(d.get("forbid_gui", True)),
        max_repo_bytes=int(d.get("max_repo_bytes", 2_000_000_000)),
        forbidden_deps=tuple(d.get("forbidden_deps", []) or []),
        immutable_paths=tuple(d.get("immutable_paths", []) or []),
        allowed_extra_deps=tuple(d.get("allowed_extra_deps", []) or []),
    )


def _eval_from(d: dict) -> EvalConfig:
    return EvalConfig(
        kind=str(d.get("kind", "custom")),
        fixtures_glob=d.get("fixtures_glob"),
        scorer=str(d.get("scorer", "eval/score.py")),
        command=d.get("command"),
    )
