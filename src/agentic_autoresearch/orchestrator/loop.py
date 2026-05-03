"""The Plan/Act/Reflect loop. Three claude -p calls per iteration.

This is the heart of the framework. It's small on purpose — most of the
intelligence is in the agents, the memory queries, and the eval.
"""

from __future__ import annotations

import json
import os
import signal
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from agentic_autoresearch.agents.prompts import (
    ACTOR_PROMPT,
    PLANNER_PROMPT,
    REFLECTOR_PROMPT,
)
from agentic_autoresearch.agents.spawn import parse_json_block, run_agent
from agentic_autoresearch.guardrails.enforce import (
    enforce_post_iter,
    hash_immutable,
)
from agentic_autoresearch.memory import intuition, store
from agentic_autoresearch.memory.embeddings import embed, is_zero
from agentic_autoresearch.memory.schema import init_db
from agentic_autoresearch.orchestrator.eval_runner import (
    default_eval_command,
    run_eval,
)
from agentic_autoresearch.orchestrator.worktree import ensure_initial_commit
from agentic_autoresearch.paths import artifacts_dir
from agentic_autoresearch.spec import ProblemSpec, parse_spec

# Hard deadlines per agent role (matches plan)
PLANNER_DEADLINE = 5 * 60
ACTOR_DEADLINE = 60 * 60
REFLECTOR_DEADLINE = 3 * 60
PLANNER_TURNS = 20
ACTOR_TURNS = 80
REFLECTOR_TURNS = 15
EVAL_TIMEOUT = 30 * 60


@dataclass
class LoopOptions:
    max_hours: float | None = None
    max_iters: int | None = None
    target_score: float | None = None
    max_cost_usd: float | None = None


def run_loop(spec_path: Path, opts: LoopOptions | None = None) -> str:
    """Entry point — runs until exit condition. Returns the run_id.

    Bug B model: every iter merges its work directly to main. No
    keep/revert. The loop walks search space, memory pulls planner
    back from bad regions, early-stop on plateau.
    """
    init_db()
    spec = parse_spec(spec_path)
    _load_problem_env(spec.repo_path)
    ensure_initial_commit(spec.repo_path)

    opts = opts or LoopOptions()
    target_score = opts.target_score or spec.exit.target_score
    max_iters = opts.max_iters or spec.exit.max_iters
    max_hours = opts.max_hours or spec.exit.max_hours
    deadline = time.monotonic() + max_hours * 3600

    run_id = store.create_run(spec.name, spec.repo_path)
    store.register_process(os.getpid(), "orchestrator", run_id, None)

    # heartbeat thread
    stop = threading.Event()
    threading.Thread(target=_heartbeat_loop, args=(os.getpid(), stop), daemon=True).start()

    # signal handling for graceful shutdown
    def _term(signum, _):
        stop.set()
    signal.signal(signal.SIGTERM, _term)
    signal.signal(signal.SIGINT, _term)

    final_score: float | None = None
    exit_reason = "unknown"
    max_cost = opts.max_cost_usd or spec.exit.max_cost_usd
    consecutive_crashes = 0
    MAX_CONSECUTIVE_CRASHES = 3
    try:
        while True:
            if stop.is_set():
                exit_reason = "interrupted"
                break
            if time.monotonic() > deadline:
                exit_reason = "max_hours"
                break
            iter_num = store.last_iter_num(run_id) + 1
            if iter_num > max_iters:
                exit_reason = "max_iters"
                break
            score_before = _current_score(run_id)
            if score_before is not None and score_before >= target_score:
                exit_reason = "target_reached"
                final_score = score_before
                break
            # Cap on Gemini spend ONLY (Claude is sunk via subscription).
            # max_cost <= 0 disables the cap (problem doesn't use paid APIs).
            if max_cost > 0:
                spent = _total_gemini_cost(run_id)
                if spent >= max_cost:
                    exit_reason = "max_gemini_cost_usd"
                    break
            # Early-stop on plateau (no new best for N iters).
            _, _, plateau = store.get_run_best(run_id)
            if spec.exit.plateau_stop_after > 0 and plateau >= spec.exit.plateau_stop_after:
                exit_reason = "plateau"
                break

            try:
                final_score = _run_one_iter(spec, run_id, iter_num, score_before or 0.0)
                consecutive_crashes = 0  # success — reset crash counter
            except Exception as e:  # never crash the loop on a single iter failing
                import traceback
                print(f"[orchestrator] iter {iter_num} crashed: {e!r}")
                traceback.print_exc()
                # Mark any half-finalized iter row as crashed so resume sees it
                _mark_orphan_iters_for_run(run_id)
                consecutive_crashes += 1
                if consecutive_crashes >= MAX_CONSECUTIVE_CRASHES:
                    print(
                        f"[orchestrator] {consecutive_crashes} consecutive iters crashed — "
                        f"bailing out (likely a bug, not a transient failure)"
                    )
                    exit_reason = "consecutive_crashes"
                    break
                continue
    finally:
        stop.set()
        # Any iter still mid-phase when we exit gets marked as failed
        _mark_orphan_iters_for_run(run_id)
        store.end_process(os.getpid())
        store.end_run(run_id, exit_reason=exit_reason, final_score=final_score)
    return run_id


