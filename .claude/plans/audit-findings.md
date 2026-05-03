# Track A — Findings

> Run audited: `3032e9b6` (sheet-music-omr, 25 iters, 2026-05-02 → 03)
> Source: `~/.agentic-autoresearch/db.sqlite`
> Audit script: `scripts/audit_memory.py`
> Raw output: `audit-results.md`

## TL;DR

```
            ┌──────────────────────────────────────────────────────┐
            │  Memory IS being read and used. That's not the bug.  │
            └──────────────────────────────────────────────────────┘
                                      │
                                      ▼
       ┌──────────────────────────────────────────────────────────────┐
       │  Two real bugs:                                              │
       │   A. Framework treats every score change as equally certain. │
       │      A noise-driven blip and a real win are recorded as the  │
       │      same kind of evidence. Fix: tag every iter with a       │
       │      confidence number derived from the run's noise floor.   │
       │      Memory queries weight evidence accordingly. Nothing is  │
       │      blocked or rejected.                                    │
       │   B. Keep/revert is the wrong abstraction. Real SGD just     │
       │      keeps stepping. Fix: drop keep/revert, every iter       │
       │      merges to main, memory pulls the planner back toward    │
       │      good regions, early-stop on plateau.                    │
       └──────────────────────────────────────────────────────────────┘
```

---

## The score story

```
score
1.0 ┤
0.8 ┤  ●  iter 1: 0.778  ←─── high-water mark, first try, 5 minutes
    │   \
0.6 ┤    ●─●          ●                ●─●                 ●─●
    │       \        / \              /   \              /     \
0.4 ┤        ●─●─●  ●   ●──●─●──●──●─●     ●─●─●──●─●──●●       \─●─●
    │              ●                                    ↑          ↑
0.2 ┤                                              iter 23: 0.667  iter 25: 0.444
    │                                                    └── -0.33 vs iter 1 ──┘
    └──────────────────────────────────────────────────────
       1   3   5   7   9   11  13  15  17  19  21  23  25
                              iteration
```

After 25 iters and ~12 hours of compute, **iter 25 is 0.33 lower
than iter 1's first 5-minute attempt.** Best score across the entire
run was iter 1's 0.778 — never beaten.

---

## What memory looked like to the planner

```
iter   prior   kept   open hypotheses   visible to planner?
─────  ─────  ──────  ───────────────   ───────────────────
  1      0      0           0           (nothing — first iter)
  2      1      1           1           ✓
  5      4      2           4           ✓
 10      9      4           9           ✓
 15     14      5          14           ✓
 20     19      7          19           ✓
 25     24     10          24           ✓ memory is FULL of data
```

Memory wasn't empty. The `intuition.format_for_prompt()` call returned
a non-trivial prompt section every iter past iter 1.

---

## Did planners cite memory?

```
              cited memory?
              ┌─────────────────────────────┐
              │  YES = 23 iters             │
              │  NO  =  1 iter              │  ← only iter 25
              └─────────────────────────────┘
                       96% citation rate

  what counts as "cited":
   - referenced an iter number ("iter 8 found...")
   - referenced a fixture by name ("02-rhythm")
   - used lesson-recall language ("prior", "previously", "as iter X")
```

Almost every planner explicitly named iter numbers, fixture names, or
used lesson-recall language. Memory is structurally wired into the
prompt and the planners do read it.

---

## Did citing memory help?

```
                     mean Δ score   median Δ   stdev
                     ────────────   ────────   ─────
   cited (n=23)        -0.0145      -0.0556   0.149
   didn't cite (n=1)   +0.0000      +0.0000   0.000

   Δ-of-means:         -0.0145
                       ──────────────────────────────
                       Memory citation has NO MEASURABLE EFFECT
                       on iter outcomes.

   Worse: stdev (0.149) is LARGER than the typical delta itself.
   Every "+0.111" win is statistically indistinguishable from noise.
```

The agents *are* consulting memory, but doing so doesn't help, because
the memory itself is bad.

