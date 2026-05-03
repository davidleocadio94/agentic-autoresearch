"""The 8 PLANNER intuition queries.

Each function answers a specific question the PLANNER might ask while
deciding what to try next. They return formatted markdown strings ready
to splice into the PLANNER prompt — the agent doesn't query the DB
itself; the orchestrator pre-builds the context.

Queries:
  1. recent       — what just happened?
  2. similar      — past iters near $draft (vector)
  3. failure_mode — what does memory know about $failure (vector over lessons)
  4. category     — are we plateauing in any category?
  5. winners      — what HAS worked?
  6. open_hyp     — what was proposed but never tried?
  7. error_curve  — which fixture got worse since iter K?
  8. cross_run    — past kept iters in any prior run (cross-problem v2)
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable

from agentic_autoresearch.memory.schema import connect


# --------------------------------------------------------------------------- 1. recent


def recent(run_id: str, limit: int = 5) -> list[sqlite3.Row]:
    with _conn() as c:
        return list(
            c.execute(
                "SELECT * FROM iterations WHERE run_id=? ORDER BY iter_num DESC LIMIT ?",
                (run_id, limit),
            ).fetchall()
        )


# --------------------------------------------------------------------------- 2/3. vector


def similar_hypotheses(vec: Iterable[float], k: int = 10) -> list[sqlite3.Row]:
    return _vec_search("iteration_hypothesis_embed", vec, k)


def similar_lessons(vec: Iterable[float], k: int = 10) -> list[sqlite3.Row]:
    return _vec_search("iteration_lesson_embed", vec, k)


def _vec_search(table: str, vec: Iterable[float], k: int) -> list[sqlite3.Row]:
    import struct

    arr = list(vec)
    if not any(arr):
        return []
    blob = struct.pack(f"{len(arr)}f", *arr)
    with _conn() as c:
        try:
            return list(
                c.execute(
                    f"""
                    SELECT i.*, e.distance AS distance
                    FROM {table} e
                    JOIN iterations i ON i.id = e.id
                    WHERE e.embedding MATCH ? AND k = ?
                    ORDER BY e.distance ASC
                    """,
                    (blob, k),
                ).fetchall()
            )
        except sqlite3.OperationalError:
            return []


# --------------------------------------------------------------------------- 4. category


def category_stats(run_id: str) -> list[sqlite3.Row]:
    with _conn() as c:
        return list(
            c.execute(
                """
                SELECT
                    category,
                    COUNT(*) AS attempts,
                    SUM(CASE WHEN kept=1 THEN 1 ELSE 0 END) AS wins,
                    AVG(score_after - score_before) AS avg_delta
                FROM iterations
                WHERE run_id=? AND category IS NOT NULL AND score_after IS NOT NULL
                GROUP BY category
                ORDER BY avg_delta DESC
                """,
                (run_id,),
            ).fetchall()
        )


# --------------------------------------------------------------------------- 5. winners


def winners(run_id: str) -> list[sqlite3.Row]:
    """Returns full iter rows so they're compatible with _fmt_iter_one."""
    with _conn() as c:
        return list(
            c.execute(
                "SELECT * FROM iterations WHERE run_id=? AND kept=1 "
                "ORDER BY (score_after - score_before) DESC",
                (run_id,),
            ).fetchall()
        )


def high_confidence_wins(run_id: str, threshold: float = 1.0, limit: int = 5) -> list[sqlite3.Row]:
    """Iters whose |delta|/sigma > threshold AND score went up.
    These are the trustworthy experiments the planner should reason from."""
    with _conn() as c:
        return list(
            c.execute(
                "SELECT * FROM iterations "
                "WHERE run_id=? AND confidence > ? "
                "AND score_after IS NOT NULL AND score_before IS NOT NULL "
                "AND score_after > score_before "
                "ORDER BY score_after DESC, confidence DESC LIMIT ?",
                (run_id, threshold, limit),
            ).fetchall()
        )


def low_confidence_wins(run_id: str, threshold: float = 1.0, limit: int = 5) -> list[sqlite3.Row]:
    """Iters that improved but with low confidence. Suggestive but
    might be noise — surface as contrast so the planner can weight
    them modestly."""
    with _conn() as c:
        return list(
            c.execute(
                "SELECT * FROM iterations "
                "WHERE run_id=? AND confidence IS NOT NULL AND confidence <= ? "
                "AND score_after IS NOT NULL AND score_before IS NOT NULL "
                "AND score_after > score_before "
                "ORDER BY score_after DESC LIMIT ?",
                (run_id, threshold, limit),
            ).fetchall()
        )


