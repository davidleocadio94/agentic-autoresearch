"""Pre/post-iter guardrail enforcement.

Reads constraints from the parsed ProblemSpec and runs deterministic
checks. NO agent involved. Violations cause the orchestrator to mark
the iter as failed_guardrail and discard the worktree.
"""

from __future__ import annotations

import fnmatch
import hashlib
import subprocess
from dataclasses import dataclass
from pathlib import Path

from agentic_autoresearch.spec import Constraints


@dataclass(frozen=True)
class Violation:
    kind: str
    detail: str


def repo_size_bytes(repo: Path) -> int:
    total = 0
    for p in repo.rglob("*"):
        if not p.is_file():
            continue
        if any(part in {".git", ".venv", ".agentic-autoresearch-worktrees"} for part in p.parts):
            continue
        try:
            total += p.stat().st_size
        except OSError:
            pass
    return total


def hash_immutable(repo: Path, patterns: tuple[str, ...]) -> dict[str, str]:
    """Snapshot SHA-256 of every path matching the immutable_paths globs."""
    out: dict[str, str] = {}
    for pat in patterns:
        for p in repo.glob(pat):
            if not p.is_file():
                continue
            rel = p.relative_to(repo).as_posix()
            out[rel] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


def detect_immutable_drift(
    repo: Path, before: dict[str, str], patterns: tuple[str, ...]
) -> list[Violation]:
    after = hash_immutable(repo, patterns)
    violations: list[Violation] = []
    for path, h in before.items():
        if path not in after:
            violations.append(Violation("immutable_deleted", path))
        elif after[path] != h:
            violations.append(Violation("immutable_modified", path))
    for path in after:
        if path not in before and any(fnmatch.fnmatch(path, p) for p in patterns):
            # New file under an immutable glob — also a drift
            violations.append(Violation("immutable_added", path))
    return violations


def check_repo_size(repo: Path, max_bytes: int) -> Violation | None:
    sz = repo_size_bytes(repo)
    if sz > max_bytes:
        return Violation("repo_too_large", f"{sz} > {max_bytes}")
    return None


def check_forbidden_deps(repo: Path, forbidden: tuple[str, ...]) -> list[Violation]:
    """Look for forbidden packages in pyproject.toml / requirements.txt."""
    out: list[Violation] = []
    if not forbidden:
        return out
    candidates = [repo / "pyproject.toml", repo / "requirements.txt"]
    for cf in candidates:
        if not cf.exists():
            continue
        text = cf.read_text().lower()
        for dep in forbidden:
            if dep.lower() in text:
                out.append(Violation("forbidden_dep", f"{dep} in {cf.name}"))
    return out


def enforce_post_iter(
    repo: Path,
    constraints: Constraints,
    immutable_before: dict[str, str],
) -> list[Violation]:
    """Run all post-iter checks. Returns list of violations (empty if clean)."""
    violations: list[Violation] = []
    violations.extend(detect_immutable_drift(repo, immutable_before, constraints.immutable_paths))
    if v := check_repo_size(repo, constraints.max_repo_bytes):
        violations.append(v)
    violations.extend(check_forbidden_deps(repo, constraints.forbidden_deps))
    return violations


def git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=check
    )