# --------------------------------------------------------------------------- one iter


def _run_one_iter(spec: ProblemSpec, run_id: str, iter_num: int, score_before: float) -> float:
    """One iter under the gradient-descent model.

    The actor edits files in spec.repo_path (main checkout), runs the
    eval, and commits. No worktrees. No keep/revert decision. The
    score moves up or down each iter; memory + plateau detection are
    what pull the loop back from bad regions.
    """
    art_dir = artifacts_dir(run_id)
    iter_id = store.create_iteration(
        store.IterationDraft(
            run_id=run_id,
            iter_num=iter_num,
            parent_iter_id=None,
            score_before=score_before,
        )
    )
    started = time.monotonic()
    repo = spec.repo_path

    # ----- PLAN
    store.set_phase(iter_id, "planning")
    plan_prompt = _build_plan_prompt(spec, run_id, score_before)
    plan_log = art_dir / f"iter_{iter_num:04d}.planner.log"
    plan_res = run_agent(
        plan_prompt,
        cwd=repo,
        log_path=plan_log,
        deadline_seconds=PLANNER_DEADLINE,
        max_turns=PLANNER_TURNS,
    )
    hypothesis = parse_json_block(plan_res.final_message or "")
    store.add_artifact(iter_id, "planner_log", path=plan_log)
    if not hypothesis:
        store.finalize_iteration(
            iter_id,
            phase="failed_planning",
            kept=None,
            score_after=None,
            per_fixture_scores=None,
            lesson="planner produced no hypothesis JSON",
            proposed_next=None,
            commit_hash=None,
            duration_seconds=int(time.monotonic() - started),
        )
        return score_before
    store.update_iteration(
        iter_id,
        category=hypothesis.get("category"),
        hypothesis=hypothesis.get("rationale"),
        files_touched=json.dumps(hypothesis.get("files_to_touch") or []),
    )

    # ----- ACT (directly on main; commit when done)
    store.set_phase(iter_id, "acting")
    immutable_before = hash_immutable(repo, spec.constraints.immutable_paths)
    eval_output = repo / ".autoresearch_eval.json"
    actor_prompt = ACTOR_PROMPT.format(
        iter_num=iter_num,
        cwd=repo,
        hypothesis_json=json.dumps(hypothesis, indent=2),
        immutable_paths=", ".join(spec.constraints.immutable_paths) or "(none)",
        forbidden_deps=", ".join(spec.constraints.forbidden_deps) or "(none)",
        max_repo_bytes=spec.constraints.max_repo_bytes,
        eval_command=default_eval_command(spec.eval, eval_output),
        eval_output_path=eval_output,
        category=hypothesis.get("category", "unknown"),
        spec_body=spec.body,
    )
    actor_log = art_dir / f"iter_{iter_num:04d}.actor.log"
    actor_res = run_agent(
        actor_prompt,
        cwd=repo,
        log_path=actor_log,
        deadline_seconds=ACTOR_DEADLINE,
        max_turns=ACTOR_TURNS,
    )
    store.add_artifact(iter_id, "actor_log", path=actor_log)
    actor_report = parse_json_block(actor_res.final_message or "") or {}

    # Read the actor's eval output. If missing, re-run.
    score_after = None
    if eval_output.exists():
        import json as _json
        try:
            raw = _json.loads(eval_output.read_text())
            sa = raw.get("score")
            if isinstance(sa, (int, float)):
                score_after = float(sa)
            per_fixture = {
                k: float(v) for k, v in (raw.get("per_fixture") or {}).items()
            }
            from agentic_autoresearch.orchestrator.eval_runner import EvalResult
            eval_result = EvalResult(
                score=score_after,
                per_fixture=per_fixture,
                raw=raw,
                stdout="(read from actor's eval_output)",
                stderr="",
                exit_code=0,
            )
        except Exception:
            eval_result = run_eval(repo, spec.eval, eval_output, timeout=EVAL_TIMEOUT)
            score_after = eval_result.score
    else:
        eval_result = run_eval(repo, spec.eval, eval_output, timeout=EVAL_TIMEOUT)
        score_after = eval_result.score
    if score_after is None:
        rs = actor_report.get("score")
        if isinstance(rs, (int, float)):
            score_after = float(rs)

    # Make sure the agent's edits + the eval output are committed.
    # If the actor already committed, this is a no-op.
    commit_hash = _ensure_commit(repo, hypothesis.get("category", "iter"), iter_num)
    if not commit_hash:
        commit_hash = actor_report.get("commit_hash")

    # ----- REFLECT (lesson + proposed_next only — no keep/revert authority)
    store.set_phase(iter_id, "reflecting")
    diff = _diff_last_commit(repo)
    reflector_prompt = REFLECTOR_PROMPT.format(
        iter_num=iter_num,
        hypothesis_json=json.dumps(hypothesis, indent=2),
        actor_report=json.dumps(actor_report, indent=2),
        eval_results=json.dumps(eval_result.per_fixture, indent=2),
        score_before=score_before,
        score_after=("%.4f" % score_after) if score_after is not None else "null",
        diff=diff,
        similar_iters=_format_similar(run_id, hypothesis.get("rationale", "")),
    )
    reflector_log = art_dir / f"iter_{iter_num:04d}.reflector.log"
    refl_res = run_agent(
        reflector_prompt,
        cwd=repo,
        log_path=reflector_log,
        deadline_seconds=REFLECTOR_DEADLINE,
        max_turns=REFLECTOR_TURNS,
    )
    store.add_artifact(iter_id, "reflector_log", path=reflector_log)
    reflection = parse_json_block(refl_res.final_message or "") or {}

    # ----- guardrails (audit only — we already committed; if violated, mark)
    violations = enforce_post_iter(repo, spec.constraints, immutable_before)
    for v in violations:
        store.add_artifact(
            iter_id, "guardrail_violation", content=f"{v.kind}: {v.detail}".encode()
        )

    # ----- compute confidence + update best/plateau
    confidence = _compute_confidence(run_id, score_before, score_after)
    if score_after is not None:
        store.update_best(run_id, score=score_after, commit=commit_hash)

    # ----- cost tracking
    claude_cost = (
        (plan_res.cost_usd or 0.0)
        + (actor_res.cost_usd or 0.0)
        + (refl_res.cost_usd or 0.0)
    )
    gemini_cost = 0.0
    if eval_output.exists():
        try:
            import json as _json2
            extra = _json2.loads(eval_output.read_text()).get("cost_usd")
            if isinstance(extra, (int, float)):
                gemini_cost = float(extra)
        except Exception:
            pass
    store.update_iteration(
        iter_id,
        api_cost_usd=claude_cost + gemini_cost,
        claude_cost_usd=claude_cost,
        gemini_cost_usd=gemini_cost,
        confidence=confidence,
    )
    store.update_run_cost(run_id)

    # The `kept` field is now informational only — kept=1 if score_after >
    # score_before, kept=0 otherwise. Used for backward-compat with
    # existing memory queries; the loop no longer reverts.
    informational_kept = (
        score_after is not None and score_before is not None and score_after > score_before
    )
    store.finalize_iteration(
        iter_id,
        phase="done",
        kept=informational_kept,
        score_after=score_after,
        per_fixture_scores=eval_result.per_fixture,
        lesson=reflection.get("lesson"),
        proposed_next=reflection.get("proposed_next"),
        commit_hash=commit_hash,
        duration_seconds=int(time.monotonic() - started),
    )

    # ----- embed hypothesis + lesson for future PLANNER intuition queries
    rationale = hypothesis.get("rationale") or ""
    lesson_text = reflection.get("lesson") or ""
    if rationale.strip():
        h_vec = embed(rationale)
        if not is_zero(h_vec):
            store.store_hypothesis_embed(iter_id, h_vec)
    if lesson_text.strip():
        l_vec = embed(lesson_text)
        if not is_zero(l_vec):
            store.store_lesson_embed(iter_id, l_vec)

    return score_after if score_after is not None else score_before


