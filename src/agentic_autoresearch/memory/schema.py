"""sqlite + sqlite-vec schema. The DB is the source of truth.

Tables:
  runs                  one row per autoresearch run
  iterations            one row per Plan/Act/Reflect cycle
  artifacts             diffs, eval JSON, agent stream-json (path-pointer
                          for >1MB blobs)
  guardrail_violations  audit log
  processes             heartbeat-based registry (orchestrator + agents)

Virtual tables (sqlite-vec):
  iteration_hypothesis_embed  vec0  (id, embedding[1536])
  iteration_lesson_embed      vec0  (id, embedding[1536])
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path

import sqlite_vec

from agentic_autoresearch.paths import db_path

EMBED_DIM = 768  # gemini-embedding-001, output_dimensionality=768

SCHEMA_SQL = f"""
CREATE TABLE IF NOT EXISTS runs (
    id              TEXT PRIMARY KEY,
    problem_name    TEXT NOT NULL,
    problem_path    TEXT NOT NULL,
    started_at      TEXT NOT NULL,
    ended_at        TEXT,
    status          TEXT NOT NULL DEFAULT 'running',
    exit_reason     TEXT,
    final_score     REAL,
    total_cost_usd    REAL DEFAULT 0.0,
    gemini_cost_usd   REAL DEFAULT 0,
    claude_cost_usd   REAL DEFAULT 0,
    best_score_so_far REAL,
    best_commit       TEXT,
    plateau_count     INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS iterations (
    id                 TEXT PRIMARY KEY,
    run_id             TEXT NOT NULL REFERENCES runs(id),
    iter_num           INTEGER NOT NULL,
    parent_iter_id     TEXT REFERENCES iterations(id),
    phase              TEXT NOT NULL DEFAULT 'pending',
    category           TEXT,
    hypothesis         TEXT,
    files_touched      TEXT,
    commit_hash        TEXT,
    score_before       REAL,
    score_after        REAL,
    score_delta        REAL GENERATED ALWAYS AS (score_after - score_before) VIRTUAL,
    per_fixture_scores TEXT,
    duration_seconds   INTEGER,
    api_cost_usd       REAL,
    gemini_cost_usd    REAL DEFAULT 0,
    claude_cost_usd    REAL DEFAULT 0,
    confidence         REAL,
    kept               INTEGER,
    lesson             TEXT,
    proposed_next      TEXT,
    guardrail_violations INTEGER DEFAULT 0,
    superseded_by      TEXT,
    contradicted_by    TEXT,
    started_at         TEXT NOT NULL,
    phase_started_at   TEXT,
    ended_at           TEXT,
    UNIQUE (run_id, iter_num)
);

CREATE INDEX IF NOT EXISTS idx_iter_run ON iterations(run_id, iter_num);
CREATE INDEX IF NOT EXISTS idx_iter_phase ON iterations(phase);
CREATE INDEX IF NOT EXISTS idx_iter_kept ON iterations(run_id, kept);

CREATE TABLE IF NOT EXISTS artifacts (
    id            TEXT PRIMARY KEY,
    iter_id       TEXT NOT NULL REFERENCES iterations(id),
    kind          TEXT NOT NULL,
    path          TEXT,
    content       BLOB,
    content_hash  TEXT,
    size_bytes    INTEGER,
    created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_artifact_iter ON artifacts(iter_id);

CREATE TABLE IF NOT EXISTS guardrail_violations (
    id           TEXT PRIMARY KEY,
    iter_id      TEXT NOT NULL REFERENCES iterations(id),
    kind         TEXT NOT NULL,
    detail       TEXT,
    action_taken TEXT,
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS processes (
    pid                INTEGER PRIMARY KEY,
    role               TEXT NOT NULL,
    run_id             TEXT REFERENCES runs(id),
    iter_id            TEXT REFERENCES iterations(id),
    host               TEXT NOT NULL,
    started_at         TEXT NOT NULL,
    last_heartbeat_at  TEXT NOT NULL,
    ended_at           TEXT
);

CREATE VIRTUAL TABLE IF NOT EXISTS iteration_hypothesis_embed
USING vec0(id TEXT PRIMARY KEY, embedding FLOAT[{EMBED_DIM}]);

CREATE VIRTUAL TABLE IF NOT EXISTS iteration_lesson_embed
USING vec0(id TEXT PRIMARY KEY, embedding FLOAT[{EMBED_DIM}]);
"""


def connect(path: Path | None = None) -> sqlite3.Connection:
    """Open the framework DB with sqlite-vec loaded."""
    p = path or db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p, isolation_level=None)  # autocommit; we use BEGIN explicitly
    conn.row_factory = sqlite3.Row
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(path: Path | None = None) -> Path:
    """Idempotent — safe to call on every framework start."""
    p = path or db_path()
    conn = connect(p)
    try:
        for stmt in SCHEMA_SQL.strip().split(";"):
            s = stmt.strip()
            if s:
                conn.execute(s)
        # Idempotent ALTERs for columns added after initial release
        for col, ddl in [
            ("gemini_cost_usd", "ALTER TABLE iterations ADD COLUMN gemini_cost_usd REAL DEFAULT 0"),
            ("claude_cost_usd", "ALTER TABLE iterations ADD COLUMN claude_cost_usd REAL DEFAULT 0"),
            ("confidence",      "ALTER TABLE iterations ADD COLUMN confidence REAL"),
            ("gemini_cost_usd_runs", "ALTER TABLE runs ADD COLUMN gemini_cost_usd REAL DEFAULT 0"),
            ("claude_cost_usd_runs", "ALTER TABLE runs ADD COLUMN claude_cost_usd REAL DEFAULT 0"),
            ("best_score_so_far",    "ALTER TABLE runs ADD COLUMN best_score_so_far REAL"),
            ("best_commit",          "ALTER TABLE runs ADD COLUMN best_commit TEXT"),
            ("plateau_count",        "ALTER TABLE runs ADD COLUMN plateau_count INTEGER DEFAULT 0"),
        ]:
            try:
                conn.execute(ddl)
            except Exception:
                pass  # column already exists
    finally:
        conn.close()
    return p


@contextmanager
def db():
    """Context manager — short-lived connection per call."""
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()
