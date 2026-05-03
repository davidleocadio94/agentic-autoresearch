"""FastAPI + htmx dashboard at localhost:8765.

Reads the same DB the loop writes to. Read-only by default.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from agentic_autoresearch.memory.schema import connect

TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def _from_json(v):
    if not v:
        return {}
    try:
        return json.loads(v)
    except Exception:
        return {}


templates.env.filters["from_json"] = _from_json


def _format_cell(table: str, col: str, val) -> str:
    """Cell renderer for /db. Returns HTML."""
    from html import escape as _esc

    if val is None:
        return '<span class="cell-null">null</span>'
    # ID linking
    if col == "id" and table == "iterations":
        return f'<a href="/iter/{_esc(str(val))}" class="cell-id">{_esc(str(val)[:8])}…</a>'
    if col == "id" and table == "runs":
        return f'<a href="/run/{_esc(str(val))}" class="cell-id">{_esc(str(val)[:8])}…</a>'
    if col == "run_id":
        return f'<a href="/run/{_esc(str(val))}" class="cell-id">{_esc(str(val)[:8])}…</a>'
    if col == "iter_id":
        return f'<a href="/iter/{_esc(str(val))}" class="cell-id">{_esc(str(val)[:8])}…</a>'
    if col == "parent_iter_id":
        return f'<a href="/iter/{_esc(str(val))}" class="cell-id">{_esc(str(val)[:8])}…</a>'
    if col == "commit_hash":
        return f'<span class="cell-id">{_esc(str(val)[:8])}</span>'
    # Phase pills
    if col == "phase":
        return f'<span class="pill pill-phase-{_esc(str(val))}">{_esc(str(val))}</span>'
    # Booleans
    if col == "kept":
        if val == 1:
            return '<span class="cell-bool-yes">✓ yes</span>'
        if val == 0:
            return '<span class="cell-bool-no">✗ no</span>'
        return '<span class="cell-null">—</span>'
    # Numeric / scores
    if col in ("score_before", "score_after", "score_delta", "final_score"):
        try:
            return f'<span class="cell-num">{float(val):.4f}</span>'
        except (TypeError, ValueError):
            return _esc(str(val))
    if col in ("iter_num", "duration_seconds", "size_bytes", "guardrail_violations"):
        return f'<span class="cell-num">{_esc(str(val))}</span>'
    if col in ("api_cost_usd", "total_cost_usd"):
        try:
            return f'<span class="cell-num">${float(val):.3f}</span>'
        except (TypeError, ValueError):
            return _esc(str(val))
    # JSON columns — pretty print
    if col in ("per_fixture_scores", "files_touched"):
        try:
            parsed = json.loads(val) if isinstance(val, str) else val
            pretty = json.dumps(parsed, indent=2)
            return f'<pre class="cell-json">{_esc(pretty)}</pre>'
        except Exception:
            return _esc(str(val))
    # Long text — truncate with hover for full
    s = str(val)
    if len(s) > 200:
        full = _esc(s).replace('"', "&quot;")
        short = _esc(s[:200]) + "…"
        return f'<span class="cell-text cell-trunc" title="{full}">{short}</span>'
    return f'<span class="cell-text">{_esc(s)}</span>'


templates.env.globals["format_cell"] = _format_cell

app = FastAPI(title="agentic-autoresearch")


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    with connect() as conn:
        runs = list(
            conn.execute("SELECT * FROM runs ORDER BY started_at DESC LIMIT 50").fetchall()
        )
        live = list(conn.execute("SELECT * FROM runs WHERE status='running'").fetchall())
    return templates.TemplateResponse(
        request=request, name="index.html", context={"runs": runs, "live": live}
    )


@app.get("/run/{run_id}", response_class=HTMLResponse)
def run_view(request: Request, run_id: str, show: str = "all"):
    """show=all shows all iters; show=N shows the most recent N (default 20)."""
    with connect() as conn:
        run = conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if not run:
            raise HTTPException(404, "run not found")
        all_count = conn.execute(
            "SELECT COUNT(*) AS n FROM iterations WHERE run_id=?", (run_id,)
        ).fetchone()["n"]
        # By default show top 20 most-recent. ?show=all gives the full list.
        limit_clause = "" if show == "all" else "LIMIT 20"
        iters = list(
            conn.execute(
                f"SELECT * FROM iterations WHERE run_id=? "
                f"ORDER BY iter_num DESC {limit_clause}",
                (run_id,),
            ).fetchall()
        )
        # Live iter (in-progress) for sticky header
        live = conn.execute(
            "SELECT * FROM iterations WHERE run_id=? "
            "AND phase IN ('pending','planning','acting','reflecting') "
            "ORDER BY iter_num DESC LIMIT 1",
            (run_id,),
        ).fetchone()
    return templates.TemplateResponse(
        request=request,
        name="run.html",
        context={
            "run": run,
            "iters": iters,
            "all_count": all_count,
            "showing": "all" if show == "all" else min(20, all_count),
            "live": live,
        },
    )


@app.get("/run/{run_id}/score-curve")
def score_curve(run_id: str):
    with connect() as conn:
        rows = conn.execute(
            "SELECT iter_num, score_after FROM iterations "
            "WHERE run_id=? AND score_after IS NOT NULL ORDER BY iter_num ASC",
            (run_id,),
        ).fetchall()
    return JSONResponse({"x": [r["iter_num"] for r in rows], "y": [r["score_after"] for r in rows]})


@app.get("/errors/{run_id}")
def error_curve(run_id: str):
    """Per-fixture score over iters → JSON for client-side plotting."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT iter_num, per_fixture_scores FROM iterations "
            "WHERE run_id=? AND per_fixture_scores IS NOT NULL ORDER BY iter_num ASC",
            (run_id,),
        ).fetchall()
    fixtures: dict[str, list[dict]] = {}
    for r in rows:
        try:
            scores = json.loads(r["per_fixture_scores"])
        except (TypeError, json.JSONDecodeError):
            continue
        for f, s in scores.items():
            fixtures.setdefault(f, []).append({"iter": r["iter_num"], "score": s})
    return JSONResponse(fixtures)


