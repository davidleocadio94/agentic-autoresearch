# comfyui-character + framework extensions

> Stress-test `agentic-autoresearch` on the comfyui character-consistency
> problem. Five framework extensions are scoped here because the comfyui
> problem demands them — but each is designed problem-agnostically so the
> next problem inherits them for free.
>
> This is THE plan. Edit it in place as the design evolves — no `-final`,
> no version suffixes, no copies.

## Goal

Produce a saved ComfyUI configuration (workflow JSON + parameter dict) that
generates a photorealistic AI character with face-consistency across diverse
contexts, reaching mean score **≥ 4.95 / 5** on the existing 7-dimension
rubric, with `face_consistency ≥ 4.85` and replicate std **≤ 0.05**, within
**30 pod-hours and a hard $50 judge-cost cap**.

Equally important secondary goal: by the end of the run, the per-project
world-model DB contains a queryable structure of beliefs, thoughts,
understandings, intuitions, and saved configs — *that* is the deliverable
that compounds across future runs.

---

## What stays

- Plan / Act / Reflect loop, three `claude -p` calls per iteration
- sqlite + sqlite-vec memory store
- Git worktree isolation for ACT
- FastAPI + htmx dashboard at `:8765`
- Guardrails (path immutability, dep limits, size caps)
- CLI verbs: `new / run / resume / stop / self-test / dashboard / status`

## What changes

```
ADDITION                         SHAPE OF CHANGE
────────                         ───────────────
1. Non-blocking ACT              orchestrator state machine learns
                                 `awaiting_remote` — submit job, suspend,
                                 poll, resume — Claude session not held
                                 open during multi-hour pod work
2. Per-project memory namespace  ~/.aar/projects/<problem>/db.sqlite
                                 (was: single global db)
3. World-model schema            beliefs / thoughts / understandings /
                                 intuitions / configurations tables,
                                 not a flat lessons list
4. Claude-Opus-4.7 vision judge  replaces Gemini; held-out refs +
                                 fresh judge session per call
5. Global credentials store      ~/.agentic-autoresearch/credentials.toml
                                 chmod 600, never in spec.md, never git-
                                 committed, never logged
6. RunPod headless lifecycle     start_pod / poll_pod / stop_pod via API,
                                 zero browser, zero copy-paste
```

---

## Credentials: where everything lives

Hard rules:

1. **`spec.md` declares which credentials a problem needs by name only.**
   It never contains a key, a token, a password, or a pod ID.
2. **All secrets live in `~/.agentic-autoresearch/credentials.toml`**,
   chmod 600, owned by `$USER`, never written to logs, never in any
   directory that is or could become a git repo.
3. **The CLI is the only thing that reads the file.** Agent processes
   (`claude -p`) inherit only the specific env vars they need for the call
   they're about to make, via subprocess `env=`. Nothing global, nothing
   shell-exported, nothing in `.env` files in project dirs.
4. **`autoresearch credentials add <service>`** is the one user-facing way
   to write keys. It prompts (no shell history), writes with 600, and
   verifies the key by making one cheap API call before saving.

### File layout

```
~/.agentic-autoresearch/
  credentials.toml          # 600. THE secrets file. Never copied anywhere.
  config.toml               # non-secret defaults (default models, polling
                            # intervals, dashboard port). Safe to back up.
  projects/                 # per-project state, per-problem isolation
    omr/
      db.sqlite             # existing OMR memory (left alone)
      artifacts/
    comfyui-character/
      db.sqlite             # NEW — fresh world model for this problem
      artifacts/            # pulled images, workflow JSONs, judge outputs
      pods/                 # active pod manifests (handle, started_at,
                            # last_seen, cumulative_seconds)
  logs/
    aar-YYYY-MM-DD.log      # framework logs, secrets redacted at write
```

### credentials.toml schema

