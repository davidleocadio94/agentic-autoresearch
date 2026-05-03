"""CRUD on the memory DB. Pure-data, no agent/orchestrator logic here."""

from __future__ import annotations

import json
import socket
import sqlite3
import struct
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from contextlib import contextmanager

from agentic_autoresearch.memory.schema import connect


@contextmanager
def _conn():
    """Short-lived connection that always closes."""
    c = connect()
    try:
        yield c
    finally:
        c.close()


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_id() -> str:
    return str(uuid.uuid4())


# --------------------------------------------------------------------------- runs


def create_run(problem_name: str, problem_path: Path) -> str:
    rid = new_id()
    with _conn() as conn:
        conn.execute(
            "INSERT INTO runs (id, problem_name, problem_path, started_at, status) "
            "VALUES (?, ?, ?, ?, 'running')",
            (rid, problem_name, str(problem_path), now()),
        )
    return rid


def end_run(run_id: str, *, exit_reason: str, final_score: float | None) -> None:
    with _conn() as conn:
        # Roll up cumulative cost from iters into the run row for dashboard.
        row = conn.execute(
            "SELECT COALESCE(SUM(api_cost_usd), 0) AS total, "
            "COALESCE(SUM(gemini_cost_usd), 0) AS gemini, "
            "COALESCE(SUM(claude_cost_usd), 0) AS claude "
            "FROM iterations WHERE run_id=?",
            (run_id,),
        ).fetchone()
        conn.execute(
            "UPDATE runs SET ended_at=?, status='done', exit_reason=?, final_score=?, "
            "total_cost_usd=?, gemini_cost_usd=?, claude_cost_usd=? WHERE id=?",
            (
                now(),
                exit_reason,
                final_score,
                float(row["total"] or 0),
                float(row["gemini"] or 0),
                float(row["claude"] or 0),
                run_id,
            ),
        )


def update_best(run_id: str, *, score: float, commit: str | None) -> tuple[float | None, str | None, int]:
    """If `score` beats the run's best_score_so_far, update best + reset
    plateau_count. Otherwise increment plateau_count. Returns the
    (best_score_so_far, best_commit, plateau_count) AFTER the update.
    """
    with _conn() as conn:
        row = conn.execute(
            "SELECT best_score_so_far, best_commit, plateau_count FROM runs WHERE id=?",
            (run_id,),
        ).fetchone()
        prev_best = row["best_score_so_far"] if row else None
        if prev_best is None or score > prev_best:
            conn.execute(
                "UPDATE runs SET best_score_so_far=?, best_commit=?, plateau_count=0 WHERE id=?",
                (score, commit, run_id),
            )
            return score, commit, 0
        new_plateau = (row["plateau_count"] or 0) + 1
        conn.execute(
            "UPDATE runs SET plateau_count=? WHERE id=?",
            (new_plateau, run_id),
        )
        return prev_best, row["best_commit"], new_plateau


def get_run_best(run_id: str) -> tuple[float | None, str | None, int]:
    with _conn() as conn:
        row = conn.execute(
            "SELECT best_score_so_far, best_commit, plateau_count FROM runs WHERE id=?",
            (run_id,),
        ).fetchone()
    if not row:
        return None, None, 0
    return row["best_score_so_far"], row["best_commit"], (row["plateau_count"] or 0)


def absolute_deltas(run_id: str, limit: int = 20) -> list[float]:
    """Return |score_after - score_before| for the most recent `limit`
    iters in this run that have both values. Used to compute sigma."""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT score_before, score_after FROM iterations "
            "WHERE run_id=? AND score_after IS NOT NULL AND score_before IS NOT NULL "
            "ORDER BY iter_num DESC LIMIT ?",
            (run_id, limit),
        ).fetchall()
    return [abs((r["score_after"] or 0) - (r["score_before"] or 0)) for r in rows]


def update_run_cost(run_id: str) -> None:
    """Update the run's cost rollups for in-flight runs (live dashboard)."""
    with _conn() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(api_cost_usd), 0) AS total, "
            "COALESCE(SUM(gemini_cost_usd), 0) AS gemini, "
            "COALESCE(SUM(claude_cost_usd), 0) AS claude "
            "FROM iterations WHERE run_id=?",
            (run_id,),
        ).fetchone()
        conn.execute(
            "UPDATE runs SET total_cost_usd=?, gemini_cost_usd=?, claude_cost_usd=? WHERE id=?",
            (float(row["total"] or 0), float(row["gemini"] or 0), float(row["claude"] or 0), run_id),
        )


def latest_run(problem_name: str | None = None) -> sqlite3.Row | None:
    with _conn() as conn:
        if problem_name:
            cur = conn.execute(
                "SELECT * FROM runs WHERE problem_name=? ORDER BY started_at DESC LIMIT 1",
                (problem_name,),
            )
        else:
            cur = conn.execute("SELECT * FROM runs ORDER BY started_at DESC LIMIT 1")
        return cur.fetchone()


# --------------------------------------------------------------------------- iterations


@dataclass
class IterationDraft:
    run_id: str
    iter_num: int
    parent_iter_id: str | None
    score_before: float


def create_iteration(d: IterationDraft) -> str:
    iid = new_id()
    t = now()
    with _conn() as conn:
        conn.execute(
            "INSERT INTO iterations (id, run_id, iter_num, parent_iter_id, phase, "
            "score_before, started_at, phase_started_at) "
            "VALUES (?, ?, ?, ?, 'pending', ?, ?, ?)",
            (iid, d.run_id, d.iter_num, d.parent_iter_id, d.score_before, t, t),
        )
    return iid