# --------------------------------------------------------------------------- helpers added for Bugs A+B


def _ensure_commit(repo: Path, category: str, iter_num: int) -> str | None:
    """If there are uncommitted changes from the actor, commit them.
    Returns the resulting HEAD commit hash, or None on failure."""
    import subprocess
    try:
        # are there changes?
        st = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=repo, capture_output=True, text=True, check=True,
        )
        if st.stdout.strip():
            subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
            subprocess.run(
                ["git",
                 "-c", "user.email=loop@local",
                 "-c", "user.name=loop",
                 "commit", "-m", f"[{category}] iter {iter_num}",
                 "--allow-empty"],
                cwd=repo, check=True, capture_output=True,
            )
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo, capture_output=True, text=True, check=True,
        )
        return head.stdout.strip()
    except subprocess.CalledProcessError as e:
        print(f"[orchestrator] commit failed: {e.stderr or e.stdout}")
        return None


def _diff_last_commit(repo: Path, max_chars: int = 4000) -> str:
    """Diff the most recent commit (the one this iter just made)."""
    import subprocess
    try:
        res = subprocess.run(
            ["git", "diff", "HEAD~1..HEAD"],
            cwd=repo, capture_output=True, text=True, check=False,
        )
        out = res.stdout
        return out[:max_chars] + ("\n... (truncated)" if len(out) > max_chars else "")
    except Exception:
        return "(diff unavailable)"


