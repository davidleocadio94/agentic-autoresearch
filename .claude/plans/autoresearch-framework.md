# Generalized Autoresearch Framework — Brainstorm

> Framework spec. NOT yet implementation. The agentic-sheet-music project
> stays as a problem fixture — the framework consumes its problem spec
> and spits out a working repo.

---

## The thesis

```
PROBLEM SPEC                                     WORKING REPO
(declarative: what's the eval,                  (executable: passes
what's the budget, what's              ──────▶  the eval, satisfies
the tech stack)                                  guardrails)

                  ┌──────────────────────────┐
                  │   AUTORESEARCH FRAMEWORK │
                  │   (generic, problem-     │
                  │    agnostic, reusable)   │
                  └──────────────────────────┘
```

```
plug in   sheet-music problem        ──▶ framework writes the OMR repo
plug in   slide-converter problem    ──▶ framework writes the slide repo
plug in   any problem with eval+caps ──▶ framework iterates until done
```

The framework is the product. Problems are inputs.

---

## What "a problem" looks like

**One file plug-in: `spec.md`.** Everything else (eval contract,
constraints, exit conditions) is expressed inside that file in
front-matter or marked-up sections. The framework parses it.

```
problems/<name>/
└── spec.md         ◀── the ONLY thing the user must write
```

```
spec.md
─────────────────────────────────────────────────────
---
name: sheet-music
exit:
  target_score: 1.0
  max_hours: 24
  max_iters: 200
constraints:
  language: python
  manager: uv
  forbid_gui: true
  max_repo_bytes: 2_000_000_000
  forbidden_deps: [selenium, playwright]
  immutable_paths: [eval/score.py, eval/fixtures/**]
eval:
  kind: per-fixture-exact-match     # framework-provided template
  fixtures_glob: eval/fixtures/**/ground-truth.musicxml
  scorer: eval/score.py             # OR a built-in template name
---

# What we're building

PDF in → MusicXML out, scored by per-measure exact match against
ground truth.

# How to evaluate

(framework-provided template "per-fixture-exact-match" + the
scorer module declared above)

# Optional starter notes

Tried before: Audiveris (lossy), then pivoted to Gemini Vision.
Current best: 55.6%.
```

```
WHAT THE FRAMEWORK GETS FROM ONE FILE
──────────────────────────────────────
front matter   → exit conditions, constraints, eval kind
"# What"       → human charter, passed to PLANNER agent
"# Optional"   → seed context
eval pointer   → either built-in template name or path to score.py

NO eval/ directory required for trivial problems
   (string-match / regex / unit tests live in spec.md too)
ADD an eval/ directory only when you have data fixtures
```

The framework reads `spec.md` and runs.

---

## The control loop — Plan / Act / Reflect

```
┌────────────────────────────────────────────────────────────────────┐
│  ORCHESTRATOR (parent process)                                     │
│                                                                    │
│   while not exit:                                                  │
│   ┌──────────┐    ┌──────────┐    ┌──────────┐    ┌──────────┐     │
│   │   PLAN   │ ─▶ │   ACT    │ ─▶ │ REFLECT  │ ─▶ │ ENFORCE  │     │
│   └──────────┘    └──────────┘    └──────────┘    └──────────┘     │
│        │              │              │              │              │
│        ▼              ▼              ▼              ▼              │
│  ┌────────────────────────────────────────────────────────────┐    │
│  │  PERSIST every transition to Postgres                      │    │
│  │  (on crash, resume from last persisted phase)              │    │
│  └────────────────────────────────────────────────────────────┘    │
│                                                                    │
└────────────────────────────────────────────────────────────────────┘
```

### PLAN — what to try this iteration

```
INPUT                                OUTPUT
─────                                ──────
- problem spec.md                    JSON hypothesis:
- last 5 iter outcomes from DB        {
- top 10 iters semantically            "category": "preproc",
  similar to "what should we           "rationale": "...",
  try next" via pgvector               "expected_delta": "+5%",
- error curve per fixture              "files_to_touch": [...],
- known constraints                    "estimated_runtime_min": 8
- exit conditions                    }
```

```
THIS PHASE LIVES OR DIES BY MEMORY
───────────────────────────────────
the PLANNER doesn't read code. it reasons over past experiments.
postgres serves three queries here:

  Q1. RECENT  — "last 5 iters in this run"
                SELECT * FROM iterations
                WHERE run_id = $r
                ORDER BY iter_num DESC LIMIT 5

  Q2. SIMILAR — "past iters semantically near my draft hypothesis"
                SELECT * FROM iterations
                WHERE embedding <=> $draft_embedding
                ORDER BY similarity LIMIT 10
                (cross-run by default — learn from old problems too)

  Q3. CATEGORY-STATS — "are we plateauing in any category?"
                SELECT category, AVG(score_after - score_before)
                FROM iterations WHERE run_id = $r
                GROUP BY category

  Q4. OPEN-HYPOTHESES — "anything I proposed but never tried?"
                SELECT proposed_next FROM iterations
                WHERE run_id = $r AND proposed_next IS NOT NULL
                EXCEPT (matched against current iter list)

PLANNER calls these (via tool) before deciding.
returns a hypothesis row → ACT picks it up.
```

### ACT — implement the hypothesis and measure it

