"""Track A — Audit memory effectiveness on the music-omr run.

For each iter in the run, in chronological order:
  - reconstruct the planner-visible memory AS OF that iter (only iters with
    iter_num < this one)
  - classify whether the hypothesis cited that memory (regex + manual rules)
  - record the resulting score delta
  - cluster citing-vs-not-citing iter score distributions
  - dump lesson→next-hypothesis chains

Read-only — won't disturb the running json-parser loop.
"""

from __future__ import annotations

import json
import re
import sqlite3
import statistics
import sys
from pathlib import Path

# import path
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "src"))

from agentic_autoresearch.memory.schema import connect  # noqa: E402

PROBLEM = "sheet-music-omr"


def get_run_iters() -> tuple[str, list[sqlite3.Row]]:
    c = connect()
    try:
        run = c.execute(
            "SELECT id FROM runs WHERE problem_name=? AND status='done' "
            "ORDER BY started_at DESC LIMIT 1",
            (PROBLEM,),
        ).fetchone()
        if not run:
            sys.exit(f"no completed run for {PROBLEM}")
        iters = list(
            c.execute(
                "SELECT * FROM iterations WHERE run_id=? ORDER BY iter_num ASC",
                (run["id"],),
            ).fetchall()
        )
        return run["id"], iters
    finally:
        c.close()


# ----- Q2: did hypothesis cite memory? ----------------------------------------

ITER_REF = re.compile(r"\biter[\s-]*(\d+)\b", re.IGNORECASE)
FIXTURE_REF = re.compile(
    r"\b(0\d-(pitch|rhythm|meter|key|voicing|articulation|real)|"
    r"c[-\s]*major|d[-\s]*major|octave[-\s]*leap|quarter[-\s]*eighth|dotted[-\s]*quarter|"
    r"3[-\s/]*4[-\s]*time)\b",
    re.IGNORECASE,
)
LESSON_LANGUAGE = re.compile(
    r"\b(prior|previous|earlier|past|history|memory|lesson|previously|already (tried|done|attempted)|"
    r"as iter|in iter|from iter|kept iter|reverted|noted in)\b",
    re.IGNORECASE,
)


def classify_citation(hypothesis: str) -> dict:
    if not hypothesis:
        return {"cited": False, "iter_refs": [], "fixture_refs": [], "lesson_lang": False}
    iter_refs = sorted({int(m) for m in ITER_REF.findall(hypothesis)})
    fixture_refs = sorted({m[0] for m in FIXTURE_REF.findall(hypothesis)})
    lesson_lang = bool(LESSON_LANGUAGE.search(hypothesis))
    cited = bool(iter_refs or fixture_refs or lesson_lang)
    return {
        "cited": cited,
        "iter_refs": iter_refs,
        "fixture_refs": fixture_refs,
        "lesson_lang": lesson_lang,
    }


# ----- Q1: what memory was visible? -------------------------------------------


def memory_size_as_of(iters: list[sqlite3.Row], iter_num: int) -> dict:
    """How much memory was available when the planner for `iter_num` ran?"""
    prior = [it for it in iters if it["iter_num"] < iter_num]
    kept = [it for it in prior if it["kept"] == 1]
    reverted = [it for it in prior if it["kept"] == 0]
    open_hyp = sum(1 for it in prior if (it["proposed_next"] or "").strip())
    return {
        "n_prior": len(prior),
        "n_kept": len(kept),
        "n_reverted": len(reverted),
        "n_open_hypotheses": open_hyp,
    }


# ----- main report ------------------------------------------------------------