def _compute_confidence(run_id: str, score_before: float | None, score_after: float | None) -> float | None:
    """confidence = |delta| / sigma, where sigma = stdev of past iters'
    |deltas| over the rolling 20-iter window. None for first 2 iters
    (insufficient data)."""
    import statistics
    if score_before is None or score_after is None:
        return None
    deltas = store.absolute_deltas(run_id, limit=20)
    if len(deltas) < 2:
        return None
    sigma = statistics.stdev(deltas)
    if sigma <= 0:
        # all prior |deltas| equal — degenerate. Treat |delta|>0 as
        # high-confidence; |delta|=0 as zero.
        return float("inf") if abs(score_after - score_before) > 0 else 0.0
    return abs(score_after - score_before) / sigma


# --------------------------------------------------------------------------- helpers


def _load_problem_env(repo_path: Path) -> None:
    """Pull GEMINI_API_KEY (and others) from the problem repo's .env."""
    env_path = repo_path / ".env"
    if not env_path.exists():
        return
    try:
        from dotenv import load_dotenv

        load_dotenv(env_path, override=False)
    except Exception:
        # last-resort: parse manually
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip()
            v = v.strip().strip('"').strip("'")
            if k and k not in os.environ:
                os.environ[k] = v


def _heartbeat_loop(pid: int, stop: threading.Event) -> None:
    while not stop.is_set():
        try:
            store.heartbeat(pid)
        except Exception:
            pass
        stop.wait(15)


