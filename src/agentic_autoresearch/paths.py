"""Where framework state lives on disk.

The DB is the source of truth. Everything else is either ephemeral
(worktrees) or a sidecar (large blobs that sqlite is bad at storing).
"""

from __future__ import annotations

import os
from pathlib import Path

HOME_ENV = "AGENTIC_AUTORESEARCH_HOME"
DEFAULT_HOME = Path.home() / ".agentic-autoresearch"


def home() -> Path:
    p = Path(os.environ.get(HOME_ENV, DEFAULT_HOME))
    p.mkdir(parents=True, exist_ok=True)
    return p


def db_path() -> Path:
    return home() / "db.sqlite"


def worktrees_dir(problem_repo: Path) -> Path:
    """Worktrees live INSIDE the problem repo so git can find them,
    but in a gitignored directory."""
    d = problem_repo / ".agentic-autoresearch-worktrees"
    d.mkdir(parents=True, exist_ok=True)
    return d


def artifacts_dir(run_id: str) -> Path:
    """Sidecar dir for blobs too big for sqlite (agent stream-json, plots)."""
    d = home() / "artifacts" / run_id
    d.mkdir(parents=True, exist_ok=True)
    return d