```toml
# ~/.agentic-autoresearch/credentials.toml — chmod 600

[runpod]
api_key = "..."             # RunPod REST API key
default_datacenter = "EU-RO-1"
default_gpu = "A40"
network_volume_id = "..."   # the existing volume with 34GB of models

[anthropic]
api_key = "..."             # used by claude -p for planner/actor/reflector
                            # AND by the Opus-4.7 vision judge
                            # (one key, the framework segregates calls)

[huggingface]
token = "..."               # for pulling/pushing LoRAs (yummygummy/elena-*)

[openai]                    # optional, unused by comfyui problem
api_key = "..."

[gemini]                    # legacy, kept for backward compat with old evals
api_key = "..."             # comfyui-character problem does NOT use this
```

### spec.md credentials declaration

```yaml
# in problems/comfyui-character/spec.md frontmatter
requires_credentials:
  - runpod          # pod lifecycle
  - anthropic       # planner/actor/reflector + vision judge
  - huggingface     # LoRA download (Elena v2, lenovo, etc.)
```

If a required credential is missing at `autoresearch run` time, the CLI
errors before spawning anything, with the exact `autoresearch credentials
add runpod` command to fix it.

---

## The world-model memory (the substantive upgrade)

Per-project DB (`~/.aar/projects/comfyui-character/db.sqlite`) has the
existing `runs` / `iterations` / `artifacts` tables, plus five new tables
that form a structured, queryable epistemic state:

```
beliefs
───────
A proposition the agent currently treats as true, with evidence.
"uint3 quantization causes cross-hatch artifacts in Qwen-Image LoRAs"
columns: id, content, confidence (0–1), evidence_iters (json array),
         created_at, last_validated_at, status (active/retired/contradicted),
         embedding (vec, for similarity queries)

thoughts
────────
A hypothesis in flight. Planner emits these; Reflector promotes to belief
on confirmation, retires on contradiction.
"increasing id_weight from 0.87 to 0.92 will raise FC without hurting
 photorealism"
columns: id, content, status (proposed/testing/confirmed/refuted),
         parent_belief_ids (json), iter_proposed, iter_resolved,
         embedding

understandings
──────────────
A causal explanation linking multiple beliefs. Reflector writes these
after enough beliefs cluster.
"Face consistency degrades as LoRA strength exceeds 1.0 because the
 character LoRA fights IPAdapter conditioning; sweet spot 0.7–0.9 across
 all tested seeds"
columns: id, content, supporting_belief_ids (json), confidence,
         created_at, last_validated_at, embedding

intuitions
──────────
Soft priors that bias Planner without being formally proven. Earned by
multiple confirmed understandings in a region of the search space.
"for night-context prompts, prefer warmer color temp + lower CFG"
columns: id, content, weight (0–1), supporting_understanding_ids (json),
         created_at, last_used_at, embedding

configurations
──────────────
Saved configs. Every iteration that beats prior-best is named and stored.
columns: id, name (e.g. "mickmumpitz-ccc-3.8-tuned-v2"), workflow_json,
         param_dict (json), best_score, best_score_std, replicates,
         provenance_iter, parent_config_id, sample_image_path,
         sample_embedding (CLIP vector, for visual-similarity queries),
         created_at
```

### Promotion path

```
        Planner proposes               Reflector evaluates
   ┌──────────────────────┐          ┌──────────────────────┐
   │  THOUGHT (proposed)  │ ──iter──▶│   confirmed?         │
   └──────────────────────┘          └─────────┬────────────┘
                                               │
                          ┌────────────────────┼────────────────────┐
                          ▼                    ▼                    ▼
                      contradicted          inconclusive         confirmed
                      retire thought        keep testing         promote → BELIEF
                                                                     │
                                                          ┌──────────┘
                                                          ▼
                                              N related beliefs cluster
                                                          │
                                                          ▼
                                                 UNDERSTANDING written
                                                          │
                                                          ▼
                                              M understandings in a region
                                                          │
                                                          ▼
                                                  INTUITION earned
```

### Query examples the Planner will actually run