def _current_score(run_id: str) -> float | None:
    rows = store.recent_iterations(run_id, limit=1)
    if not rows:
        return None
    s = rows[0]["score_after"]
    return float(s) if s is not None else None


def _mark_orphan_iters_for_run(run_id: str) -> None:
    """Any iter still in a transient phase becomes failed_<phase>."""
    from agentic_autoresearch.memory.schema import connect as _connect

    c = _connect()
    try:
        rows = c.execute(
            "SELECT id, phase FROM iterations WHERE run_id=? "
            "AND phase IN ('pending','planning','acting','reflecting')",
            (run_id,),
        ).fetchall()
        for r in rows:
            new_phase = f"failed_{r['phase']}"
            c.execute(
                "UPDATE iterations SET phase=?, kept=0, ended_at=COALESCE(ended_at, ?) "
                "WHERE id=?",
                (new_phase, store.now(), r["id"]),
            )
    finally:
        c.close()


def _total_cost(run_id: str) -> float:
    """Sum api_cost_usd (Claude + Gemini) across all iters in this run."""
    return _sum_col(run_id, "api_cost_usd")


def _total_gemini_cost(run_id: str) -> float:
    """Sum gemini_cost_usd only — used for the budget cap."""
    return _sum_col(run_id, "gemini_cost_usd")


def _sum_col(run_id: str, col: str) -> float:
    from agentic_autoresearch.memory.schema import connect as _connect

    c = _connect()
    try:
        row = c.execute(
            f"SELECT COALESCE(SUM({col}), 0) AS s FROM iterations WHERE run_id=?",
            (run_id,),
        ).fetchone()
        return float(row["s"] or 0)
    finally:
        c.close()


def _build_plan_prompt(spec: ProblemSpec, run_id: str, score: float) -> str:
    """Pre-compute all 8 intuition queries and splice into prompt."""
    # First pass: no draft rationale → similar section will be empty.
    # The PLANNER's first read of memory still gets recent/category/winners/etc.
    # (draft-similar requires an iterative agent; v2 work)
    sections = intuition.format_for_prompt(run_id, draft_rationale="")
    return PLANNER_PROMPT.format(
        spec_body=spec.body,
        current_score=score,
        target_score=spec.exit.target_score,
        recent_iters=sections["recent"],
        similar_iters=sections["similar"],
        category_stats=sections["category"],
        high_conf_wins=sections["high_conf_wins"],
        low_conf_wins=sections["low_conf_wins"],
        noise_floor=sections["noise_floor"],
        open_hypotheses=sections["open_hyp"],
        error_curve=sections["error_curve"],
        cross_run=sections["cross_run"],
        constraints=_format_constraints(spec),
    )


def _format_constraints(spec: ProblemSpec) -> str:
    return (
        f"  immutable_paths: {list(spec.constraints.immutable_paths)}\n"
        f"  forbidden_deps:  {list(spec.constraints.forbidden_deps)}\n"
        f"  max_repo_bytes:  {spec.constraints.max_repo_bytes}\n"
        f"  forbid_gui:      {spec.constraints.forbid_gui}"
    )


def _format_similar(run_id: str, rationale: str) -> str:
    """Vector-search past iters by hypothesis similarity to the current one."""
    if not rationale.strip():
        return "(no rationale to compare)"
    v = embed(rationale)
    if is_zero(v):
        return "(embedding unavailable)"
    rows = intuition.similar_hypotheses(v, k=5)
    if not rows:
        return "(no similar past hypotheses)"
    return "\n".join(intuition._fmt_iter_one(r) + f"  [d={r['distance']:.3f}]" for r in rows)