```
"ACT" = "do the experiment in code, then run the eval."

step-by-step:
─────────────
1. orchestrator creates a git worktree:
      git worktree add .worktrees/iter-042 main
   the agent's cwd for this call = the worktree dir.

2. spawn an agent in that worktree:
      claude -p "<hypothesis JSON + ACT instructions>" \
        --dangerously-skip-permissions \
        --output-format stream-json

3. agent reads the hypothesis, edits code in the worktree:
      Edit src/.../gemini_omr.py
      Write src/.../new_helper.py
      Bash uv add pillow
      Bash uv run eval --json runs/<id>/iter_042.json

4. agent commits inside the worktree:
      git -C .worktrees/iter-042 add -A
      git -C .worktrees/iter-042 commit -m "[preproc] try X"

5. agent exits with the commit hash + score on stdout.

KEY POINT: the parent repo's main branch is untouched until
REFLECT decides to merge.
```

```
WHY GIT WORKTREES (instead of `git reset --hard`)
─────────────────────────────────────────────────
TODAY'S WORLD                      WORKTREE WORLD
─────────────                      ──────────────
agent edits the main repo          agent edits an isolated copy
                                   at .worktrees/iter-042/
on bad eval:                       on bad eval:
  git reset --hard pre              rm -rf .worktrees/iter-042/
  git clean -fd                     git worktree prune
                                   ↑ atomic, no chance of leaving
  ↑ can leak files,                  cruft in main
    can clobber agent's
    in-progress doc                main repo always stays clean
                                   parallel iters are easy
                                   (different worktrees, no
                                    contention)
```

### REFLECT — synthesize the outcome

REFLECT also reads from memory. The reflector isn't just "did the
score go up?" — it's "what does this teach us, framed against past
attempts?"

```
INPUT                                OUTPUT
─────                                ──────
- hypothesis JSON                    JSON outcome:
- eval JSON (per-fixture scores)      {
- git diff against main                "kept": true|false,
- the agent's stream-json log          "what_happened": "...",
- pg_query tool                        "lesson": "...",
                                       "proposed_next": "..." | null,
                                       "supersedes_iters": [3,7,12]?,
                                       "contradicts_iters": [4]?,
                                       "embedding": [...]
                                     }
```

```
WHY REFLECT NEEDS MEMORY TOO
────────────────────────────
the reflector queries the same DB the planner does, but for
DIFFERENT questions:

  1. "is this lesson NEW or REDUNDANT?"
        SELECT * FROM iterations
        ORDER BY lesson_embed <=> embed($my_lesson) LIMIT 5
        if top match has cosine > 0.9 → write a shorter lesson,
          flag this iter as "confirms iter K"

  2. "does this iter SUPERSEDE an old proposed_next?"
        SELECT iter_num, proposed_next FROM iterations
        WHERE run_id=$r AND proposed_next IS NOT NULL
          AND proposed_next ~~* (parts of $hypothesis)
        → mark those as resolved

  3. "does this CONTRADICT a prior 'works' claim?"
        SELECT * FROM iterations
        WHERE run_id=$r AND kept=true
          AND lesson_embed <=> embed($my_lesson) > 0.85
          AND score_delta * $my_score_delta < 0
        → write a stronger lesson, flag the contradiction

  4. "is the per-fixture pattern unprecedented?"
        compare per_fixture_scores against last 10 iters' jsonb;
        if a never-broken fixture suddenly broke → URGENT lesson
        if a never-fixed fixture suddenly passed → URGENT lesson

  5. "what category-level summary does this push toward?"
        SELECT category, AVG(score_delta) WHERE run_id=$r
          GROUP BY category
        → "preproc category is now -2% over 5 iters; recommend
           planner avoid for at least 3 turns"
        (this becomes part of the proposed_next)
```

```
ORCHESTRATOR'S POST-REFLECT WORK
────────────────────────────────
if outcome.kept:
  git -C .worktrees/iter-042 push origin worktree-branch
  cd main → git merge --ff-only worktree-branch
else:
  the worktree is discarded (rm -rf + git worktree prune)
  the branch lingers as "iter-042-failed" for forensics

embed both:
  hypothesis_embed = embed(hypothesis.rationale)
  lesson_embed     = embed(outcome.lesson)
store in iterations row.

if outcome.supersedes_iters:
  UPDATE iterations SET superseded_by=$this WHERE id IN ($list)

if outcome.contradicts_iters:
  UPDATE iterations SET contradicted_by=$this WHERE id IN ($list)
  ↑ the dashboard surfaces these as "live debates"
```

### ENFORCE — pre/post invariants

```
quick deterministic check (no agent):
  - constraints.immutable paths unchanged
  - constraints.forbidden_deps not present
  - repo size still under cap
  - no bypass-permission file edits outside the worktree

if violated:
  unmerge, log violation row, mark iter as FAILED-GUARDRAIL
  PLANNER's next call will see it → won't retry same path
```

---

## How agents are spawned and communicate