@app.get("/iter/{iter_id}", response_class=HTMLResponse)
def iter_view(request: Request, iter_id: str):
    with connect() as conn:
        it = conn.execute("SELECT * FROM iterations WHERE id=?", (iter_id,)).fetchone()
        if not it:
            raise HTTPException(404, "iter not found")
        artifacts = list(
            conn.execute(
                "SELECT * FROM artifacts WHERE iter_id=? ORDER BY created_at",
                (iter_id,),
            ).fetchall()
        )
        # Find prev/next iter in the same run for nav
        prev_it = conn.execute(
            "SELECT id FROM iterations WHERE run_id=? AND iter_num<? ORDER BY iter_num DESC LIMIT 1",
            (it["run_id"], it["iter_num"]),
        ).fetchone()
        next_it = conn.execute(
            "SELECT id FROM iterations WHERE run_id=? AND iter_num>? ORDER BY iter_num ASC LIMIT 1",
            (it["run_id"], it["iter_num"]),
        ).fetchone()
        run = conn.execute("SELECT * FROM runs WHERE id=?", (it["run_id"],)).fetchone()
    # Parse per-fixture into a list[(name, score)]
    import json as _json
    per_fixture: list[tuple[str, float]] = []
    if it["per_fixture_scores"]:
        try:
            d = _json.loads(it["per_fixture_scores"])
            per_fixture = sorted(d.items())
        except Exception:
            pass
    files_touched: list[str] = []
    if it["files_touched"]:
        try:
            files_touched = _json.loads(it["files_touched"])
        except Exception:
            files_touched = [it["files_touched"]]
    return templates.TemplateResponse(
        request=request,
        name="iter.html",
        context={
            "it": it,
            "run": run,
            "artifacts": artifacts,
            "per_fixture": per_fixture,
            "files_touched": files_touched,
            "prev_it": prev_it,
            "next_it": next_it,
        },
    )