def recent_confidences(run_id: str, limit: int = 10) -> list[float]:
    """Last N iter confidences (most recent first). For the noise-floor
    section of the planner prompt."""
    with _conn() as c:
        rows = c.execute(
            "SELECT confidence FROM iterations "
            "WHERE run_id=? AND confidence IS NOT NULL "
            "ORDER BY iter_num DESC LIMIT ?",
            (run_id, limit),
        ).fetchall()
    return [float(r["confidence"]) for r in rows]


def current_sigma(run_id: str, window: int = 20) -> float | None:
    """Same sigma the loop computes for confidence — exposed here so
    the planner prompt can show it."""
    import statistics
    with _conn() as c:
        rows = c.execute(
            "SELECT score_before, score_after FROM iterations "
            "WHERE run_id=? AND score_after IS NOT NULL AND score_before IS NOT NULL "
            "ORDER BY iter_num DESC LIMIT ?",
            (run_id, window),
        ).fetchall()
    deltas = [abs((r["score_after"] or 0) - (r["score_before"] or 0)) for r in rows]
    if len(deltas) < 2:
        return None
    return statistics.stdev(deltas)


def best_so_far(run_id: str) -> float | None:
    with _conn() as c:
        row = c.execute(
            "SELECT best_score_so_far FROM runs WHERE id=?", (run_id,)
        ).fetchone()
    if not row:
        return None
    return row["best_score_so_far"]


# --------------------------------------------------------------------------- 6. open hypotheses


def open_hypotheses(run_id: str) -> list[sqlite3.Row]:
    with _conn() as c:
        return list(
            c.execute(
                "SELECT iter_num, proposed_next FROM iterations "
                "WHERE run_id=? AND proposed_next IS NOT NULL AND TRIM(proposed_next) != '' "
                "ORDER BY iter_num DESC LIMIT 20",
                (run_id,),
            ).fetchall()
        )


# --------------------------------------------------------------------------- 7. error curve


def fixture_regression(run_id: str) -> dict[str, list[tuple[int, float]]]:
    """Per-fixture score over iters. Returns {fixture: [(iter_num, score), ...]}."""
    with _conn() as c:
        rows = c.execute(
            "SELECT iter_num, per_fixture_scores FROM iterations "
            "WHERE run_id=? AND per_fixture_scores IS NOT NULL ORDER BY iter_num ASC",
            (run_id,),
        ).fetchall()
    out: dict[str, list[tuple[int, float]]] = {}
    for r in rows:
        try:
            scores = json.loads(r["per_fixture_scores"])
        except (TypeError, json.JSONDecodeError):
            continue
        for fid, s in scores.items():
            out.setdefault(fid, []).append((r["iter_num"], float(s)))
    return out


def stuck_fixtures(run_id: str) -> list[str]:
    """Fixtures that have never had score > 0."""
    curves = fixture_regression(run_id)
    return [fid for fid, hist in curves.items() if all(s == 0 for _, s in hist)]


# --------------------------------------------------------------------------- 8. cross-run


def cross_run_winners(this_run_id: str, limit: int = 10) -> list[sqlite3.Row]:
    with _conn() as c:
        return list(
            c.execute(
                "SELECT iter_num, category, hypothesis, lesson, run_id "
                "FROM iterations WHERE run_id != ? AND kept=1 "
                "ORDER BY (score_after - score_before) DESC LIMIT ?",
                (this_run_id, limit),
            ).fetchall()
        )


# --------------------------------------------------------------------------- helpers


from contextlib import contextmanager


@contextmanager
def _conn():
    c = connect()
    try:
        yield c
    finally:
        c.close()


# --------------------------------------------------------------------------- formatting