```
                ORCHESTRATOR
                (long-running Python)
                       │
        ┌──────────────┼──────────────┐
        ▼              ▼              ▼
   ┌─────────┐    ┌─────────┐    ┌──────────┐
   │ PLANNER │    │  ACTOR  │    │REFLECTOR │
   │         │    │         │    │          │
   │ tool:   │    │ runs in │    │ reads    │
   │ pg_query│    │ git     │    │ diff +   │
   │ returns │    │ wktree, │    │ eval;    │
   │ JSON    │    │ commits │    │ embeds   │
   │ hypoth. │    │ + score │    │ outcome  │
   └─────────┘    └─────────┘    └──────────┘
        │              │              │
        ▼              ▼              ▼
            Postgres  +  files

ALL AGENTS = `claude -p` running headless with
             --dangerously-skip-permissions
             --output-format stream-json
```

```
SPAWN
─────
claude -p "<role-specific prompt + JSON inputs>" \
  --dangerously-skip-permissions \   ← no permission prompts
                                       at all (loop is autonomous)
  --output-format stream-json \      ← one JSON event per line so
                                       observability can replay it
  --max-turns 40

COMM (between orchestrator and agent)
─────────────────────────────────────
input  = the prompt string + paths to JSON-on-disk files the
         agent reads
output = stdout = stream of {tool_use, message, result} events
final  = the agent's last message OR a structured
         "outcome.json" the agent wrote to a known path

ISOLATION
─────────
each ACT call:
  - cwd = .worktrees/iter-NNN/
  - has its own git history (a branch named iter-NNN)
  - cannot touch eval/ or fixtures/ (immutable_paths enforced)
  - the worktree is deleted at end of iter, kept or merged

no `git reset --hard` ever. failures = `rm -rf` the worktree.
```

---

## Memory — building intuition about the problem

```
TODAY                         PROPOSED
─────                         ────────
eval-runs/iter_*.md           Postgres + pgvector (single DB)
eval-runs/MEMORY.md           + iter_*.md still written to disk
                                 (forensics + git history)

flat read,                    the agent QUERIES memory:
no targeted query             - SQL for structural facts
                              - vector for semantic fuzzy match
                              - aggregations for trends
```

### What's IN the iterations table — both columns and embeddings

```
COLUMN                  TYPE       MEANING
──────                  ────       ───────
id                      uuid       primary key
run_id                  uuid       which run (FK)
iter_num                int        sequential within run
parent_iter_id          uuid       null at iter 1; otherwise prev iter
                                   (lets us reconstruct the experiment
                                    tree even when iters branch)
phase                   enum       planning|acting|reflecting|done|failed
category                text       prompt|preproc|chunk|schema|rerun|...
hypothesis              text       the PLANNER's rationale
                                   ("try cropping each measure separately
                                    because line-vs-space confusion")
hypothesis_embed        vector     embed(hypothesis) — 1536-dim
files_touched           text[]     paths the ACTOR planned to edit
commit_hash             text       worktree's commit (null if reverted)
score_before            float      score going in
score_after             float      score after eval (null if eval failed)
score_delta             float      computed
per_fixture_scores      jsonb      {"01-pitch/...": 1.0, "02-..": 0.5}
duration_seconds        int        wall clock for ACT
api_cost_usd            float      from claude usage
kept                    boolean    did REFLECT merge it?
lesson                  text       the REFLECTOR's one-paragraph takeaway
                                   ("doubling image size makes Gemini's
                                    503 rate spike — cap at 2× upscale")
lesson_embed            vector     embed(lesson)
proposed_next           text       agent's "try X next" suggestion
                                   (or null)
guardrail_violations    int        count
created_at              timestamp
```

```
TWO EMBEDDINGS PER ROW — INTENTIONALLY
──────────────────────────────────────
hypothesis_embed  ← what we WANTED to try
lesson_embed      ← what we LEARNED

different queries answer different questions:

   PLANNER drafting iter 42's hypothesis:
     SELECT * FROM iterations
     ORDER BY hypothesis_embed <=> $my_draft LIMIT 10
     ↑ "have we tried things like this before?"

   PLANNER asking about a specific failure mode:
     SELECT * FROM iterations
     ORDER BY lesson_embed <=> embed("octave reading is brittle on big jumps")
     LIMIT 10
     ↑ "what have we LEARNED about this?"
```

### The PLANNER's "intuition queries" — beyond `last 5 iters`

```
the PLANNER agent has a pg_query tool. it runs whichever of these
it needs to plan iter N. the framework doesn't pre-decide; the
agent decides.

──────────────────────────────────────────────────────────────────
1. recent context
   "what just happened?"
   → SELECT * FROM iterations WHERE run_id=$r ORDER BY iter_num DESC LIMIT 5

2. similarity check
   "have we tried THIS before?"
   → SELECT * FROM iterations
     ORDER BY hypothesis_embed <=> embed($draft_hypothesis) LIMIT 10

3. failure-mode lookup
   "what does memory say about THIS failure pattern?"
   → SELECT lesson FROM iterations
     ORDER BY lesson_embed <=> embed("Gemini reads C5 as F5") LIMIT 10

4. category exhaustion
   "are we plateauing in any category?"
   → SELECT category,
            COUNT(*) FILTER (WHERE kept) AS wins,
            AVG(score_delta) AS avg_delta
     FROM iterations WHERE run_id=$r GROUP BY category

5. winners-only
   "what HAS worked? show the diffs."
   → SELECT iter_num, category, hypothesis, score_delta, commit_hash
     FROM iterations WHERE run_id=$r AND kept ORDER BY iter_num

6. open hypotheses
   "what did past iters propose but no one tried?"
   → SELECT iter_num, proposed_next FROM iterations
     WHERE run_id=$r AND proposed_next IS NOT NULL
     EXCEPT (already-attempted set)

7. error-curve regression
   "which fixture got worse since iter K?"
   → SELECT fixture, scores
     FROM (UNNEST per_fixture_scores) ...
     WHERE score now < score at iter K

8. cross-run learning (opt-in, v2)
   "in any prior RUN on a similar problem, what worked?"
   → SELECT lesson FROM iterations
     WHERE run_id != $r AND kept
     ORDER BY hypothesis_embed <=> embed($current_problem_summary)
     LIMIT 10
──────────────────────────────────────────────────────────────────

THE INTUITION COMES FROM CHAINING THESE
───────────────────────────────────────
PLANNER, plausible internal monologue:
  "let me see (1) what we just tried.
   ok, lots of regressions in `chunk`.
   let me check (4) — yes, chunk has -3% avg over 4 iters, exhausted.
   let me try (6) — there's a proposed `per-measure crop` idea
     unused since iter 7.
   let me check (2) — has anyone tried per-measure crops? no.
   let me check (3) — what does memory know about per-measure crops?
     three iters mention measure-level processing as a hypothesis
     but no one ran it.
   ok, that's my next experiment."
```