---

## Where the "noise" actually comes from

The evaluator (`eval/score.py`) is **fully deterministic**. Same two
MusicXML files → same score. Always.

```
   ┌──────────────┐    ┌──────────────────┐    ┌─────────┐
   │  GT.xml      │───▶│                  │───▶│ score   │
   │  (fixed)     │    │  evaluator.py    │    │ (fixed) │
   │              │    │  (deterministic) │    │         │
   │  cand.xml    │───▶│                  │    │         │
   │  (input)     │    └──────────────────┘    └─────────┘
   └──────────────┘
```

What's noisy is the **other input** — the candidate XML. Each Gemini
call on the same PDF produces a slightly different XML, and a slightly
different XML produces a different score:

```
   ┌─────────────┐
   │ source.pdf  │  ──┐
   │ (fixed)     │    │
   └─────────────┘    │  call 1: cand_v1.xml  →  score 0.42
                      │
                      ├─▶  Gemini Vision   call 2: cand_v2.xml  →  score 0.75
                      │   (NON-DETERMINISTIC)
                      │
                      │  call 3: cand_v3.xml  →  score 0.58
                      │
   ┌─────────────┐    │
   │  prompt     │  ──┘
   │  (fixed)    │
   └─────────────┘
                       ▲
                       │
              same code, different XMLs
              same evaluator, different scores
```

The eval is a thermometer. It reads correctly. The agent's pipeline
keeps dunking it in different bowls and the framework writes down
"this kitchen is hotter than that one!"

---

## Why this is the agent's bug to solve, not the framework's

```
   the framework's job                      the agent's job
   ───────────────────                      ───────────────
   own the keep/revert decision             own the pipeline noise

   refuse to act on a single                if a stable score matters,
   data point as if it were truth            do self-consistency voting
                                             inside transcribe(pdf)
```

The agent owns `transcribe(pdf)`. If the agent wants a stable score,
it should do self-consistency voting *inside its pipeline* (call
Gemini N times, vote per measure). The framework can't fix this from
above — it would just smear out the noise behind the agent's back
without telling the agent the pipeline is noisy.

---

## The noise → bad lesson → wasted iters pipeline

```
        single noisy eval                  reflector promotes
        score happens to be                "kept" iter to a
        +0.111 by chance         ──▶       canonical lesson
                                                    │
                                                    ▼
        ──────────────────────                next planner reads
                                              the lesson, cites it,
        single noisy eval                     designs an experiment
        on next iter is -0.111   ◀───         downstream of the
        — same code would                     (false) finding
        produce different scores
                                                    │
                                                    ▼
                                              now reflector sees a
                                              regression, marks it
                                              "reverted" — the false
                                              lesson stays canonical
                                              even though it never
                                              produced a stable win
```

Concrete trace from the run:

```
iter 8   preproc (DPI bump)          +0.333  →  KEPT
                                                  │
                                                  ▼
                           lesson: "DPI bump is the win for rhythm fixtures"
                                                  │
                                                  ▼
iter 9   rerun (re-eval iter 8)      -0.222  →  REVERTED  (same code, different score)
iter 13  preproc (per-staff crop)    +0.222  →  KEPT
iter 14  preproc (label overlays)    -0.111  →  REVERTED
iter 15  postproc (third-repair)     +0.111  →  KEPT
iter 16  prompt (temp=0)             -0.111  →  REVERTED
iter 17  rerun (N=3 vote @ temp=0)   +0.000  →  REVERTED  ← agent's correct fix
iter 18  rerun (temp=0 alone)        -0.222  →  REVERTED
iter 19  rerun (temp=0 alone again)  +0.111  →  KEPT      ← lucky retry of SAME idea
                                                  │
                                                  ▼
                           lesson: "fixing the measurement instrument
                                    BEFORE chasing prompt/preproc levers
                                    was correct"
                                                  │
                                                  ▼
iters 20-25 all cite iter 19's lesson as gospel,
              each tweaking around the alleged "now-deterministic" baseline,
              all reverting, score drifts down to 0.444
                                                  │
                                                  ▼
iter 24's lesson finally says:
   "The 0.5556→0.4444 swing here proves Gemini is NOT byte-deterministic
    at temp=0 the way iter 19 claimed."
                                                  │
                                                  ▼
                  ✗ too late. iter 19's bad lesson already shaped 5 wasted iters.
```