@app.get("/artifact/{artifact_id}/preview")
def artifact_preview(artifact_id: str, n: int = 200):
    """Tail the last `n` lines of an artifact (e.g. agent stream-json log)."""
    with connect() as conn:
        a = conn.execute("SELECT * FROM artifacts WHERE id=?", (artifact_id,)).fetchone()
    if not a:
        raise HTTPException(404)
    if a["path"]:
        from pathlib import Path as _P

        p = _P(a["path"])
        if not p.exists():
            return {"text": "(file not found)"}
        lines = p.read_text(errors="replace").splitlines()
        return {"text": "\n".join(lines[-n:])}
    if a["content"]:
        try:
            text = a["content"].decode("utf-8", errors="replace")
        except Exception:
            text = "(binary)"
        return {"text": text[-50_000:]}
    return {"text": "(empty)"}


@app.get("/health")
def health():
    with connect() as conn:
        live_run = conn.execute(
            "SELECT * FROM runs WHERE status='running' ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
        orch = conn.execute(
            "SELECT * FROM processes WHERE role='orchestrator' AND ended_at IS NULL "
            "ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
        cur_iter = None
        if live_run:
            cur_iter = conn.execute(
                "SELECT * FROM iterations WHERE run_id=? "
                "AND phase IN ('pending','planning','acting','reflecting') "
                "ORDER BY iter_num DESC LIMIT 1",
                (live_run["id"],),
            ).fetchone()
    payload = {
        "orchestrator": _orch_payload(orch),
        "current_run": dict(live_run) if live_run else None,
        "current_iter": dict(cur_iter) if cur_iter else None,
    }
    return JSONResponse(payload)


def _orch_payload(row) -> dict | None:
    if not row:
        return {"alive": False}
    last = row["last_heartbeat_at"]
    age = None
    try:
        age = (dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(last)).total_seconds()
    except Exception:
        pass
    return {
        "alive": True,
        "pid": row["pid"],
        "started_at": row["started_at"],
        "heartbeat_age_seconds": age,
    }


@app.get("/db", response_class=HTMLResponse)
def db_view(
    request: Request,
    table: str = Query("iterations"),
    limit: int = 50,
    q: str = Query("", description="free-text search across text columns"),
    preset: str = Query(""),
):
    allowed = {"runs", "iterations", "artifacts", "guardrail_violations", "processes"}
    if table not in allowed:
        raise HTTPException(400, f"table must be one of {sorted(allowed)}")
    with connect() as conn:
        cols = [d[1] for d in conn.execute(f"PRAGMA table_info({table})").fetchall()]
        sql, params, preset_label = _resolve_db_query(table, q, preset, limit)
        try:
            rows = list(conn.execute(sql, params).fetchall())
            err = None
        except Exception as e:
            rows = []
            err = str(e)
        # Stats per table
        stats = {
            t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in allowed
        }
    presets = _presets_for(table)
    return templates.TemplateResponse(
        request=request,
        name="db.html",
        context={
            "table": table,
            "rows": rows,
            "cols": cols,
            "tables": sorted(allowed),
            "stats": stats,
            "q": q,
            "preset": preset,
            "preset_label": preset_label,
            "presets": presets,
            "sql": sql,
            "err": err,
            "limit": limit,
        },
    )


def _presets_for(table: str) -> list[dict]:
    """Pre-canned queries shown as buttons in the sidebar."""
    if table == "iterations":
        return [
            {"key": "kept", "label": "✓ kept iters", "desc": "all iters that were merged"},
            {"key": "reverted", "label": "✗ reverted", "desc": "all iters that were rolled back"},
            {"key": "in_progress", "label": "⏵ in progress", "desc": "currently planning/acting/reflecting"},
            {"key": "regressions", "label": "⚠ regressions", "desc": "iters where score went down"},
            {"key": "biggest_wins", "label": "🏆 biggest wins", "desc": "iters with largest +Δ"},
        ]
    if table == "runs":
        return [
            {"key": "running", "label": "● running", "desc": "currently active runs"},
            {"key": "perfect", "label": "🎯 score=1.0", "desc": "runs that hit target"},
        ]
    if table == "processes":
        return [
            {"key": "alive", "label": "● alive", "desc": "processes without ended_at"},
        ]
    return []


def _resolve_db_query(table: str, q: str, preset: str, limit: int) -> tuple[str, tuple, str]:
    """Map preset/search → SQL. Returns (sql, params, label)."""
    base = f"SELECT * FROM {table}"
    where: list[str] = []
    params: list = []
    label = ""
    if table == "iterations":
        if preset == "kept":
            where.append("kept = 1"); label = "kept iters"
        elif preset == "reverted":
            where.append("kept = 0"); label = "reverted iters"
        elif preset == "in_progress":
            where.append("phase IN ('pending','planning','acting','reflecting')"); label = "in-progress"
        elif preset == "regressions":
            where.append("score_after IS NOT NULL AND score_after < score_before"); label = "regressions"
        elif preset == "biggest_wins":
            base = (f"SELECT * FROM {table}")
            label = "biggest wins"
            order = "ORDER BY (score_after - score_before) DESC"
            sql = f"{base} WHERE score_after IS NOT NULL {order} LIMIT ?"
            return sql, (limit,), label
    elif table == "runs":
        if preset == "running":
            where.append("status = 'running'"); label = "running"
        elif preset == "perfect":
            where.append("final_score >= 1.0"); label = "perfect runs"
    elif table == "processes":
        if preset == "alive":
            where.append("ended_at IS NULL"); label = "alive"
    if q:
        text_cols = _text_columns_for(table)
        if text_cols:
            ors = " OR ".join(f"{c} LIKE ?" for c in text_cols)
            where.append(f"({ors})")
            params.extend([f"%{q}%"] * len(text_cols))
            if not label:
                label = f"search: {q}"
    sql = base
    if where:
        sql += " WHERE " + " AND ".join(where)
    # default ordering (most recent first)
    if table == "iterations":
        sql += " ORDER BY iter_num DESC"
    elif table in ("runs", "artifacts", "guardrail_violations", "processes"):
        sql += " ORDER BY rowid DESC"
    sql += " LIMIT ?"
    params.append(limit)
    return sql, tuple(params), label


def _text_columns_for(table: str) -> list[str]:
    return {
        "iterations": ["category", "hypothesis", "lesson", "proposed_next", "files_touched", "phase"],
        "runs": ["problem_name", "status", "exit_reason"],
        "artifacts": ["kind", "path"],
        "guardrail_violations": ["kind", "detail", "action_taken"],
        "processes": ["role", "host"],
    }.get(table, [])


@app.get("/search", response_class=HTMLResponse)
def search(request: Request, q: str = Query("")):
    """Substring search over hypotheses + lessons (vector swap is v2)."""
    rows: list = []
    if q:
        with connect() as conn:
            rows = list(
                conn.execute(
                    "SELECT * FROM iterations WHERE hypothesis LIKE ? OR lesson LIKE ? "
                    "ORDER BY iter_num DESC LIMIT 50",
                    (f"%{q}%", f"%{q}%"),
                ).fetchall()
            )
    return templates.TemplateResponse(
        request=request, name="search.html", context={"q": q, "rows": rows}
    )


def serve(host: str = "127.0.0.1", port: int = 8765) -> None:
    import uvicorn

    uvicorn.run(app, host=host, port=port, log_level="warning")