### Other tables

```
runs                   one row per autoresearch run
 ├ id, problem_name, started_at, status, exit_reason
 └ final_score, total_cost_usd

artifacts              blobs (diffs, eval JSON, agent stream-json)
 ├ iter_id, kind, path, content_hash, size_bytes

guardrail_violations
 ├ iter_id, kind, detail, action_taken (rolled-back?)

processes              for resume + dashboard
 ├ pid, role, started_at, last_heartbeat_at, host
```

### Local install footprint

```
v1: sqlite + sqlite-vec extension
    zero install, single file, perfect for one-laptop research
    "open this file in DataGrip" → debug

v2 (only if needed): docker compose up postgres pgvector
    same SQL surface, scales to many runs in parallel,
    accessible from a remote dashboard

both options ship with the framework. user picks via spec.md.
```

---

## Observability — open the hood

Today: `tail -f eval-runs/loop.log`. Insufficient.

```
TODAY                                       PROPOSED
─────                                       ────────
1MB stream-json scattered                   web UI on localhost:8765
across iter_*.agent.log                     reads same Postgres the
                                            loop writes to
score history in CSV
                                            SQL + vector search
no way to see thinking                      live agent stream replay
4 hours ago                                 side-by-side iter compare
```

```
ROUTES
──────
/                 ← run list + which run is currently live
/health           ← machine-readable health JSON
/run/<id>         ← score curve, iter table, live progress card
/iter/<id>        ← hypothesis, diff, eval, replay
/search?q=...     ← pgvector across all iters
/errors/<run>     ← per-fixture error curves
/db               ← raw memory browser (tables + ad-hoc SQL)

minimal stack: FastAPI + htmx, ~400 lines, no SPA build
auto-opens at http://localhost:8765 when the loop starts
```

### `/` — live status card (always visible while running)

This is the page you leave open in a tab while the loop works
overnight. Every section refreshes every 2 seconds via htmx polling.

```
┌────────────────────────────────────────────────────────────────┐
│  AUTORESEARCH — sheet-music-omr            ● running           │
├────────────────────────────────────────────────────────────────┤
│  iter 42 / ∞   started 14:03 (3m 22s ago)   PHASE: ACT         │
│                                                                │
│  hypothesis (from PLANNER):                                    │
│  ┌──────────────────────────────────────────────────────────┐  │
│  │ category: preproc                                         │ │
│  │ rationale: try cropping each measure separately           │ │
│  │ because line-vs-space confusion gets worse on full pages  │ │
│  │ files_to_touch: src/.../gemini_omr.py                     │ │
│  │ estimated_runtime_min: 8                                  │ │
│  └──────────────────────────────────────────────────────────┘  │
│                                                                │
│  current activity (last agent event, 4s ago):                  │
│  ┌──────────────────────────────────────────────────────────┐  │
│  │ TOOL: Bash                                                │ │
│  │ uv run eval --json runs/abc/iter_042.json                 │ │
│  └──────────────────────────────────────────────────────────┘  │
│                                                                │
│  health:                                                       │
│   ✓ orchestrator heartbeat: 12s ago                            │
│   ✓ active claude pid 71234, 3m 18s elapsed                    │
│   ✓ db reachable (sqlite, 18.4 MB)                             │
│   ✓ guardrails clean (last check 31s ago)                      │
│   ⚠ 1 worktree pending merge decision                          │
│                                                                │
│  budget:                                                       │
│   wall-clock        2h 14m / 8h                                │
│   iters this run    42  (target: until score=1.0)              │
│   api $              $4.21 spent                               │
└────────────────────────────────────────────────────────────────┘
```

### `/run/<id>` — what's happening this run

