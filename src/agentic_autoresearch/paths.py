"""Where framework state lives on disk.

The DB is the source of truth. Everything else is either ephemeral
(worktrees) or a sidecar (large blobs that sqlite is bad at storing).

Per-project namespacing:
  ~/.agentic-autoresearch/
    credentials.toml          (chmod 600, never copied)
    projects/<project>/
      db.sqlite               # world model + iterations for ONE problem
      iters/iter_NNN/         # ~150KB/iter: winner.webp + scores.json
      pods/active.json        # live pod manifests
    artifacts/<run_id>/       # legacy sidecar; new code uses iters/

Old layout (~/.aar/db.sqlite, flat) is migrated automatically on first
use of any project-aware path. The old DB becomes
~/.aar/projects/omr/db.sqlite (OMR was the only pre-namespace user).
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

HOME_ENV = "AGENTIC_AUTORESEARCH_HOME"
DEFAULT_HOME = Path.home() / ".agentic-autoresearch"
LEGACY_PROJECT = "omr"  # everything in the old flat DB was OMR


def home() -> Path:
    p = Path(os.environ.get(HOME_ENV, DEFAULT_HOME))
    p.mkdir(parents=True, exist_ok=True)
    return p


def projects_dir() -> Path:
    d = home() / "projects"
    d.mkdir(parents=True, exist_ok=True)
    return d


def project_dir(project: str) -> Path:
    d = projects_dir() / project
    d.mkdir(parents=True, exist_ok=True)
    return d


def db_path(project: str | None = None) -> Path:
    """Per-project DB path. If project is None, falls back to the legacy
    flat path during migration — callers should pass a project name."""
    if project is None:
        # Backward-compat for code that hasn't been threaded with a project
        # yet. Triggers migration on first call.
        _migrate_legacy_if_needed()
        return projects_dir() / LEGACY_PROJECT / "db.sqlite"
    return project_dir(project) / "db.sqlite"


def iters_dir(project: str) -> Path:
    """Where per-iter winner.webp + scores.json + workflow.json live."""
    d = project_dir(project) / "iters"
    d.mkdir(parents=True, exist_ok=True)
    return d


def iter_dir(project: str, iter_num: int) -> Path:
    d = iters_dir(project) / f"iter_{iter_num:04d}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def pods_file(project: str) -> Path:
    """Live pod manifests (JSON). Used for leak detection on startup."""
    return project_dir(project) / "pods" / "active.json"


def worktrees_dir(problem_repo: Path) -> Path:
    """Worktrees live INSIDE the problem repo so git can find them,
    but in a gitignored directory."""
    d = problem_repo / ".agentic-autoresearch-worktrees"
    d.mkdir(parents=True, exist_ok=True)
    return d


def artifacts_dir(run_id: str) -> Path:
    """Sidecar dir for blobs too big for sqlite (agent stream-json, plots).
    Legacy callers — new code should use iter_dir(project, iter_num)."""
    d = home() / "artifacts" / run_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def _migrate_legacy_if_needed() -> None:
    """One-shot migration: ~/.aar/db.sqlite -> ~/.aar/projects/omr/db.sqlite.

    Idempotent. Doesn't delete the source until the target exists and
    sqlite_master row counts match.
    """
    legacy_db = home() / "db.sqlite"
    if not legacy_db.exists():
        return
    target_dir = projects_dir() / LEGACY_PROJECT
    target_db = target_dir / "db.sqlite"
    if target_db.exists():
        # Already migrated. Caller might have manually moved or re-run.
        return
    target_dir.mkdir(parents=True, exist_ok=True)
    # copy first, verify, then move WAL/SHM and remove legacy
    shutil.copy2(legacy_db, target_db)
    for suffix in ("-wal", "-shm"):
        legacy_side = home() / f"db.sqlite{suffix}"
        if legacy_side.exists():
            shutil.copy2(legacy_side, target_dir / f"db.sqlite{suffix}")
    # Verify table presence as a sanity check before retiring source
    import sqlite3

    src_tables = _table_set(sqlite3.connect(legacy_db))
    dst_tables = _table_set(sqlite3.connect(target_db))
    if src_tables != dst_tables:
        raise RuntimeError(
            f"migration verification failed: legacy tables {src_tables - dst_tables} "
            f"missing from {target_db}"
        )
    # Rename legacy to .pre-migration so it's recoverable but won't be re-read
    legacy_db.rename(home() / "db.sqlite.pre-migration")
    for suffix in ("-wal", "-shm"):
        legacy_side = home() / f"db.sqlite{suffix}"
        if legacy_side.exists():
            legacy_side.rename(home() / f"db.sqlite{suffix}.pre-migration")


def _table_set(conn) -> set[str]:
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
        return {r[0] for r in rows}
    finally:
        conn.close()