def format_for_prompt(run_id: str, draft_rationale: str = "") -> dict[str, str]:
    """Pre-build all 8 markdown sections for the PLANNER prompt."""
    from agentic_autoresearch.memory.embeddings import embed, is_zero

    sections: dict[str, str] = {}

    rec = recent(run_id, limit=5)
    sections["recent"] = _fmt_iters(rec) if rec else "(no prior iters)"

    sim_lines: list[str] = []
    if draft_rationale.strip():
        v = embed(draft_rationale)
        if not is_zero(v):
            for r in similar_hypotheses(v, k=5):
                sim_lines.append(_fmt_iter_one(r) + f"  [d={r['distance']:.3f}]")
    sections["similar"] = "\n".join(sim_lines) if sim_lines else "(no similar past hypotheses)"

    cat = category_stats(run_id)
    if cat:
        sections["category"] = "\n".join(
            f"  [{r['category']:>10}] attempts={r['attempts']} wins={r['wins']} avg_delta={r['avg_delta']:+.3f}"
            for r in cat
        )
    else:
        sections["category"] = "(no category data yet)"

    win = winners(run_id)
    sections["winners"] = (
        "\n".join(_fmt_iter_one(r) for r in win[:10])
        if win
        else "(nothing kept yet)"
    )

    op = open_hypotheses(run_id)
    sections["open_hyp"] = (
        "\n".join(f"  - from iter {r['iter_num']}: {r['proposed_next']}" for r in op)
        if op
        else "(none)"
    )

    stuck = stuck_fixtures(run_id)
    sections["error_curve"] = (
        "stuck-at-zero fixtures: " + ", ".join(stuck) if stuck else "(no stuck fixtures)"
    )

    cross = cross_run_winners(run_id, limit=5)
    sections["cross_run"] = (
        "\n".join(
            f"  - run {r['run_id'][:8]} iter {r['iter_num']} [{r['category']}]: {(r['lesson'] or '')[:160]}"
            for r in cross
        )
        if cross
        else "(no prior runs)"
    )

    # ----- Bug A: confidence-based sections
    high_conf = high_confidence_wins(run_id, threshold=1.0, limit=5)
    sections["high_conf_wins"] = (
        "\n".join(_fmt_high_conf(r) for r in high_conf)
        if high_conf
        else "(none yet — high-conf wins require |delta|/sigma > 1.0)"
    )

    low_conf = low_confidence_wins(run_id, threshold=1.0, limit=5)
    sections["low_conf_wins"] = (
        "\n".join(_fmt_low_conf(r) for r in low_conf)
        if low_conf
        else "(none — no improvements with low confidence so far)"
    )

    sigma = current_sigma(run_id)
    confs = recent_confidences(run_id, limit=10)
    best = best_so_far(run_id)
    nf_lines = []
    if sigma is None:
        nf_lines.append("sigma: not yet computable (need ≥2 prior iters)")
    else:
        nf_lines.append(f"sigma (rolling 20-iter window): {sigma:.3f}")
    if confs:
        nf_lines.append(
            "last 10 iter confidences: ["
            + ", ".join(f"{c:.2f}" if c != float("inf") else "inf" for c in confs)
            + "]"
        )
    if best is not None:
        nf_lines.append(f"best_score_so_far in this run: {best:.3f}")
    sections["noise_floor"] = "\n".join(nf_lines) if nf_lines else "(insufficient data)"

    return sections


def _fmt_high_conf(r: sqlite3.Row) -> str:
    """Rich format with hypothesis + outcome + lesson for the planner
    to reason over."""
    iter_num = r["iter_num"]
    score_b = r["score_before"] or 0
    score_a = r["score_after"]
    cat = r["category"] or "?"
    conf = r["confidence"]
    conf_str = "n/a" if conf is None else (f"{conf:.2f}" if conf != float("inf") else "inf")
    hyp = (r["hypothesis"] or "").replace("\n", " ")[:280]
    lesson = (r["lesson"] or "(no lesson)").replace("\n", " ")[:280]
    return (
        f"\n"
        f"  iter {iter_num:>3} · score {score_a:.3f} · conf {conf_str} · category: {cat}\n"
        f"      hypothesis: {hyp}\n"
        f"      outcome:    score went {score_b:.3f} → {score_a:.3f}\n"
        f"      lesson:     {lesson}"
    )


def _fmt_low_conf(r: sqlite3.Row) -> str:
    iter_num = r["iter_num"]
    score_a = r["score_after"]
    cat = r["category"] or "?"
    conf = r["confidence"]
    conf_str = "n/a" if conf is None else f"{conf:.2f}"
    hyp = (r["hypothesis"] or "").replace("\n", " ")[:200]
    return (
        f"  iter {iter_num:>3} · score {score_a:.3f} · conf {conf_str} · {cat}\n"
        f"      {hyp}"
    )


def _fmt_iters(rows: Iterable[sqlite3.Row]) -> str:
    return "\n".join(_fmt_iter_one(r) for r in rows)


def _fmt_iter_one(r: sqlite3.Row) -> str:
    """Defensive: handles partial-column rows by falling back to None."""
    g = lambda k: r[k] if k in r.keys() else None  # noqa: E731
    sa, sb = g("score_after"), g("score_before") or 0
    delta = f"{(sa - sb):+.3f}" if sa is not None else "n/a"
    cat = g("category") or "?"
    hyp = (g("hypothesis") or "").replace("\n", " ")[:140]
    kept_val = g("kept")
    if kept_val == 1:
        kept = "✓ KEPT"
    elif kept_val == 0:
        kept = "✗ reverted"
    else:
        kept = "—"
    return f"  iter {g('iter_num') or '?':>3} [{cat}] Δ={delta} {kept} — {hyp}"