```
┌────────────────────────────────────────────────────────────────┐
│  RUN abc-123 (sheet-music-omr)             ● live              │
├────────────────────────────────────────────────────────────────┤
│  SCORE OVER ITERATIONS              ERROR CURVE PER FIXTURE    │
│  ────────────────────                ─────────────────────────  │
│  100% │  ★ target            1.0 ┤ ●─●─●─●  fixture A          │
│   80% │                          │           ●  fixture B       │
│   60% │ ●●● ● ● ● ●          0.5 ┤   ●           ●  fixture C   │
│   40% │●                         │      ●─●─●─●     fixture D   │
│   20% │                          │ ●  ●  ●  ●  ●               │
│        └──────────────       0.0 ┤   fixture E (broken)         │
│        1   10  20  30 ...        └────────────────              │
│                                    1   10  20  30 ...           │
│                                                                │
│  RECENT ITERATIONS                                             │
│  ┌────────┬──────────┬─────────┬────────┬──────────────────┐   │
│  │ iter   │ category │ Δ score │ kept?  │ hypothesis       │   │
│  ├────────┼──────────┼─────────┼────────┼──────────────────┤   │
│  │ 42 ●●● │ preproc  │  ?      │ acting │ per-measure crop │   │
│  │ 41     │ chunk    │ -0.12   │ ✗      │ multi-image      │   │
│  │ 40     │ prompt   │ +0.05   │ ✓      │ explicit ledger  │   │
│  │ 39     │ rerun    │ +0.00   │ ✗      │ N=5 instead of 3 │   │
│  │ 38     │ preproc  │ +0.07   │ ✓      │ DPI 400          │   │
│  └────────┴──────────┴─────────┴────────┴──────────────────┘   │
│                                                                │
│  CONTRADICTIONS / OPEN-DEBATES                                 │
│  iter 23 says "DPI > 300 hurts"                                │
│  iter 38 says "DPI 400 helped" ← contradicts                   │
│  → click to see both diffs side-by-side                        │
└────────────────────────────────────────────────────────────────┘
```

### `/iter/<id>` — the work, replayable

```
top of page:
  hypothesis card (same as live status)
  score before / after / per-fixture breakdown
  diff vs main (collapsible)

middle:
  AGENT REPLAY — scrubbable timeline of stream-json events
   ┌────────────────────────────────────────────────────────┐
   │ ◀─── [============●==============================] ──▶ │
   │       0:00                                       3:18  │
   │                                                        │
   │ at this point in time:                                 │
   │   PLANNER said: "let's try X..."                       │
   │   tool: Read src/gemini_omr.py                         │
   │   tool: Edit src/gemini_omr.py                         │
   │   ...                                                  │
   └────────────────────────────────────────────────────────┘

bottom:
  RELATED ITERS (auto-surfaced from pgvector search):
   - iter 23 (similar hypothesis, opposite outcome)
   - iter 17 (proposed this idea but never tried)
```

### `/health` — machine-readable

```
GET /health
{
  "orchestrator": {
    "alive": true,
    "pid": 4521,
    "heartbeat_age_seconds": 12,
    "started_at": "2026-04-30T18:03:00Z"
  },
  "current_iter": {
    "id": "uuid",
    "iter_num": 42,
    "phase": "acting",
    "hypothesis_summary": "per-measure crop",
    "phase_started_at": "...",
    "agent_pid": 71234,
    "last_agent_event_at": "...",
    "stalled_seconds": 4
  },
  "budget": {
    "elapsed_hours": 2.23,
    "max_hours": 8,
    "iters_this_run": 42,
    "api_cost_usd": 4.21
  },
  "db": {
    "kind": "sqlite",
    "path": "runs/abc-123/memory.db",
    "size_mb": 18.4
  },
  "warnings": ["worktree iter-039 not yet pruned"]
}
```

A bash one-liner can grep this for `phase` to know if we're stuck.

### `/db` — raw memory browser

```
opens a stripped-down DataGrip-like view:
  - left sidebar: tables (runs / iterations / artifacts / ...)
  - main pane: scrollable rows, click to expand
  - top: a SQL textarea for ad-hoc queries
  - bottom: a "common queries" picker:
      • "latest iter doc" → auto-runs SQL
      • "all kept iters" → auto-runs SQL
      • "this run's contradictions" → auto-runs SQL
      • "vector search over lessons" → text input + similarity slider

read-only by default. you can flip a toggle to allow UPDATE/DELETE
  with a "are you sure" dialog. useful for clearing a corrupted iter
  without writing migration code.
```

### Score and error curves on the dashboard

Two plots, side by side, updated every iteration:

```
SCORE OVER TIME                            ERROR CURVE (per fixture)
───────────────                            ─────────────────────────

100% │              ★ target               1.0 ┤  ●─────●  fixture A
 90% │                                         │           ●  fixture B
 80% │                                     0.5 ┤  ●           ●  fixture C
 70% │            ●                            │     ●─────●─────●
 60% │       ●─────  ●                         │           ●  fixture D
 50% │   ●●●                                   │
 40% │ ●                                       │  ●  ●  ●  ●  ●
 30% │                                         │  fixture E (broken)
 20% │                                     0.0 ┤
     └───────────────                          └────────────────
       1  3  5  7  9 ...                         1  3  5  7  9 ...
       iteration                                 iteration
```

```
WHAT THE ERROR CURVE TELLS YOU THAT THE SCORE CURVE DOESN'T
───────────────────────────────────────────────────────────
- which fixture is regressing while others improve
- which fixture is "stuck" at zero (never been solved)
- whether a category of failure (e.g. "all rhythm fixtures")
  responds together to a single experiment
- whether the average score is masking a tradeoff
   (5 fixtures up 10%, 1 fixture down 50% → avg looks ok,
    error curve shows the real story)

PLOTTED BY DEFAULT
──────────────────
- one line per fixture, sorted by total error
- second-derivative annotations: green dot for "newly solved",
  red dot for "newly broken"
- separate panel: error breakdown by category (pitch, rhythm,
  meter, key) for problems that opt in
```