```
"what do we believe about CFG values?"
   → SELECT * FROM beliefs WHERE content MATCH 'cfg' AND status='active'

"what configs are nearest to brooke_1.png in CLIP space?"
   → vec_search on configurations.sample_embedding

"what's our current intuition for night-context prompts?"
   → SELECT * FROM intuitions WHERE content MATCH 'night' ORDER BY weight DESC

"what thoughts has the planner already tried and refuted?"
   → SELECT * FROM thoughts WHERE status='refuted' (kills redundant proposals)

"show every belief whose evidence includes iter 17"
   → JSON path query on beliefs.evidence_iters
```

Planner's prompt is built from a **structured snapshot** of this world
model (top-K beliefs by recency × confidence, all active intuitions, all
configurations within X CLIP distance of the goal), not a flat lesson
list. That is the upgrade that turns "loop that remembers" into "loop
that learns."

---

## Non-blocking ACT — the state machine

```
                    iteration state machine (per iter)
   ┌─────────┐
   │ PLANNED │ planner wrote hypothesis JSON, awaiting ACT spawn
   └────┬────┘
        ▼
   ┌─────────┐
   │ ACTING  │ claude -p actor running locally, building config +
   └────┬────┘ workflow JSON, calling RunPod API to submit
        │
        ├──── synchronous eval (no remote) ───▶ SCORING
        │
        └──── remote job submitted ──▶ AWAITING_REMOTE
                                       (claude -p actor EXITS
                                        job_handle.json persisted,
                                        orchestrator owns polling)
                                            │
                                            ▼ orchestrator polls
                                            ▼ every 5 min via RunPod API
                                       ┌──────────┐
                                       │ POLLING  │
                                       └────┬─────┘
                                            ▼ results landed
                                       ┌──────────┐
                                       │ SCORING  │ fresh claude -p vision
                                       └────┬─────┘ judge call (Opus 4.7)
                                            ▼
                                       ┌──────────┐
                                       │REFLECTING│ fresh claude -p
                                       └────┬─────┘ updates world model
                                            ▼
                                       ┌──────────┐
                                       │   DONE   │
                                       └──────────┘
```

Key property: a 4-hour pod batch consumes ~0 Claude-session minutes
between submit and results. Three `claude -p` calls happen (actor for
submit, judge for scoring, reflector for world-model update), each
short. The orchestrator (a single long-lived Python process) owns the
polling and state transitions.

---

## Vision judge: Opus 4.7, not Gemini

```
JUDGE CALL SHAPE
────────────────
input:
  - rubric (7 dimensions, weights, scoring guide — pinned per run)
  - 3 visible Brooke refs (planner & reflector see these)
  - 2 held-out Brooke refs (judge sees, planner never does)
  - 1 identity reference (the face to match)
  - k=3 candidate images for this prompt × 5 prompts = 15 candidates
output:
  - per-image scores across 7 dimensions
  - per-image final_score
  - mean + std per dimension across k seeds
  - drift_check: re-score 2 fixed historical samples every 5 iters;
    if absolute drift > 0.3, pause for recalibration

isolation:
  - fresh claude -p, no system prompt referencing the project,
    no memory access, no planner context — just rubric + images
  - this is the framework's anti-Goodhart guard. NOT bulletproof
    (planner/actor/judge are same model family); it's harm reduction.
```

---

## Per-project memory namespace

Currently: `~/.agentic-autoresearch/db.sqlite` is one flat DB shared by
every problem (OMR is in there). The change:

```
BEFORE                                AFTER
──────                                ─────
~/.aar/db.sqlite                      ~/.aar/projects/omr/db.sqlite
  (everything)                        ~/.aar/projects/comfyui-character/db.sqlite
                                      ~/.aar/projects/<future>/db.sqlite
```

`autoresearch run <problem>` opens *only* that problem's DB.
`autoresearch run <problem> --include-project omr` is the opt-in way to
make the planner query OMR memories too (rare; default off).