---

## Bug A — Decisions on noisy data without a noise floor

```
   iter delta: +0.111  →  reflector compares to ZERO  →  KEEP, canonize lesson
                                                                  │
                                                                  ▼
                                                       next planner cites it
                                                                  │
                                                                  ▼
                                                       5 iters of work
                                                       built on a coin flip
```

The framework treats every Δ as signal, without ever asking "is this
Δ statistically meaningful?"

### The fix in one line

```
   for every iter, record:

       confidence = |score_after - score_before|  /  sigma

   where sigma = stdev of |score_after - score_before| across all
                 prior iters in this run (rolling 20-iter window)

   higher confidence = more likely to be real signal
   confidence < 1   = movement smaller than typical noise; suspect
   confidence > 2   = clearly outside noise; trust this evidence
```

Note the absolute value — a big regression is just as informative
("this change made things much worse, with confidence > 2") as a
big improvement. Both deserve to enter memory as strong evidence.

### What confidence is used for

Confidence is **not a gate.** Nothing gets blocked or reverted based
on it. Every iter merges to main regardless of confidence (that's
Bug B). Confidence is an annotation surfaced to the planner so it
can REASON about how trustworthy the run's evidence is.

```
   ┌─────────────────────────────────────────────────────────────┐
   │  iter 8  +0.333  conf=1.11  ●●●  (big win, trust)           │
   │  iter 9  -0.222  conf=0.74  ●●   (regression, suspect noise)│
   │  iter 13 +0.222  conf=0.74  ●●   (improvement, suspect)     │
   │  iter 15 +0.111  conf=0.37  ●    (tiny, likely noise)       │
   │  iter 19 +0.111  conf=0.37  ●    (tiny, likely noise)       │
   │  iter 22 +0.111  conf=0.37  ●    (tiny, likely noise)       │
   └─────────────────────────────────────────────────────────────┘
```

This sidesteps the threshold-paradox entirely. There's no scenario
where a real path from 0 → 0.7 → 1.0 gets blocked, because nothing
is blocked. Each step records its confidence, the planner reads them,
and the planner decides how much weight to give each one.

### How memory surfaces confidence to the planner

The planner intuition prompt currently has 7 sections (recent,
similar, category-stats, winners, open-hypotheses, error-curve,
cross-run). None of them today mention confidence.

The framework's job is to **surface a clean, structured view of the
high-confidence frontier** to the planner. Not to tell the planner
what to do — to give it the right evidence so it can reason properly.

The new section in the planner prompt looks like a researcher's
lab notebook organized by experiment quality:

```
─────────────────────────────────────────────────────────────────────
HIGH-CONFIDENCE EXPERIMENTS (conf > 1.0, sorted by score then conf)
─────────────────────────────────────────────────────────────────────

iter 8  · score 0.778 · conf 1.51 · category: preproc
    hypothesis: "render at 600 DPI to give Gemini denser pixels
                 per notehead, hopefully fixing the rhythm fixtures"
    outcome:    score went 0.444 → 0.778
    lesson:     "DPI bump from 300 to 600 reliably helps rhythm
                 fixtures. The pixel density improves vertical
                 notehead localization."
    diff:       eval/preproc.py: +12 lines (DPI param)

iter 1  · score 0.778 · conf n/a · category: prompt
    hypothesis: "a single fully-worked few-shot example matching
                 the evaluator's exact ground-truth format"
    outcome:    score went 0.000 → 0.778
    lesson:     "structural prompt scaffolding (one full XML
                 example) was the unlock. Zero-shot was wrong on
                 structure, not perception."
    diff:       prompts/page.txt: +47 lines

─────────────────────────────────────────────────────────────────────
LOW-CONFIDENCE WINS (conf < 1.0) — for contrast, weight modestly
─────────────────────────────────────────────────────────────────────

iter 13 · score 0.556 · conf 0.74 · category: preproc
    hypothesis: "per-staff cropping"
    note:       suggestive but might be noise — verify before
                building on this

iter 19 · score 0.556 · conf 0.50 · category: rerun
    hypothesis: "pin Gemini temperature to 0"
    note:       fragile; iter 24 contradicted via deterministic
                test — temperature=0 is NOT byte-deterministic

─────────────────────────────────────────────────────────────────────
NOISE FLOOR
─────────────────────────────────────────────────────────────────────
sigma (rolling 20-iter window): 0.218
last 10 iter confidences:        [0.4, 1.2, 0.3, 0.5, 0.4, 1.1, ...]
```

The planner now has the **whole record** for high-confidence
experiments — hypothesis, outcome, lesson, diff. It can do real
reasoning over it. Possible reasoning paths it could take:

```
   "both high-conf wins were structural interventions on a single
    failure mode. The low-conf wins were parameter sweeps. So my
    next experiment should target a specific named failure with a
    structural change, not another parameter tweak."

   "the rhythm fixtures unlocked with DPI; the structural-prompt
    iter unlocked everything. What's the analogous structural lever
    I haven't pulled? Probably the schema — Gemini's response_schema
    parameter for guaranteed XML structure."

   "low-conf wins cluster in the rerun category. Memory shows that
    category produces noise-level deltas. I should avoid it and try
    something I haven't explored — maybe verovio render flags."
```

These are possibilities, not directives. The planner does the
synthesis. The framework's job ends at presenting the evidence
cleanly: high-conf as primary, low-conf as contrast, noise floor
for context.

### The corresponding `winners` SQL

```sql
   -- high-conf wins (primary evidence)
   SELECT iter_num, category, hypothesis, score_before, score_after,
          confidence, lesson, commit_hash
   FROM iterations
   WHERE run_id=? AND confidence > 1.0
   ORDER BY score_after DESC, confidence DESC

   -- low-conf wins (contrast)
   SELECT iter_num, category, hypothesis, score_before, score_after,
          confidence, lesson, contradicted_by
   FROM iterations
   WHERE run_id=? AND kept = 1 AND confidence <= 1.0
   ORDER BY score_after DESC

   -- noise floor (for the NOISE FLOOR section)
   SELECT confidence FROM iterations
   WHERE run_id=? AND confidence IS NOT NULL
   ORDER BY iter_num DESC LIMIT 10
```

Diff content for each high-conf iter is fetched from
`artifacts.path` (the diff file we already record per iter) and
truncated to ~10 lines for the prompt — enough for the planner to
see the *kind* of change (preproc vs prompt vs schema), not the
exact code.

All three queries run on data the framework already records. The
only schema addition is the `confidence` column on iterations.

### Where sigma comes from

Sigma is computed live from data the framework already records.
Each completed iter has `(score_before, score_after)` in the DB.
The absolute delta is `|score_after - score_before|`. Take the
stdev of those values across past iters → that's sigma.

```
                            ROLLING SIGMA (computed every iter)
                            ──────────────────────────────────

       iter   score_before  score_after  |delta|
       ────   ────────────  ───────────  ───────
         1        0.000        0.778     0.778
         2        0.778        0.667     0.111
         3        0.667        0.444     0.222
         4        0.444        0.556     0.111
         5        0.556        0.500     0.056
         6        0.500        0.375     0.125
         7        0.375        0.444     0.069
         8        0.444        0.778     0.333
         9        0.778        0.556     0.222
                                          │
                                          ▼
                              |deltas| = [.778, .111, .222, .111,
                                          .056, .125, .069, .333,
                                          .222]
                              sigma    = stdev(|deltas|) = 0.218
```