```
FILES (also written so you can `jq` if you don't want a UI)
────────────────────────────────────────────────────────────
runs/<id>/events.jsonl       structured events, append-only
runs/<id>/score.csv          score per iter, parseable
runs/<id>/errors.csv         per-fixture per-iter, parseable
runs/<id>/score-plot.png     same as UI, for sharing
runs/<id>/error-plot.png     same as UI, for sharing
```

The principle: **everything an agent did or thought is inspectable
later, in seconds. No archaeology in stream-json files.**

---

## Guardrails — declarative, enforced

Today: hardcoded in `snapshot.py`. Each new problem needs a fork.
Bad pattern.

```
constraints.toml (per problem)
─────────────────────────────────────────────────────
[stack]
language          = "python"
allowed_managers  = ["uv"]
forbid_gui_apps   = true

[deps]
allowed_extra     = ["music21", "verovio", "pymupdf"]
forbidden         = ["selenium", "playwright"]

[size]
max_repo_bytes              = 2_000_000_000
max_runtime_install_bytes   =   500_000_000

[immutable]
paths = ["eval/score.py", "eval/fixtures/**"]

[network]
allowed_hosts = ["api.gemini.google.com",
                 "*.anthropic.com"]
```

```
ENFORCEMENT
───────────
framework's snapshot module reads constraints.toml
runs before + after every iter
any drift → git reset --hard, or worktree discard

NO problem-specific code in the framework.
problem-specific limits live in the problem.
```

---

## Crash resilience — graceful exit + resume

Today's failure mode: lid close → laptop sleep → SSL socket dead →
loop hangs forever in `_ssl__SSLSocket_read`. We find the PID,
manually kill it, lose the iter. Five iterations wasted overnight.

### The fix in one sentence

```
Every iteration's state lives in the database, not in a Python
process. The Python process is just a temporary worker that picks
up wherever the database said to start.
```

### Walking through each crash scenario

```
SCENARIO A — Ctrl-C in the foreground
──────────────────────────────────────
SIGINT → orchestrator's signal handler:
  1. mark current iteration row: status="interrupted",
     interrupted_at=now()
  2. send SIGTERM to active claude subprocess (if any)
  3. wait up to 30s for clean exit, then SIGKILL
  4. mark process row: ended_at=now()
  5. exit 0

ON RESUME:
  read iterations WHERE status="interrupted" → mark "abandoned"
  start a new iteration from current code state


SCENARIO B — laptop sleep mid-Gemini-call
──────────────────────────────────────────
WHEN SLEEP HITS:
  socket pauses, Python thread parked, no signals, no logging
  the orchestrator's heartbeat row stops updating

WHEN LID OPENS:
  the active SSL read STILL HANGS (the socket is dead but Python
  doesn't know yet)

DETECTION (without polling):
  every external call has a hard deadline:
    HTTP/SSL    → connect_timeout=10s, read_timeout=120s
    subprocess  → preexec_fn sets PR_SET_PDEATHSIG so children
                  die when parent dies; explicit timeout= on .wait
    sigalrm     → orchestrator's main thread wakes every 30s and
                  checks "did my child make progress?" if not,
                  send SIGTERM; if still no, SIGKILL

once the SSL call dies (timeout), normal control flow resumes:
  → REFLECTOR sees no eval result
  → outcome row marked status="failed" reason="api_timeout"
  → orchestrator increments iter and continues


SCENARIO C — orchestrator process itself dies (OOM, crash, kill -9)
────────────────────────────────────────────────────────────────────
the database has the source of truth. on next `autoresearch run`:

  1. find rows: status IN ('planning','acting','reflecting')
     → these are interrupted iters from the prior process
  2. for each: check process registry — is the Python pid alive?
     - alive: someone else is running. exit with "already running"
     - dead: claim the iter, mark status='abandoned'
  3. start new iter from current code state (whatever last
     successful merge produced)

NO manual intervention. NO loop.pid hacks.


SCENARIO D — a single agent call hangs
───────────────────────────────────────
each agent call has its own deadline:
  PLANNER   ≤  5 min hard timeout
  ACTOR     ≤ 60 min hard timeout (eval can be slow)
  REFLECTOR ≤  3 min hard timeout

  + a 45-min-of-no-stdout-activity watchdog (today's design,
    proven to work; agent stream-json keeps log file growing)

if killed: phase row marked status='failed_timeout'.
  PLANNER fails  → skip iter, try again next loop tick
  ACTOR fails    → REFLECT runs against partial state, marks
                   iter as failed, no merge
  REFLECT fails  → use deterministic fallback synthesis (just
                   record score delta + diff stat)


SCENARIO E — git worktree corruption
─────────────────────────────────────
the worktree IS the agent's sandbox.
on any phase exit (clean or crash):
  if worktree dir exists AND iter status != 'kept':
    rm -rf .worktrees/iter-NNN/
    git worktree prune
  if iter status == 'kept' AND not yet merged:
    merge happens atomically in REFLECT phase
    (in main repo, fast-forward only)
```