Migration: existing `~/.aar/db.sqlite` is moved to
`~/.aar/projects/omr/db.sqlite` on first run after the change (one-time, idempotent,
verified by row count before delete of the old file).

---

## ComfyUI problem scaffold

```
problems/comfyui-character/
  spec.md                   # problem statement, eval, budget, kill
                            # conditions, requires_credentials
  eval/
    score.py                # wraps existing score_pro_gap.py logic but
                            # using Opus-4.7 vision judge, reads
                            # --config + --seeds, returns
                            # {score, replicates, per_seed, per_dim}
    prompts.json            # 5 fixed evaluation prompts (night street,
                            # indoor cozy, outdoor daylight, full-body
                            # action, close-up)
    references/
      visible/              # brooke_1..3.png (planner sees)
      held_out/             # brooke_4..5.png (judge only, never in prompts)
      identity.png          # the face to match
  references/               # SEED MEMORY — the survey findings
    seed.jsonl              # 3 validated 2026 stacks pre-loaded as
                            # beliefs + saved configurations:
                            #   1. Mickmumpitz CCC 3.8 (Flux + PuLID)
                            #   2. Qwen-Image-Edit one-headshot
                            #   3. Apatero IPAdapter-FaceID+LoRA+ControlNet
                            # each with: source URL, claimed FC, params
  workflows/                # known-good ComfyUI workflow JSON templates
    flux-pulid.json
    qwen-edit.json
    sdxl-ipadapter-controlnet.json
  src/comfyui_character/
    runner.py               # ACTOR EDITS THIS (the one mutable file —
                            # parameterized; renders template + params
                            # into a workflow JSON, submits to ComfyUI on
                            # the pod, waits, downloads)
    config_schema.py        # the structured artifact: typed dict of
                            # (workflow_template, model, lora_stack,
                            #  sampler, scheduler, steps, cfg, shift,
                            #  prompt_template, post_process)
```

### spec.md (sketch)

```yaml
---
name: comfyui-character
requires_credentials: [runpod, anthropic, huggingface]
budget:
  pod_hours: 30
  judge_dollars: 50
target:
  mean_score: 4.95
  face_consistency: 4.85
  max_std: 0.05
kill_conditions:
  - budget_exhausted
  - no_improvement_over_2_sigma_for: 5  # iterations
  - judge_drift_absolute: 0.3           # on held-out anchors
replicates_per_config: 3                # seeds
forced_first_iterations:
  - mickmumpitz-ccc-3.8
  - qwen-image-edit-one-headshot
  - apatero-ipadapter-lora-controlnet
---

(prose problem description, rubric reference, etc.)
```

### Forced first 3 iterations

Before the planner is free to invent, the loop runs the three validated
reference stacks from the survey. This gives the world model a real
baseline — three confirmed configurations, with mean+std, judge outputs,
and CLIP embeddings — before any inventive iteration. Without this, the
planner spends early iterations re-deriving public knowledge.

---

## RunPod headless lifecycle

Everything via the RunPod REST API, no browser. The framework's RunPod
agent has four operations:

```
start_pod(gpu, datacenter, volume_id) ─▶ {pod_id, ssh_url, comfyui_url}
   - reuses existing 34GB network volume
   - polls until ComfyUI responds on :8188
   - appends to ~/.aar/projects/<p>/pods/active.json

poll_pod(pod_id) ─▶ {state, cumulative_seconds, last_seen}

submit_job(comfyui_url, workflow_json) ─▶ {job_handle, prompt_id}

stop_pod(pod_id)
   - on iteration done OR on kill condition OR on framework crash
   - cost ledger gets final pod_seconds, writes to iteration row
```

Pod-leak guards: orchestrator startup scans `pods/active.json`; any pod
older than `spec.budget.pod_hours × 1.5` is auto-stopped with a logged
warning. Manual `autoresearch pods list` / `autoresearch pods stop <id>`
verbs for inspection.

---

## Cost ledger

Every iteration row gets:

```
costs jsonb:
  pod_seconds: int
  pod_dollars: float (computed from GPU type × seconds)
  judge_calls: int
  judge_input_tokens: int
  judge_output_tokens: int
  judge_dollars: float
  planner_tokens, actor_tokens, reflector_tokens: int
  total_dollars: float
```

Reflector sees cumulative spend when writing lessons. Hypothesis
proposals that cost >$5 to gain <0.02 score get demoted to refuted
thoughts. Budget kill-condition is a strict cap, not a target.

---

## The full ASCII diagram

```
┌────────────────────────────────────────────────────────────────────────────┐
│                       agentic-autoresearch — full system                   │
└────────────────────────────────────────────────────────────────────────────┘

  USER
    │
    │ writes once: problems/comfyui-character/spec.md
    │ runs:        autoresearch run comfyui-character
    ▼
┌──────────────────────────────────────────────────────────────────────────┐
│                              CLI                                         │
│  - loads ~/.agentic-autoresearch/credentials.toml (chmod 600)            │
│  - validates spec.md `requires_credentials` are present                  │
│  - opens ~/.aar/projects/comfyui-character/db.sqlite                     │
│  - starts orchestrator (long-lived python process)                       │
└──────────────────────────────────────────────────────────────────────────┘
    │
    ▼
┌──────────────────────────────────────────────────────────────────────────┐
│                          ORCHESTRATOR                                    │
│  state machine: PLANNED → ACTING → AWAITING_REMOTE → POLLING →           │
│                 SCORING → REFLECTING → DONE                              │
│  owns: pod polling, kill conditions, budget tracking, drift checks       │
└──────────────────────────────────────────────────────────────────────────┘
    │                          │                          │
    │ spawns short-lived       │ owns long-running        │ writes/reads
    │ claude -p subprocesses   │ RunPod jobs (async)      │ per-project DB
    ▼                          ▼                          ▼

╔═══════════════════════╗  ╔═══════════════════════╗  ╔═══════════════════════╗
║   THREE AGENTS        ║  ║   RUNPOD                  ║   WORLD-MODEL DB      ║
║   (claude -p each)    ║  ║   (headless via API)      ║  (per-project sqlite) ║
╠═══════════════════════╣  ╠═══════════════════════╣  ╠═══════════════════════╣
║                       ║  ║                       ║  ║                       ║
║  ┌─────────────────┐  ║  ║  start_pod (GPU,      ║  ║  runs                 ║
║  │     PLANNER     │  ║  ║   datacenter,         ║  ║  iterations           ║
║  │                 │  ║  ║   volume_id)          ║  ║  artifacts            ║
║  │ reads world     │  ║  ║                       ║  ║                       ║
║  │ model snapshot, │  ║  ║  ┌──────────────┐     ║  ║  ┌─────────────────┐  ║
║  │ refs, spec      │  ║  ║  │ network vol  │     ║  ║  │  beliefs        │  ║
║  │                 │──╬──╬──┤ 34GB models  │     ║  ║  │  thoughts       │  ║
║  │ writes:         │  ║  ║  │ persistent   │     ║  ║  │  understandings │  ║
║  │  thought JSON   │  ║  ║  └──────┬───────┘     ║  ║  │  intuitions     │  ║
║  └────────┬────────┘  ║  ║         │             ║  ║  │  configurations │  ║
║           ▼           ║  ║         ▼             ║  ║  │                 │  ║
║  ┌─────────────────┐  ║  ║  ┌──────────────┐     ║  ║  │  (each table    │  ║
║  │     ACTOR       │──╬──╬─▶│  ComfyUI on  │     ║  ║  │   has embedding │  ║
║  │                 │  ║  ║  │  pod, :8188  │     ║  ║  │   col for       │  ║
║  │ edits           │  ║  ║  └──────┬───────┘     ║  ║  │   sqlite-vec    │  ║
║  │ runner.py +     │  ║  ║         │ generates   ║  ║  │   similarity    │  ║
║  │ config dict     │  ║  ║         ▼             ║  ║  │   queries)      │  ║
║  │                 │  ║  ║  ┌──────────────┐     ║  ║  └─────────────────┘  ║
║  │ submits job,    │  ║  ║  │  k=3 seeds × │     ║  ║                       ║
║  │ EXITS — writes  │  ║  ║  │  5 prompts = │     ║  ║  costs ledger         ║
║  │  pending_job    │  ║  ║  │  15 images   │     ║  ║   (per iter row)      ║
║  │  .json          │  ║  ║  └──────┬───────┘     ║  ║                       ║
║  └─────────────────┘  ║  ║         │             ║  ║                       ║
║                       ║  ║         ▼ download    ║  ║                       ║
║  (orchestrator polls  ║  ║   ~/.aar/projects/    ║  ║                       ║
║   every 5min until    ║  ║   comfyui-character/  ║  ║                       ║
║   complete)           ║  ║   artifacts/iter_NNN/ ║  ║                       ║
║                       ║  ║                       ║  ║                       ║
║  ┌─────────────────┐  ║  ║                       ║  ║                       ║
║  │  VISION JUDGE   │  ║  ║                       ║  ║                       ║
║  │  (Opus 4.7)     │  ║  ║                       ║  ║                       ║
║  │                 │  ║  ║                       ║  ║                       ║
║  │ fresh claude -p,│  ║  ║                       ║  ║                       ║
║  │ no project ctx, │  ║  ║                       ║  ║                       ║
║  │ sees: rubric +  │  ║  ║                       ║  ║                       ║
║  │  refs (incl.    │  ║  ║                       ║  ║                       ║
║  │  held-out) +    │  ║  ║                       ║  ║                       ║
║  │  15 candidates  │  ║  ║                       ║  ║                       ║
║  │                 │  ║  ║                       ║  ║                       ║
║  │ returns:        │  ║  ║                       ║  ║                       ║
║  │  per-image      │  ║  ║                       ║  ║                       ║
║  │  scores, mean,  │  ║  ║                       ║  ║                       ║
║  │  std, drift     │  ║  ║                       ║  ║                       ║
║  └────────┬────────┘  ║  ║                       ║  ║                       ║
║           ▼           ║  ║                       ║  ║                       ║
║  ┌─────────────────┐  ║  ║                       ║  ║                       ║
║  │    REFLECTOR    │  ║  ║                       ║  ║                       ║
║  │                 │  ║  ║                       ║  ║                       ║
║  │ updates world   │──╬──╬───────────────────────╬──╬─▶ promote/retire      ║
║  │ model:          │  ║  ║                       ║  ║   thoughts,           ║
║  │  - confirm or   │  ║  ║                       ║  ║   confirm beliefs,    ║
║  │    refute       │  ║  ║                       ║  ║   write understandings║
║  │    thoughts     │  ║  ║                       ║  ║   earn intuitions,    ║
║  │  - promote to   │  ║  ║                       ║  ║   save configurations ║
║  │    beliefs      │  ║  ║                       ║  ║   (if beat best),     ║
║  │  - write under- │  ║  ║                       ║  ║   embed sample → vec  ║
║  │    standings    │  ║  ║                       ║  ║                       ║
║  │  - earn         │  ║  ║                       ║  ║                       ║
║  │    intuitions   │  ║  ║                       ║  ║                       ║
║  └─────────────────┘  ║  ║                       ║  ║                       ║
║                       ║  ║                       ║  ║                       ║
╚═══════════════════════╝  ╚═══════════════════════╝  ╚═══════════════════════╝

  loop until: target reached  OR  budget exhausted  OR  plateau (5 iters)  OR
              judge drift > 0.3 (pause, recalibrate)

   ┌────────────────────────────────────────────────────────────────────────┐
   │                      FILESYSTEM SUMMARY                                │
   ├────────────────────────────────────────────────────────────────────────┤
   │                                                                        │
   │  ~/.agentic-autoresearch/                                              │
   │    credentials.toml          chmod 600 — RunPod, Anthropic, HF, etc.   │
   │    config.toml               non-secret defaults                       │
   │    projects/                                                           │
   │      omr/db.sqlite           untouched (migrated from old flat DB)     │
   │      comfyui-character/                                                │
   │        db.sqlite             world model: beliefs, thoughts,           │
   │                              understandings, intuitions, configs       │
   │        artifacts/iter_NNN/   15 images per iter, judge outputs         │
   │        pods/active.json      live pod manifests (leak-detection)       │
   │    logs/aar-YYYY-MM-DD.log   framework log, secrets redacted           │
   │                                                                        │
   │  ~/Documents/agentic-autoresearch/                                     │
   │    src/agentic_autoresearch/                                           │
   │      orchestrator/           state machine (async-act states)          │
   │      agents/                 planner, actor, reflector, judge, runpod  │
   │      memory/                 world-model schema + queries              │
   │      credentials/            loader for ~/.aar/credentials.toml        │
   │      obs/                    dashboard (extended with world-model      │
   │                              browser, cost ledger, pod manifests)      │
   │    problems/                                                           │
   │      hello-world/            bundled self-test                         │
   │      comfyui-character/                                                │
   │        spec.md               PROBLEM DEFINITION (no secrets)           │
   │        eval/score.py         vision-judge wrapper                      │
   │        eval/prompts.json     5 fixed evaluation prompts                │
   │        eval/references/      visible/, held_out/, identity.png         │
   │        references/seed.jsonl 3 validated 2026 reference stacks         │
   │        workflows/*.json      known-good ComfyUI workflow templates     │
   │        src/comfyui_character/runner.py    the one mutable file         │
   │                                                                        │
   └────────────────────────────────────────────────────────────────────────┘
```