def set_phase(iter_id: str, phase: str) -> None:
    with _conn() as conn:
        conn.execute(
            "UPDATE iterations SET phase=?, phase_started_at=? WHERE id=?",
            (phase, now(), iter_id),
        )


def update_iteration(iter_id: str, **fields: Any) -> None:
    if not fields:
        return
    cols = ", ".join(f"{k}=?" for k in fields)
    vals = list(fields.values())
    with _conn() as conn:
        conn.execute(f"UPDATE iterations SET {cols} WHERE id=?", (*vals, iter_id))


def finalize_iteration(
    iter_id: str,
    *,
    phase: str,
    kept: bool | None,
    score_after: float | None,
    per_fixture_scores: dict | None,
    lesson: str | None,
    proposed_next: str | None,
    commit_hash: str | None,
    duration_seconds: int,
) -> None:
    with _conn() as conn:
        conn.execute(
            "UPDATE iterations SET phase=?, kept=?, score_after=?, per_fixture_scores=?, "
            "lesson=?, proposed_next=?, commit_hash=?, duration_seconds=?, ended_at=? "
            "WHERE id=?",
            (
                phase,
                None if kept is None else int(kept),
                score_after,
                json.dumps(per_fixture_scores) if per_fixture_scores else None,
                lesson,
                proposed_next,
                commit_hash,
                duration_seconds,
                now(),
                iter_id,
            ),
        )


def last_iter_num(run_id: str) -> int:
    with _conn() as conn:
        cur = conn.execute(
            "SELECT COALESCE(MAX(iter_num), 0) AS n FROM iterations WHERE run_id=?",
            (run_id,),
        )
        return cur.fetchone()["n"]


def recent_iterations(run_id: str, limit: int = 5) -> list[sqlite3.Row]:
    with _conn() as conn:
        cur = conn.execute(
            "SELECT * FROM iterations WHERE run_id=? ORDER BY iter_num DESC LIMIT ?",
            (run_id, limit),
        )
        return list(cur.fetchall())


def iterations_for_run(run_id: str) -> list[sqlite3.Row]:
    with _conn() as conn:
        cur = conn.execute(
            "SELECT * FROM iterations WHERE run_id=? ORDER BY iter_num ASC",
            (run_id,),
        )
        return list(cur.fetchall())


def open_phase_iters() -> list[sqlite3.Row]:
    with _conn() as conn:
        cur = conn.execute(
            "SELECT * FROM iterations WHERE phase IN ('pending','planning','acting','reflecting')"
        )
        return list(cur.fetchall())


# --------------------------------------------------------------------------- artifacts


def add_artifact(
    iter_id: str,
    kind: str,
    *,
    path: Path | None = None,
    content: bytes | None = None,
    content_hash: str | None = None,
) -> str:
    aid = new_id()
    size = (path.stat().st_size if path and path.exists() else (len(content) if content else 0))
    with _conn() as conn:
        conn.execute(
            "INSERT INTO artifacts (id, iter_id, kind, path, content, content_hash, "
            "size_bytes, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (aid, iter_id, kind, str(path) if path else None, content, content_hash, size, now()),
        )
    return aid


# --------------------------------------------------------------------------- vectors


def _vec_blob(vec: Iterable[float]) -> bytes:
    arr = list(vec)
    return struct.pack(f"{len(arr)}f", *arr)


def store_hypothesis_embed(iter_id: str, vec: Iterable[float]) -> None:
    with _conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO iteration_hypothesis_embed (id, embedding) VALUES (?, ?)",
            (iter_id, _vec_blob(vec)),
        )


def store_lesson_embed(iter_id: str, vec: Iterable[float]) -> None:
    with _conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO iteration_lesson_embed (id, embedding) VALUES (?, ?)",
            (iter_id, _vec_blob(vec)),
        )


def search_similar(table: str, vec: Iterable[float], k: int = 10) -> list[sqlite3.Row]:
    """KNN search; table ∈ {iteration_hypothesis_embed, iteration_lesson_embed}.

    Returns iteration rows (joined) with `distance` column.
    """
    if table not in ("iteration_hypothesis_embed", "iteration_lesson_embed"):
        raise ValueError(f"unknown vec table {table!r}")
    blob = _vec_blob(vec)
    with _conn() as conn:
        cur = conn.execute(
            f"""
            SELECT i.*, e.distance AS distance
            FROM {table} e
            JOIN iterations i ON i.id = e.id
            WHERE e.embedding MATCH ? AND k = ?
            ORDER BY e.distance ASC
            """,
            (blob, k),
        )
        return list(cur.fetchall())


# --------------------------------------------------------------------------- processes


def register_process(pid: int, role: str, run_id: str | None, iter_id: str | None) -> None:
    t = now()
    with _conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO processes (pid, role, run_id, iter_id, host, started_at, "
            "last_heartbeat_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (pid, role, run_id, iter_id, socket.gethostname(), t, t),
        )


def heartbeat(pid: int) -> None:
    with _conn() as conn:
        conn.execute(
            "UPDATE processes SET last_heartbeat_at=? WHERE pid=?", (now(), pid)
        )


def end_process(pid: int) -> None:
    with _conn() as conn:
        conn.execute("UPDATE processes SET ended_at=? WHERE pid=?", (now(), pid))


def alive_processes(role: str | None = None) -> list[sqlite3.Row]:
    with _conn() as conn:
        if role:
            cur = conn.execute(
                "SELECT * FROM processes WHERE ended_at IS NULL AND role=?", (role,)
            )
        else:
            cur = conn.execute("SELECT * FROM processes WHERE ended_at IS NULL")
        return list(cur.fetchall())