This is "free" — no extra eval calls, just SQL on data we already
have. It updates every iter, so if the agent rewrites the pipeline
mid-run and noise behavior changes, sigma adapts within the rolling
window.

### What sigma actually means

```
   sigma is "the typical magnitude of score movement when an iter
   runs". It includes BOTH the noise floor (random Gemini variance
   on identical code) AND the typical real-change size (how much an
   average code edit moves the score).

   a single confidence number can't tell you which kind of movement
   you're seeing — but the planner doesn't need that distinction.
   it needs to know "is this iter's evidence stronger or weaker
   than what's typical for this run?"

   confidence = |delta| / sigma  answers exactly that.
```

### The chicken-and-egg at iter 1

We need ≥2 deltas to compute stdev. The first few iters get a special
"untyped" confidence label.

```
   iter 1     no prior deltas        confidence = "n/a"
   iter 2     1 delta                confidence = "n/a"
   iter 3+    ≥2 deltas              confidence = |delta| / sigma
```

The planner prompt includes the literal string `"n/a"` for these,
and lessons from those iters surface in memory queries with no
confidence weighting (they're treated neutrally).

### How it behaves by problem type

```
   sigma = 0   (deterministic pipeline)
       every iter has confidence = ∞ (any delta over zero
       noise is infinitely confident) — really treated as "high"
       in queries. Framework behaves as today.

   sigma = 0.05  (mildly noisy pipeline)
       a +0.10 win has confidence ≈ 2 — strong signal
       a +0.02 win has confidence ≈ 0.4 — likely noise

   sigma = 0.30  (very noisy, like music-omr)
       a +0.10 win has confidence ≈ 0.33 — likely noise
       a +0.45 win has confidence ≈ 1.5 — moderate signal
       a +0.7 → 1.0 jump (delta 0.3) has confidence ≈ 1.0 —
         borderline, planner sees it but weighs it modestly
```

Notice the last case: even with very noisy pipelines, a path like
0 → 0.7 → 1.0 still gets recorded and surfaced — just with
intermediate confidence. The planner sees that the late win was
"borderline" and may decide to verify by trying the same change
again. Nothing is lost.

### Edge case

```
   ┌──────────────────────────────────────────────────────────────┐
   │  rolling window vs all-time                                  │
   │                                                              │
   │  Use the LAST 20 |deltas|, not all of them. If the agent     │
   │  rewrites the pipeline at iter 30 to add voting, the noise   │
   │  drops — but if we keep computing sigma over all 30 |deltas|,│
   │  the old pre-voting noise still inflates sigma. A 20-iter    │
   │  rolling window means sigma re-converges to the new noise    │
   │  level within ~20 iters of any pipeline change.              │
   └──────────────────────────────────────────────────────────────┘
```

---

## Bug B — Keep/revert is the wrong abstraction

Real SGD does not have anchors. It just keeps stepping. The very
fact that some steps go uphill is what helps it escape saddle
points and shallow minima — if you forbid uphill moves, you get
stuck at the first local minimum you find.

The framework should do the same: every iter is a step. The agent
edits, the eval runs, we record the score. We don't revert. We
don't reset to a "best" state. The walk continues.

```
   ┌──────────────────┐
   │ ACT              │  agent edits files in main, runs eval,
   │ (one step in     │  commits the change. Score goes up or down.
   │  search space)   │
   └────────┬─────────┘
            │
            ▼
   ┌──────────────────┐
   │ record per iter: │
   │   score_after    │
   │   commit_hash    │
   │   delta vs prev  │
   └────────┬─────────┘
            │
            ▼
   ┌──────────────────────┐  loop continues from wherever
   │ next iter starts     │  the agent left things, NOT from
   │ from current state   │  any "best" snapshot
   └──────────────────────┘
```

### What pulls the loop back from bad regions

If we never reset, what stops the loop from walking forever into a
bad region of the search space? Two things, both already in the
framework:

```
   ┌──────────────────────────────────────────────────────────────┐
   │  1. memory pulls the planner back                            │
   │                                                              │
   │  the planner reads category_stats, winners, lessons. When    │
   │  recent iters in category X are all making things worse, the │
   │  planner sees it (via category avg-delta in the prompt) and  │
   │  shifts to a different category. The recovery happens at the │
   │  HYPOTHESIS level, not the codebase level.                   │
   └──────────────────────────────────────────────────────────────┘

   ┌──────────────────────────────────────────────────────────────┐
   │  2. early-stop on plateau                                    │
   │                                                              │
   │  if no iter has produced a new best_score_so_far in N iters  │
   │  (e.g. N=15), end the run. The user gets their best result.  │
   │  No automatic "restart" — the loop just stops because        │
   │  forward progress has stalled.                               │
   └──────────────────────────────────────────────────────────────┘
```

Both are passive recovery mechanisms. Neither overrides the agent's
exploration of the search space. `best_score_so_far` is recorded as
a passive metric — it tells the user where the run peaked — but the
loop itself doesn't consult it to make decisions.

### The mapping to actual SGD

```
   SGD                                autoresearch translation
   ────────────────                   ────────────────────────
   model weights                      codebase state at HEAD
   loss                               1.0 - score
   gradient step                      one ACT iter
   stochastic noise                   agent's pipeline non-determinism
                                        + planner's exploration choices
   no anchor / no rollback            no keep/revert, no reset to
                                        best_commit
   early stopping on validation       end run when no new best in
   plateau                              N iters
   best-checkpoint saving             record best_score_so_far +
                                        commit_hash as metadata, but
                                        the training loop never reads
                                        it
```

### What changes concretely

```
   1. Drop the keep/revert binary from the orchestrator.
      Every iter that completes ACT successfully merges to main.
      Every iter commits. The score moves up or down each iter.

   2. Drop git worktrees entirely.
      Worktrees only existed to support cheap reverts. With no
      reverts, the agent edits the main checkout directly and
      commits. Simpler architecture, fewer moving parts, no
      `git worktree prune` cleanup logic.

   3. Add to runs table (passive metadata only, not consulted by
      the loop):
        best_score_so_far  REAL
        best_commit        TEXT
        plateau_count      INTEGER  (iters since last best)

      Updated whenever score_after > best_score_so_far.

   4. Add to spec.md exit conditions:
        plateau_stop_after: 15    (default; iters without new best
                                   before ending the run)

   5. The `kept` column on iterations becomes meaningless and is
      dropped. Replace with `confidence` (REAL, computed from Bug A's
      |delta| / sigma at the time the iter ran) — purely descriptive,
      used by memory queries and the dashboard.

   6. The `winners` memory query becomes:
        SELECT * FROM iterations
        WHERE run_id=? AND score_after > ?
        ORDER BY confidence DESC, score_after DESC
        - high-confidence wins surface first
        - low-confidence wins still appear but visibly tagged so the
          planner can discount them
        - the planner uses this to decide what to try next; drift
          is fought via planner-side gravity (memory weighting),
          not codebase-side rollback
```

### Why dropping worktrees is right

Worktrees were a defensive structure for keep/revert: branch off
main, let the agent thrash, throw it away if the score didn't
improve. Without revert, that whole machinery is dead weight.

```
   today (worktrees)                       proposed (single repo)
   ─────────────────                       ──────────────────────
                                           ┌─────────────────────┐
   ┌─────────┐                             │ main                │
   │ main    │                             │  ↓ commit (iter 1)  │
   └────┬────┘                             │  ↓ commit (iter 2)  │
        │ branch                           │  ↓ commit (iter 3)  │
        ▼                                  │  ↓ ...              │
   ┌─────────┐    revert?  rm -rf          │                     │
   │worktree │ ─────────────────▶ gone     │ every iter is just  │
   │iter-7   │    keep?    merge --ff-only │ a commit on main    │
   └────┬────┘                             └─────────────────────┘
        │
        ▼
   merged to main
```

(Concession: if we ever want N parallel iters exploring different
categories simultaneously, worktrees come back. For now the loop is
serial, so they're overhead.)

---

## What the run would look like with these 2 fixes

```
                         WITHOUT FIXES (actual)         WITH FIXES (predicted)
                         ──────────────────────         ───────────────────────
score                                                   score
1.0 ┤                                                   1.0 ┤  best_score_so_far
0.8 ┤ ●─●                                               0.8 ┤ ●─━━━━━━━━━━━━━━●
    │    \             ●                                    │  \              .
0.6 ┤     ●─●─●     ●─● \         ●─●                   0.6 ┤   ●●   ●─●     ●─●
    │           \  /     \      /     \                     │      \/   \   /
0.4 ┤            ●●       ●─●─●●       \─●─●            0.4 ┤       ●    ●─●     ← walk continues
    │                                                       │
0.2 ┤                                                   0.2 ┤                    ← ends when no
    │                                                       │                      new best for
    └────────────────────────────────                       └────────────────────  N iters
      1  5  10  15  20  25                                    1  5  10  15  20

   iter 25 sat at 0.444, -0.33                          best_score_so_far stays at
   below the high-water mark.                           iter 1's 0.778 throughout
   Reflector kept noise-driven                          the run as a passive
   wins, lessons polluted memory,                       record. Iters keep walking
   loop drifted downward.                               (always merge), but their
                                                        low confidence values
                                                        (≤1) tell the planner to
                                                        weight that evidence
                                                        modestly. Memory stays
                                                        useful. Run early-stops
                                                        when no new best for N
                                                        iters.
```

---

## Tying back to Track B (regex-engine, currently running)

```
         music-omr (Track A)              regex-engine (Track B)
         ───────────────────              ──────────────────────
         pipeline calls Gemini            pipeline is pure Python
         (per-call XML differs)           (per-call output identical)
              │                                    │
              ▼                                    ▼
         eval is deterministic            eval is deterministic
         BUT input is noisy               AND input is deterministic
              │                                    │
              ▼                                    ▼
         per-iter score varies            per-iter score is FULLY
         even with same code              determined by code state
              │                                    │
              ▼                                    ▼
         Bugs A AND B both apply           Bug B (drop revert) applies
         - sigma will be large             - sigma is ~0
         - confidence values cluster       - confidence is essentially
           ≤1 for noisy iters                infinite (any positive
         - planner reads memory and          delta is meaningful)
           weights low-confidence          - Bug A degenerates cleanly:
           lessons less                      every iter is high-conf,
                                             memory queries unchanged
```

Predictions for regex-engine:

- regex should converge to 1.0 cleanly. Each iter implements one
  more feature (atoms → quantifiers → groups → alternation → NFA
  for pathological cases) and the score steps up monotonically. If
  the framework lets that happen without polluting memory or
  reverting good iters, both bugs are confirmed not to bite on
  deterministic problems.
- if regex DOES drift (kept iter at 0.7 followed by reverted iters
  walking score down), Bug B's "always merge" model is necessary
  even for deterministic problems.

---

## Recommendation

1. Wait for the current control-problem runs (regex-engine finished
   in 1 iter; hot-cold-draw is the active test).
2. Implement Bug B (drop keep/revert, drop worktrees, always merge,
   early-stop on plateau) **first**. Bigger architectural change but
   structurally simpler than today's framework.
3. Implement Bug A (rolling-sigma confidence annotation) **second**.
   Cheap to add once Bug B is in. Only adds a `confidence` column on
   iterations and changes how memory queries weight evidence; loop
   machinery is unchanged.
4. Re-run music-omr on the fixed framework. Predict
   `best_score_so_far` reaches iter 1's 0.778 immediately, then
   either climbs further or holds while the loop walks looking for
   another real win. Run early-stops when no new best for 15 iters.