### State-machine view of an iteration

```
                           ┌──────────────┐
              ┌───────────▶│  pending     │
              │            │ (just made   │
              │            │  the row)    │
              │            └──────┬───────┘
              │                   │
              │                   ▼
              │            ┌──────────────┐
              │            │  planning    │
              │            └──────┬───────┘
              │                   │
              │            ┌──────┴───────┐
              │            ▼              ▼
              │     ┌──────────────┐   ┌────────────────┐
              │     │  acting      │   │ failed_planning│
              │     │ (worktree    │   └────────────────┘
              │     │  alive)      │
              │     └──────┬───────┘
              │            │
              │     ┌──────┴───────┐
              │     ▼              ▼
              │  ┌──────────┐  ┌──────────────┐
              │  │reflecting│  │ failed_acting│
              │  └────┬─────┘  └──────────────┘
              │       │
              │  ┌────┴──────┐
              │  ▼           ▼
              │ ┌──────────┐ ┌──────────────┐
              │ │ kept     │ │ reverted     │
              │ │ (merged) │ │ (worktree    │
              │ └──────────┘ │  discarded)  │
              │              └──────────────┘
              │
              └── interrupted (any phase + SIGTERM)
                  abandoned (resumed from another process)
                  failed_guardrail (post-iter check)
```

### What "graceful exit" means concretely

```
at any moment, you can:
  $ pkill -TERM autoresearch-loop   ← clean shutdown
  $ Ctrl-C in the tmux pane         ← clean shutdown
  $ shut laptop, walk away          ← (with pmset disablesleep set)
                                       loop tolerates sleep up to its
                                       deadlines, then dies on next
                                       deadline; resume on wake

and:
  $ autoresearch resume             ← idempotent, picks up state
                                       from DB, no questions asked
```

---

## What plugs into what

```
                  FRAMEWORK CORE
        ┌─────────────────────────┐
        │ orchestrator/           │
        │ memory/      (pgvector) │
        │ obs/         (web UI)   │
        │ agents/      (claude /  │
        │               openai /  │
        │               local)    │
        │ guardrails/             │
        │ resume/                 │
        └────────────┬────────────┘
                     │ reads
                     ▼
        ┌─────────────────────────┐
        │   PROBLEM (your repo)   │
        │ ┌─────────────────────┐ │
        │ │ spec.md             │ │
        │ │ eval/score.py       │ │  ◀── deterministic
        │ │ eval/fixtures/      │ │
        │ │ starter/            │ │
        │ │ constraints.toml    │ │
        │ │ exit.toml           │ │
        │ └─────────────────────┘ │
        └─────────────────────────┘

ON THE COMMAND LINE
───────────────────
$ autoresearch run problems/sheet-music
$ autoresearch run problems/slide-converter
$ autoresearch run problems/anything-with-an-eval
```

The framework is one repo. Problems are separate (or in `problems/`).
The CLI does everything else.

---

## What's hard about this — the open questions

```
1. EVAL DESIGN — the user must write a deterministic score function.
   That's a real ask.

   ┌─────────────────────┬────────────────────────────────┐
   │ TEMPLATES           │ EXAMPLE                        │
   ├─────────────────────┼────────────────────────────────┤
   │ classification      │ score = correct / total        │
   │ regression          │ score = 1 - normalized_mae     │
   │ exact-match         │ score = matches / total        │
   │ diff-vs-reference   │ score = 1 - lev(out, ref)      │
   │ llm-as-judge        │ for fuzzy domains              │
   └─────────────────────┴────────────────────────────────┘
   v1: ship templates. user fills in the blanks.

2. AGENT-MODEL CHOICE — DECIDED.

   ALL roles = `claude -p` running headless with
   `--dangerously-skip-permissions` and `--output-format stream-json`.
   It's a coding-tool agent. Not a chat model + tool framework.
   Not multi-vendor. Just claude code, headless, autonomous.

   Different roles get different prompts (and possibly different
   --max-turns budgets), but the same binary.

   ┌──────────┬───────────────┬──────────┐
   │ ROLE     │ max-turns     │ deadline │
   ├──────────┼───────────────┼──────────┤
   │ PLANNER  │ 20            │ 5 min    │
   │ ACTOR    │ 80            │ 60 min   │
   │ REFLECTOR│ 15            │ 3 min    │
   └──────────┴───────────────┴──────────┘

3. CROSS-PROBLEM MEMORY — does the framework learn across problems?
   "for OCR-style problems, page cropping wins ~+20%"
   useful but its own research question.
   v1: each run starts fresh. v2: opt-in cross-problem memory.

4. BUDGET ENFORCEMENT — beyond simple time/$ caps:
   ┌─────────────────────────────────┐
   │ "no progress" cap:               │
   │   if 5 consecutive iters in      │
   │   the same category don't        │
   │   improve, force category swap   │
   │                                  │
   │ bandit-style allocation across   │
   │   categories (UCB / Thompson)?   │
   └─────────────────────────────────┘
   v1: simple "no progress" cap. v2: bandits.

5. PARALLELISM — sequential today.
   ┌────────────────────────────────┐
   │ PRO  3-5 worktrees in parallel │
   │ CON  shared API rate limits,   │
   │      coordination cost         │
   └────────────────────────────────┘
   probably v2.
```

---

## Bootstrapping order — what would I build first