---

## Implementation order (smallest first, each independently testable)

1. **Credentials store + CLI verb.** `autoresearch credentials add <svc>`.
   Touches nothing else. Verifiable by `autoresearch credentials list`.
2. **Per-project DB namespacing.** Move `~/.aar/db.sqlite` →
   `~/.aar/projects/omr/db.sqlite`. Existing OMR self-test must still
   pass. This is the migration gate — no other framework work merges until it
   does.
3. **World-model schema.** Add the five tables (beliefs / thoughts /
   understandings / intuitions / configurations) to the per-project DB.
   Backfill OMR's existing lessons as `beliefs` with `confidence=0.7,
   evidence_iters=[iter#]` so the schema starts populated.
4. **Opus-4.7 vision judge.** New `agents/judge.py`. Drop-in replacement
   for any eval that wants vision scoring. Test on a held-out set of
   already-scored comfyui images; verify score correlation with the old
   Gemini judge before trusting it (one-time calibration run).
5. **Non-blocking ACT + RunPod lifecycle.** State machine, polling,
   pod-leak guards. Self-test with a fake "remote" that's actually a
   local sleep — verify the state machine works before pointing it at
   real GPUs.
6. **Comfyui-character problem scaffold.** spec.md, eval/, references/,
   the runner. Forced first 3 iterations on the validated stacks.
7. **First end-to-end run.** Budget 30 pod-hours, $50 cap. Walk away.

Steps 1–5 are framework work — they're the things any future problem
will inherit. Step 6 is the comfyui-specific scaffold. Step 7 is the
test of whether the framework now generalizes beyond OMR.

---

## What "done" looks like

For the run: a `configurations` row with mean ≥ 4.95, FC ≥ 4.85,
std ≤ 0.05, fully reproducible from the saved workflow JSON + param
dict. **Or** a refuted hypothesis from the world model explaining
exactly which ceiling we hit and where the next investment should go.

For the framework: five new abstractions, each demanded by a real run,
each scoped problem-agnostically. The next problem — whatever it is —
inherits async-act, per-project memory, world model, vision judge, and
headless credentials for free.