def main() -> None:
    run_id, iters = get_run_iters()
    print(f"# Track A — Memory Effectiveness Audit")
    print(f"\n**Run:** {run_id[:8]}… (`{PROBLEM}`)  ")
    print(f"**Iters:** {len(iters)}\n")

    rows = []
    citing_deltas: list[float] = []
    not_citing_deltas: list[float] = []

    print("## Q1+Q2: per-iter memory + citation table\n")
    print("| iter | mem (prior/kept/open) | cited memory? | iter refs | fixture refs | lang | Δ score |")
    print("|------|------------------------|---------------|-----------|--------------|------|---------|")
    for it in iters:
        mem = memory_size_as_of(iters, it["iter_num"])
        cit = classify_citation(it["hypothesis"] or "")
        sb = it["score_before"] or 0
        sa = it["score_after"]
        delta = sa - sb if sa is not None else None
        delta_str = f"{delta:+.3f}" if delta is not None else "n/a"
        cited_marker = "✓" if cit["cited"] else "✗"
        iter_refs = ",".join(str(x) for x in cit["iter_refs"]) or "—"
        fix_refs = ",".join(cit["fixture_refs"][:2]) or "—"
        lang = "✓" if cit["lesson_lang"] else "✗"
        mem_str = f"{mem['n_prior']}/{mem['n_kept']}/{mem['n_open_hypotheses']}"
        print(
            f"| {it['iter_num']} | {mem_str} | {cited_marker} | {iter_refs} | {fix_refs} | {lang} | {delta_str} |"
        )
        if delta is not None and it["iter_num"] > 1:
            if cit["cited"]:
                citing_deltas.append(delta)
            else:
                not_citing_deltas.append(delta)
        rows.append((it, mem, cit, delta))

    # ----- Q3: did citing memory help? ----------------------------------------
    print("\n## Q3: did citing memory correlate with better deltas?\n")
    print(f"- iters citing memory: **{len(citing_deltas)}**")
    print(f"- iters NOT citing memory: **{len(not_citing_deltas)}**")
    if citing_deltas:
        print(
            f"- citing mean Δ: **{statistics.mean(citing_deltas):+.4f}** "
            f"(median {statistics.median(citing_deltas):+.4f}, "
            f"stdev {statistics.stdev(citing_deltas) if len(citing_deltas)>1 else 0:.4f})"
        )
    if not_citing_deltas:
        print(
            f"- not-citing mean Δ: **{statistics.mean(not_citing_deltas):+.4f}** "
            f"(median {statistics.median(not_citing_deltas):+.4f}, "
            f"stdev {statistics.stdev(not_citing_deltas) if len(not_citing_deltas)>1 else 0:.4f})"
        )
    if citing_deltas and not_citing_deltas:
        diff = statistics.mean(citing_deltas) - statistics.mean(not_citing_deltas)
        verdict = (
            "memory citation correlates with BETTER outcomes"
            if diff > 0.02
            else (
                "memory citation correlates with WORSE outcomes"
                if diff < -0.02
                else "memory citation has NO meaningful effect on outcomes"
            )
        )
        print(f"\n**Δ-of-means: {diff:+.4f} — {verdict}**\n")

    # ----- Q4: lesson→hypothesis chains ---------------------------------------
    print("\n## Q4: lesson→next-hypothesis chains\n")
    print("(Walking each (iter N lesson → iter N+1 hypothesis) pair)\n")
    for i in range(len(iters) - 1):
        cur = iters[i]
        nxt = iters[i + 1]
        lesson = (cur["lesson"] or "").strip()
        nxt_hyp = (nxt["hypothesis"] or "").strip()
        if not lesson:
            continue
        nxt_cit = classify_citation(nxt_hyp)
        cited_prev = cur["iter_num"] in nxt_cit["iter_refs"]
        marker = "✓ cited" if cited_prev else "✗ ignored"
        print(f"### iter {cur['iter_num']} → iter {nxt['iter_num']} ({marker})")
        print(f"\n**iter {cur['iter_num']} lesson:**")
        print(f"> {lesson[:400]}{'…' if len(lesson) > 400 else ''}")
        print(f"\n**iter {nxt['iter_num']} hypothesis:**")
        print(f"> {nxt_hyp[:400]}{'…' if len(nxt_hyp) > 400 else ''}")
        print()

    # ----- Q5: open hypotheses leakage ---------------------------------------
    print("\n## Q5: open hypotheses left dangling\n")
    open_count = 0
    for it in iters:
        pn = (it["proposed_next"] or "").strip()
        if not pn:
            continue
        # was this proposed_next ever picked up by a later iter?
        later = [
            x for x in iters if x["iter_num"] > it["iter_num"] and (x["hypothesis"] or "")
        ]
        # crude: is any keyword from proposed_next in a later hypothesis?
        keywords = re.findall(r"\b[a-z]{5,}\b", pn.lower())[:8]
        picked_up = False
        for x in later:
            xh = (x["hypothesis"] or "").lower()
            hits = sum(1 for k in keywords if k in xh)
            if hits >= 3:
                picked_up = True
                break
        if not picked_up:
            open_count += 1
            print(
                f"- iter {it['iter_num']} proposed: {pn[:200]}{'…' if len(pn) > 200 else ''}"
            )
    print(f"\n**{open_count} of {sum(1 for it in iters if (it['proposed_next'] or '').strip())} proposed_next entries never picked up by a later iter.**\n")


if __name__ == "__main__":
    main()