```
M1 (week 1)  CORE LOOP, single problem, sqlite (no pgvector)
              ├── orchestrator.py with PLAN/ACT/REFLECT
              ├── git worktree per ACT
              ├── score.py + fixtures contract
              └── structured iteration log to sqlite
              GOAL: hit iter 10 reliably overnight

M2 (week 2)  OBSERVABILITY + MEMORY UPGRADE
              ├── FastAPI + htmx UI
              ├── pgvector memory (or sqlite-vec)
              ├── semantic search over past iters
              ├── score curve + error curve on dashboard
              └── live agent stream replay
              GOAL: open hood and understand state

M3 (week 3)  RESILIENCE
              ├── heartbeat-based process registry
              ├── resume from any phase
              └── declarative guardrails from constraints.toml
              GOAL: lid-close = safely-resumed, not lost-iter

M4 (week 4)  GENERALIZE + SECOND PROBLEM
              ├── port slide-converter as user #2
              ├── surface framework-vs-problem bits
              └── extract any "music-only" assumptions
              GOAL: framework drops "music" from any name

M5+ (later)  parallelism, multi-model routing,
             cross-problem memory
```

---

## Decisions — LOCKED 2026-04-24

```
D1. MEMORY BACKEND
    sqlite + sqlite-vec for v1 (zero install, single file).
    Postgres+pgvector remains the v2 swap if scale demands.

D2. REPO LAYOUT — TWO REPOS, FULL SPLIT
    ~/Documents/agentic-autoresearch/   (framework, the product)
    ~/Documents/sheet-music-omr/        (problem #1, FRESH start)
    The current ~/Documents/Music/agentic-sheet-music/ stays as
    a salvage source (eval/, scripts/, .claude/ rules) but the
    new music repo starts EMPTY and is populated only with what
    the framework needs (spec.md + eval/score.py + eval/fixtures/).

D3. PLAN/ACT/REFLECT — THREE SEPARATE `claude -p` CALLS
    each call has its own deadline + max-turns budget.
    costs tokens, gains observability + isolation.

D4. FRAMEWORK NAME
    `agentic-autoresearch`
    CLI binary: `autoresearch`
    Python package: `agentic_autoresearch`

D5. SCAFFOLDER — BUILT-IN
    `autoresearch new <problem-name>` generates:
       <problem-name>/spec.md      (templated)
       <problem-name>/eval/score.py (stub returning 0.0)
       <problem-name>/eval/fixtures/.gitkeep
       <problem-name>/.gitignore
    `autoresearch self-test` runs the bundled
       problems/hello-world/ to prove the loop is healthy.

D6. RUNS / ARTIFACTS — IN THE DB
    The DB is the source of truth.
    runs, iterations, artifacts (diffs / eval JSON / agent
    stream-json) all live in the sqlite file.
    The framework writes <run-id>/ on disk only for things
    sqlite is bad at (large binary blobs > 1 MB, optionally
    iter_NNN.md for grep-ability) — and even those are stored
    as artifact rows pointing to the path.
    The PROBLEM REPO never accumulates run state.

D7. THE PROBLEM REPO IS GIT-CLEAN BETWEEN RUNS
    All run state in framework's DB at
      ~/.agentic-autoresearch/db.sqlite (default).
    Worktrees live under .agentic-autoresearch-worktrees/
    inside the problem repo (gitignored), pruned per iter.
```

---

## Naming pass

Pre-implementation rename:

```
improve-omr-loop       →  autoresearch-loop
eval-runs/             →  (vanishes — DB is source of truth)
eval-fixtures/         →  <problem-repo>/eval/fixtures/
gemini_omr.py          →  problem-specific, in problem repo
.claude/CLAUDE.md      →  framework + problem-specific rules merged
```

## Migration plan — execute in order

```
M0 (today)  CREATE TWO REPOS
            ───────────────
            mkdir ~/Documents/agentic-autoresearch
            mkdir ~/Documents/sheet-music-omr

            agentic-autoresearch:
              uv init
              src/agentic_autoresearch/
                orchestrator/    — PLAN/ACT/REFLECT loop
                agents/          — claude -p spawn helpers
                memory/          — sqlite + sqlite-vec schema
                obs/             — FastAPI + htmx dashboard
                guardrails/      — constraints.toml enforcement
                resume/          — state machine + heartbeats
                cli/             — autoresearch new/run/resume/...
              problems/hello-world/spec.md  (self-test)
              tests/

            sheet-music-omr:
              uv init
              spec.md             — problem definition
              eval/score.py       — per-measure exact match
              eval/fixtures/      — 6 fixtures (carry over from
                                    agentic-sheet-music verbatim)
              .gitignore          — worktrees, runs, etc.

            (the current ~/Documents/Music/agentic-sheet-music/
             stays where it is as a salvage source for eval code,
             prompts, and the .claude/ rules. nothing in it gets
             deleted; nothing in it gets used by the new framework
             until we copy it over.)

M1+         see "Bootstrapping order" above
```

---

## Where I'd want your input first

```
1. Q3 (separate calls vs one) — shapes everything else
2. Q1 (sqlite-vec vs Postgres) — affects setup story
3. The PLAN/ACT/REFLECT phasing — overcomplicated?
4. Anything in "what's hard" that's actually NOT hard
5. The error-curve panel — what other observability matters?
```
