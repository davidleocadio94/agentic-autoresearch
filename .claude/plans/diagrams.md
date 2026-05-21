# agentic-autoresearch — system in ASCII

> Companion to `comfyui-character-and-framework.md`. Every diagram shows
> one slice of how the framework runs end-to-end. Read top-to-bottom.

---

## 1. The 30,000-ft view

```
   ┌──────────────────────────────────────────────────────────────────────┐
   │                                                                      │
   │                          PROBLEM SPEC                                │
   │                                                                      │
   │     spec.md  +  eval/score.py  +  references/  +  workflows/         │
   │                                                                      │
   └──────────────────────────────────────────────────────────────────────┘
                                       │
                                       ▼
   ┌──────────────────────────────────────────────────────────────────────┐
   │                                                                      │
   │                       agentic-autoresearch                           │
   │                                                                      │
   │       ┌────────────┐    ┌────────────┐    ┌────────────┐             │
   │       │   PLAN     │ →  │    ACT     │ →  │  REFLECT   │ ─┐          │
   │       └────────────┘    └────────────┘    └────────────┘  │          │
   │             ▲                                              │          │
   │             └──────────── world model ◀────────────────────┘          │
   │                                                                      │
   └──────────────────────────────────────────────────────────────────────┘
                                       │
                                       ▼
   ┌──────────────────────────────────────────────────────────────────────┐
   │                                                                      │
   │                       SAVED CONFIGURATION                            │
   │                                                                      │
   │       workflow.json  +  param_dict  +  reproducible score            │
   │                                                                      │
   └──────────────────────────────────────────────────────────────────────┘
```

---

## 2. The full system, all moving parts

```
╔══════════════════════════════════════════════════════════════════════════╗
║                                                                          ║
║                            USER MACHINE (Mac)                            ║
║                                                                          ║
║   ┌──────────────────────────────────────────────────────────────────┐   ║
║   │                                                                  │   ║
║   │  CLI                                                             │   ║
║   │  ───                                                             │   ║
║   │  autoresearch run comfyui-character                              │   ║
║   │     │                                                            │   ║
║   │     │  1. load ~/.agentic-autoresearch/credentials.toml (600)    │   ║
║   │     │  2. validate spec.md requires_credentials                  │   ║
║   │     │  3. open ~/.aar/projects/comfyui-character/db.sqlite       │   ║
║   │     │  4. spawn orchestrator (long-lived python process)         │   ║
║   │     ▼                                                            │   ║
║   │  ORCHESTRATOR                                                    │   ║
║   │  ────────────                                                    │   ║
║   │  - owns the state machine                                        │   ║
║   │  - owns RunPod polling (every 5 min)                             │   ║
║   │  - owns kill conditions + budget                                 │   ║
║   │  - owns drift checks (held-out anchors)                          │   ║
║   │  - spawns short-lived `claude -p` subprocesses on demand         │   ║
║   │     │                                                            │   ║
║   │     ├──spawn──▶ ┌─────────┐                                      │   ║
║   │     │           │ PLANNER │  claude -p, reads world model        │   ║
║   │     │           └─────────┘  writes thought JSON, exits          │   ║
║   │     │                                                            │   ║
║   │     ├──spawn──▶ ┌─────────┐                                      │   ║
║   │     │           │  ACTOR  │  claude -p, builds config + workflow │   ║
║   │     │           └─────────┘  submits to RunPod, exits            │   ║
║   │     │                                                            │   ║
║   │     │           ┌─────────┐                                      │   ║
║   │     │           │  JUDGE  │  Opus 4.7, runs ON THE POD,          │   ║
║   │     │           └─────────┘  scores 15 in place, returns winner  │   ║
║   │     │                                                            │   ║
║   │     └──spawn──▶ ┌─────────┐                                      │   ║
║   │                 │REFLECTOR│  claude -p, updates world model      │   ║
║   │                 └─────────┘  promotes/refutes thoughts, exits    │   ║
║   │                                                                  │   ║
║   │                                                                  │   ║
║   │  DASHBOARD (FastAPI + htmx, :8765)                               │   ║
║   │  ─────────                                                       │   ║
║   │  reads from per-project DB, shows world model + cost ledger      │   ║
║   │                                                                  │   ║
║   └──────────────────────────────────────────────────────────────────┘   ║
║                              │                                           ║
║                              │ REST over HTTPS                           ║
║                              ▼                                           ║
╚══════════════════════════════════════════════════════════════════════════╝
                               │
                               ▼
╔══════════════════════════════════════════════════════════════════════════╗
║                                                                          ║
║                              RUNPOD CLOUD                                ║
║                                                                          ║
║   ┌──────────────────────────────────────────────────────────────────┐   ║
║   │                                                                  │   ║
║   │  POD  (A40, A100, etc., chosen per spec defaults)                │   ║
║   │  ───                                                             │   ║
║   │                                                                  │   ║
║   │     ┌────────────────────────┐    ┌───────────────────────┐      │   ║
║   │     │  NETWORK VOLUME (34GB) │    │  ComfyUI on :8188     │      │   ║
║   │     │  persistent            │ ─▶ │  reads volume,        │      │   ║
║   │     │  - SDXL, Flux, Qwen    │    │  runs workflow,       │      │   ║
║   │     │  - encoders, VAE       │    │  outputs PNGs         │      │   ║
║   │     │  - custom nodes        │    └───────────┬───────────┘      │   ║
║   │     │  - LoRAs (from HF)     │                │                  │   ║
║   │     └────────────────────────┘                │                  │   ║
║   │                                               │                  │   ║
║   └───────────────────────────────────────────────┼──────────────────┘   ║
║                                                   │ artifacts             ║
║                                                   ▼                       ║
╚═══════════════════════════════════════════════════╪═══════════════════════╝
                                                    │ HTTPS POST (~150KB)
                                                    ▼   winner.webp + scores.json
                          ~/.aar/projects/comfyui-character/iters/iter_NNN/
```

---

## 3. Iteration state machine

Each iteration is a state-machine row in the per-project DB. The
orchestrator advances it; agent subprocesses come and go.

```
                    ┌──────────────────────────────┐
                    │                              │
                    │         PLANNED              │
                    │                              │
                    │  planner JSON written        │
                    │  iter row inserted           │
                    │                              │
                    └──────────────┬───────────────┘
                                   │
                                   │ orchestrator spawns actor
                                   ▼
                    ┌──────────────────────────────┐
                    │                              │
                    │         ACTING               │
                    │                              │
                    │  actor `claude -p` running   │
                    │  builds config + workflow    │
                    │  submits to RunPod           │
                    │                              │
                    └──────────────┬───────────────┘
                                   │
                  ┌────────────────┴────────────────┐
                  │                                 │
       no remote job needed                 remote job submitted
       (deterministic eval)                 actor writes
                  │                         pending_job.json
                  │                         then EXITS
                  │                                 │
                  ▼                                 ▼
       ┌─────────────────┐               ┌──────────────────────┐
       │     SCORING     │               │   AWAITING_REMOTE    │
       └─────────┬───────┘               │                      │
                 │                       │  no claude session   │
                 │                       │  open. orchestrator  │
                 │                       │  owns it now.        │
                 │                       └──────────┬───────────┘
                 │                                  │
                 │                                  │ orchestrator
                 │                                  │ polls /v1/...
                 │                                  │ every 5 min
                 │                                  ▼
                 │                       ┌──────────────────────┐
                 │                       │      POLLING         │
                 │                       │                      │
                 │                       │  state: queued /     │
                 │                       │  running / complete  │
                 │                       │  / failed / timeout  │
                 │                       └──────────┬───────────┘
                 │                                  │
                 │                       judge ran on pod; winner +
                 │                       scores.json POSTed to Mac
                 │                       (~150KB total per iter)
                 │                                  │
                 └──────────────────┬───────────────┘
                                    ▼
                    ┌──────────────────────────────┐
                    │                              │
                    │         SCORING              │
                    │                              │
                    │  Opus 4.7, runs ON THE POD   │
                    │  no project ctx, no memory   │
                    │  rubric + refs + 15 images   │
                    │  scored in place. only the   │
                    │  winner.webp + scores.json   │
                    │  come back to the Mac        │
                    │                              │
                    └──────────────┬───────────────┘
                                   │
                                   ▼
                    ┌──────────────────────────────┐
                    │                              │
                    │       REFLECTING             │
                    │                              │
                    │  fresh `claude -p` reflector │
                    │  updates world model:        │
                    │  promote / retire thoughts,  │
                    │  confirm beliefs, write      │
                    │  understandings, earn        │
                    │  intuitions, save config     │
                    │  (if new best)               │
                    │                              │
                    └──────────────┬───────────────┘
                                   │
                                   ▼
                    ┌──────────────────────────────┐
                    │            DONE              │
                    └──────────────────────────────┘

  TERMINAL FAILURE STATES (from any non-terminal state):
  ────────────────────────────────────────────────────
    AWAITING_REMOTE  ──pod failed/timeout──▶ FAILED  (retry policy in spec)
    POLLING          ──RunPod 5xx persists──▶ FAILED
    SCORING          ──judge JSON unparseable──▶ FAILED  (one retry)
    any              ──global kill condition met──▶ ABORTED
```

---

## 4. The world model — schema and promotion path

```
┌─────────────────────────────────────────────────────────────────────────┐
│                                                                         │
│           ~/.aar/projects/comfyui-character/db.sqlite                   │
│                                                                         │
│   ┌─────────────────────────────────────────────────────────────────┐   │
│   │  STANDARD TABLES (existing)                                     │   │
│   │  ─────────────────────────                                      │   │
│   │  runs            (id, started_at, ended_at, exit_reason, …)     │   │
│   │  iterations      (id, run_id, state, costs jsonb, …)            │   │
│   │  artifacts       (id, iter_id, kind, path, …)                   │   │
│   └─────────────────────────────────────────────────────────────────┘   │
│                                                                         │
│   ┌─────────────────────────────────────────────────────────────────┐   │
│   │  WORLD MODEL TABLES (new)                                       │   │
│   │  ────────────────────────                                       │   │
│   │                                                                 │   │
│   │  ┌─────────────────────────────────────────────────────────┐    │   │
│   │  │  thoughts                                               │    │   │
│   │  │  ─────────                                              │    │   │
│   │  │  hypotheses in flight                                   │    │   │
│   │  │  status ∈ {proposed, testing, confirmed, refuted}       │    │   │
│   │  │  iter_proposed, iter_resolved                           │    │   │
│   │  │  embedding (vec384)                                     │    │   │
│   │  └────────────────────────┬────────────────────────────────┘    │   │
│   │                           │ confirmation                        │   │
│   │                           ▼                                     │   │
│   │  ┌─────────────────────────────────────────────────────────┐    │   │
│   │  │  beliefs                                                │    │   │
│   │  │  ────────                                               │    │   │
│   │  │  propositions treated as true                           │    │   │
│   │  │  confidence (0–1), evidence_iters[]                     │    │   │
│   │  │  status ∈ {active, retired, contradicted}               │    │   │
│   │  │  last_validated_at                                      │    │   │
│   │  │  embedding (vec384)                                     │    │   │
│   │  └────────────────────────┬────────────────────────────────┘    │   │
│   │                           │ N related beliefs cohere            │   │
│   │                           ▼                                     │   │
│   │  ┌─────────────────────────────────────────────────────────┐    │   │
│   │  │  understandings                                         │    │   │
│   │  │  ──────────────                                         │    │   │
│   │  │  causal explanations                                    │    │   │
│   │  │  supporting_belief_ids[]                                │    │   │
│   │  │  embedding (vec384)                                     │    │   │
│   │  └────────────────────────┬────────────────────────────────┘    │   │
│   │                           │ M understandings in a region        │   │
│   │                           ▼                                     │   │
│   │  ┌─────────────────────────────────────────────────────────┐    │   │
│   │  │  intuitions                                             │    │   │
│   │  │  ───────────                                            │    │   │
│   │  │  soft priors, bias the planner                          │    │   │
│   │  │  weight (0–1), last_used_at                             │    │   │
│   │  │  embedding (vec384)                                     │    │   │
│   │  └─────────────────────────────────────────────────────────┘    │   │
│   │                                                                 │   │
│   │  ┌─────────────────────────────────────────────────────────┐    │   │
│   │  │  configurations                                         │    │   │
│   │  │  ───────────────                                        │    │   │
│   │  │  named, saved configs that beat prior-best              │    │   │
│   │  │  workflow_json, param_dict, best_score, std             │    │   │
│   │  │  sample_image_path                                      │    │   │
│   │  │  sample_embedding (CLIP, vec512) ◀── visual-similarity  │    │   │
│   │  │  parent_config_id (lineage)                             │    │   │
│   │  └─────────────────────────────────────────────────────────┘    │   │
│   │                                                                 │   │
│   └─────────────────────────────────────────────────────────────────┘   │
│                                                                         │
└─────────────────────────────────────────────────────────────────────────┘

  Promotion path (epistemic flow):

      Planner emits        Iterations test       Reflector promotes
      ──────────────        ──────────────       ──────────────────
        THOUGHTS    ─────▶   evidence accrues  ─────▶  BELIEFS
                                                          │
                                                          │ clustering
                                                          ▼
                                                    UNDERSTANDINGS
                                                          │
                                                          │ accumulation
                                                          ▼
                                                      INTUITIONS

      Refutation flow (just as important):

        THOUGHTS    ─────▶   counter-evidence  ─────▶  status=refuted
        BELIEFS     ─────▶   re-validation fails ───▶  status=contradicted
        INTUITIONS  ─────▶   biased moves fail  ────▶  weight decays
```

---

## 5. Anatomy of one iteration

Concretely, here is iter #007 in a comfyui-character run:

```
ITER 007  (parent: iter 003, the Mickmumpitz-CCC-3.8 baseline)
───────────────────────────────────────────────────────────────

  ┌──────────── PLANNER (claude -p, ~30s) ────────────┐
  │                                                   │
  │  reads world-model snapshot:                      │
  │   - top-5 beliefs by recency × confidence         │
  │   - all active intuitions                         │
  │   - 3 configs nearest goal in CLIP space          │
  │   - thoughts.status='refuted' (to avoid)          │
  │                                                   │
  │  reads references/seed.jsonl                      │
  │                                                   │
  │  writes thought.json:                             │
  │  {                                                │
  │    "category": "modify-current-best",             │
  │    "rationale": "iter 003 had arcface 0.75 std    │
  │      0.04. Belief #12 says PuLID id_weight > 0.9  │
  │      stiffens expressions. Try 0.85 with shift    │
  │      raised to 3.2 to compensate.",               │
  │    "config_changes": {                            │
  │      "pulid.id_weight": 0.85,                     │
  │      "aurashift": 3.2                             │
  │    },                                             │
  │    "cites": ["belief:12", "config:003"],          │
  │    "expected_delta": "+0.03 mean, +0.02 FC",      │
  │    "estimated_pod_minutes": 75                    │
  │  }                                                │
  │                                                   │
  └────────────────────────┬──────────────────────────┘
                           │
  ┌──────────── ACTOR (claude -p, ~2 min before exit) ┐
  │                                                   │
  │  reads thought.json + config:003                  │
  │                                                   │
  │  patches config_schema.py instance:               │
  │    config = config_003.copy_with(                 │
  │      pulid_id_weight=0.85, aurashift=3.2)         │
  │                                                   │
  │  renders → workflow.json                          │
  │                                                   │
  │  submits to ComfyUI on pod:                       │
  │    for prompt in eval/prompts.json (5):           │
  │      for seed in [101, 202, 303]:                 │
  │         POST /prompt {workflow, seed, prompt}     │
  │   15 prompt IDs returned                          │
  │                                                   │
  │  writes pending_job.json:                         │
  │  {                                                │
  │    "pod_id": "abc123",                            │
  │    "prompt_ids": [...15...],                      │
  │    "expected_complete_at": "2026-05-22T03:15Z",   │
  │    "iter_id": 7                                   │
  │  }                                                │
  │                                                   │
  │  EXITS. iter state → AWAITING_REMOTE              │
  │                                                   │
  └────────────────────────┬──────────────────────────┘
                           │
  ┌────────── ORCHESTRATOR POLLING (~75 min wall) ────┐
  │                                                   │
  │  every 5 min:                                     │
  │    GET /v1/jobs/<job_id> on the pod               │
  │    states: rendering | judging | done | failed    │
  │                                                   │
  │  also runs every poll:                            │
  │    - pod-leak guard (kill if > budget × 1.5)      │
  │    - drift check (every 5 iters: re-score 2       │
  │      historical anchor images; alert if           │
  │      |Δscore| > 0.3)                              │
  │                                                   │
  └────────────────────────┬──────────────────────────┘
                           │
  ┌────── JUDGE (Opus 4.7, runs ON THE POD, ~90s) ────┐
  │                                                   │
  │  ensemble eval, runs on the pod.                  │
  │  5 components, each independent.                  │
  │  NO single LLM "judge" — Opus is only one signal. │
  │                                                   │
  │  inputs (all on pod local disk):                  │
  │    rubric.md (narrowed, sha256 checksummed)       │
  │    visible/brooke_{1,2,3}.png  (cached on volume) │
  │    held_out/brooke_{4,5}.png   (cached on volume) │
  │    identity.png                (cached on volume) │
  │    /tmp/iter_007/*.png (15 candidates)            │
  │                                                   │
  │  for each of the 15 candidates:                   │
  │    arcface.py    → cosine to identity.png         │
  │    dinov2.py     → patch sim to nearest Brooke    │
  │    clip_sim.py   → CLIP sim to visible Brookes    │
  │    anatomy.py    → MediaPipe hand/eye sanity      │
  │    opus_rubric.py → 1 Anthropic call, 4 narrowed  │
  │                     dims (lighting, scene,        │
  │                     candid, micro_expression)     │
  │                                                   │
  │  compose.py applies:                              │
  │    gates: arcface ≥ 0.55, anatomy ≥ 0.90          │
  │    weighted composite on survivors                │
  │    picks winner = max composite                   │
  │                                                   │
  │  output → POSTed back to Mac:                     │
  │    scores.json:                                   │
  │      per_image[15]: {arcface, dinov2, clip,       │
  │                       anatomy, opus_rubric,       │
  │                       composite, gates_passed}    │
  │      per_signal: mean/std/min/max                 │
  │      gate_failures: count per gate                │
  │      drift_check: anchor re-scores                │
  │      rubric_checksum                              │
  │      winner_index                                 │
  │    winner.webp  (~150 KB)                         │
  │    workflow.json (the config used)                │
  │                                                   │
  │  Mac writes:                                      │
  │    iters/iter_007/winner.webp                     │
  │    iters/iter_007/scores.json                     │
  │    iters/iter_007/workflow.json                   │
  │    (≈ 150 KB total per iter on Mac)               │
  │                                                   │
  │  pod /tmp/iter_007/ wiped. losers gone forever.   │
  │                                                   │
  └────────────────────────┬──────────────────────────┘
                           │
  ┌──────────── REFLECTOR (claude -p, ~45s) ──────────┐
  │                                                   │
  │  reads thought.json + scores.json + world model   │
  │                                                   │
  │  outcome: composite 8.74 (Δ +0.18),               │
  │           arcface_mean 0.79 (Δ +0.04),            │
  │           std 0.32, 0 gate failures.              │
  │           Expected delta hit.                     │
  │                                                   │
  │  world-model writes (via MCP tools):              │
  │    confirm_thought(N)                             │
  │    add_belief(                                    │
  │      content="PuLID id_weight 0.85 + shift 3.2    │
  │       beats 0.92/2.01: arcface +0.04 with no      │
  │       loss on dinov2/anatomy",                    │
  │      confidence=0.65,                             │
  │      evidence_iters=[7])                          │
  │    save_configuration(                            │
  │      name="mickmumpitz-tuned-007",                │
  │      parent="config:003",                         │
  │      composite=8.74, std=0.32,                    │
  │      arcface_mean=0.79,                           │
  │      sample_path="iters/iter_007/winner.webp")    │
  │                                                   │
  │  costs ledger writes (this iter):                 │
  │    pod_seconds: 4523                              │
  │    pod_dollars: $2.08  (A40 @ $1.65/hr)           │
  │    judge_dollars: $0.31  (15 Opus calls)          │
  │    agent_tokens: 18,450 in / 2,890 out            │
  │    total_dollars: $2.55                           │
  │                                                   │
  │  iter state → DONE                                │
  │                                                   │
  └───────────────────────────────────────────────────┘
```

---

## 6. Credentials and filesystem — what lives where

```
USER HOME (~)
─────────────
│
├── .agentic-autoresearch/                  framework's home, OUTSIDE any
│   │                                       project dir, never git-tracked
│   │
│   ├── credentials.toml         600 ◀──── THE secrets file
│   │     [runpod]      api_key, default_datacenter, default_gpu,
│   │                   network_volume_id
│   │     [anthropic]   api_key (planner, actor, reflector, judge)
│   │     [huggingface] token
│   │     [gemini]      api_key (legacy, unused by comfyui)
│   │     [openai]      api_key (optional)
│   │
│   ├── config.toml              644      non-secret defaults: dashboard
│   │                                     port, poll interval, model IDs,
│   │                                     log level
│   │
│   ├── projects/                         per-problem isolation
│   │   ├── omr/
│   │   │   ├── db.sqlite                 (migrated from old global db)
│   │   │   └── iters/
│   │   ├── comfyui-character/
│   │   │   ├── db.sqlite                 fresh world model
│   │   │   ├── iters/                    ~150KB/iter — winner ONLY
│   │   │   │   ├── iter_001/
│   │   │   │   │   ├── winner.webp       ~150KB, best of 15 seeds
│   │   │   │   │   ├── scores.json       includes losing seeds' scores
│   │   │   │   │   └── workflow.json     the config that produced it
│   │   │   │   └── iter_NNN/
│   │   │   └── pods/
│   │   │       └── active.json           live pod manifests
│   │   │                                 (leak detection on startup)
│   │   └── <future-problem>/
│   │
│   └── logs/
│       └── aar-YYYY-MM-DD.log            secrets redacted at write
│
└── Documents/
    │
    └── agentic-autoresearch/              the framework REPO
        │
        ├── src/agentic_autoresearch/
        │   ├── cli/                       autoresearch <verb>
        │   ├── orchestrator/              state machine
        │   ├── agents/
        │   │   ├── planner.py
        │   │   ├── actor.py
        │   │   ├── judge.py               Opus 4.7 vision
        │   │   ├── reflector.py
        │   │   └── runpod.py              headless pod lifecycle
        │   ├── memory/                    world-model schema + queries
        │   ├── credentials/               reads ~/.aar/credentials.toml
        │   ├── guardrails/
        │   ├── resume/
        │   └── obs/                       dashboard
        │
        ├── problems/
        │   ├── hello-world/               bundled self-test
        │   └── comfyui-character/
        │       ├── spec.md                NO SECRETS. declares
        │       │                          requires_credentials: [...]
        │       ├── eval/
        │       │   ├── score.py           vision-judge wrapper
        │       │   ├── prompts.json       5 fixed eval prompts
        │       │   └── references/
        │       │       ├── visible/brooke_{1,2,3}.png
        │       │       ├── held_out/brooke_{4,5}.png
        │       │       └── identity.png
        │       ├── references/
        │       │   └── seed.jsonl         3 validated 2026 stacks
        │       ├── workflows/
        │       │   ├── flux-pulid.json
        │       │   ├── qwen-edit.json
        │       │   └── sdxl-ipadapter-controlnet.json
        │       └── src/comfyui_character/
        │           ├── runner.py          THE mutable file (actor edits)
        │           └── config_schema.py   typed config dict
        │
        └── .claude/plans/
            ├── comfyui-character-and-framework.md   THE plan
            └── diagrams.md                          THIS file
```

---

## 7. Credential flow — never leaks past where it must

```
                ~/.agentic-autoresearch/credentials.toml  (600)
                              │
                              │ read once by CLI on startup
                              ▼
              ┌───────────────────────────────────┐
              │     credentials loader            │
              │     (in-memory dict, no logging)  │
              └─────┬─────────────────────┬───────┘
                    │                     │
                    │ orchestrator        │ short-lived agent procs
                    │ (long-lived)        │ get env= only for the call
                    ▼                     ▼
        ┌──────────────────┐    ┌──────────────────────────┐
        │ RunPod API calls │    │ subprocess.run(           │
        │ Anthropic API    │    │   ['claude','-p',...],    │
        │ HuggingFace API  │    │   env={'ANTHROPIC_API_KEY'│
        └──────────────────┘    │         : '...'},         │
                                │   stdin/stdout piped)     │
                                └──────────────────────────┘

  HARD INVARIANTS:
  ───────────────
  1. spec.md NEVER contains keys, tokens, pod IDs, or volume IDs.
  2. logs/ writes are passed through a redactor that masks any string
     matching credential VALUES known at startup.
  3. No `.env` files anywhere under problems/ or src/.
  4. No shell-exported env vars set by the framework itself
     (so other tools running in the user's shell never see them).
  5. agent subprocesses get ONLY the keys they need for the API
     they're about to hit. The reflector never sees RUNPOD_API_KEY,
     for example.
```

---

## 8. The two budgets and when each kills the run

```
                ┌────────────────────────────────────┐
                │                                    │
                │      KILL CONDITIONS (any one)     │
                │                                    │
                ├────────────────────────────────────┤
                │                                    │
                │  1. TARGET REACHED                 │
                │     mean composite ≥ 8.5/10        │
                │     AND arcface_mean ≥ 0.75        │
                │     AND std ≤ 0.4                  │
                │     AND ≤1 gate fail of 15         │
                │     → exit "target_reached"        │
                │                                    │
                │  2. BUDGET EXHAUSTED               │
                │     cumulative pod_hours ≥ 30      │
                │     OR cumulative judge $ ≥ 50     │
                │     → exit "budget_exhausted"      │
                │                                    │
                │  3. PLATEAU                        │
                │     5 consecutive iters with       │
                │     Δmean < 1.5σ                   │
                │     → exit "plateau"               │
                │                                    │
                │  4. JUDGE DRIFT                    │
                │     held-out anchor re-score       │
                │     drift |Δ| > 0.3                │
                │     → PAUSE, alert user, await     │
                │       recalibration                │
                │                                    │
                │  5. POD LEAK / HARD FAILURE        │
                │     pod orphaned > budget × 1.5    │
                │     OR 3 consecutive FAILED iters  │
                │     → exit "aborted"               │
                │                                    │
                └────────────────────────────────────┘
```

---

## 9. Goodhart guards — the anti-self-gaming map

The biggest risk: planner / actor / Opus-judge are all Claude. The eval
defense-in-depth has six layers, none sufficient alone. The most
important is that the LLM judge is ONLY 25% of the composite score —
the other 75% is objective metrics (ArcFace, DINOv2, CLIP, anatomy).

```
                  ATTACK SURFACE                  DEFENSE
                  ──────────────                  ───────

   1.  agent reads judge's previous          judge runs in a FRESH
       rationales and rewrites prompts        `claude -p` with no project
       to elicit higher scores                ctx, no memory access, no
                                              system-prompt hints about
                                              the project. only the
                                              rubric + images.

   2.  reference images shown in              held_out/brooke_{4,5}.png
       planner context get encoded into       are NEVER visible to
       prompts                                planner or actor. judge
                                              sees them; world model
                                              never stores their
                                              contents, only the scores
                                              they produced.

   3.  judge drifts over time                 every 5 iters, re-score 2
       (model updates, prompt variance)       fixed historical anchor
                                              images. if |Δ| > 0.3 vs
                                              their last recorded score,
                                              PAUSE the run.

   4.  k=1 noise mistaken for                 every iter scores k=3
       improvement                            seeds; improvement
                                              requires Δmean > 1.5σ.

   5.  agent learns the rubric's              rubric is pinned per run
       weighting and games one                in run.json checksum'd at
       dimension                              start. judge gets the
                                              checksummed rubric, not
                                              one regenerated each call.

   6.  agent finds a prompt style             ENSEMBLE EVAL: composite
       that Opus-rubric loves but             score requires high
       that ignores identity or               arcface AND high dinov2
       has melted hands                       AND high clip AND high
                                              anatomy. Opus is only
                                              25% of weight; ArcFace
                                              and anatomy are HARD
                                              GATES — fail them, the
                                              candidate is rejected
                                              regardless of Opus.
```

---

## 10. What the dashboard shows at `:8765`

```
┌──────────────────────────────────────────────────────────────────────────┐
│  agentic-autoresearch                         comfyui-character  ▼       │
├──────────────────────────────────────────────────────────────────────────┤
│                                                                          │
│  RUN  d3f0461b   started 2026-05-21 14:42   running    iter 7 / ?        │
│                                                                          │
│  ┌──────────────────────────────────┐   ┌────────────────────────────┐   │
│  │  COMPOSITE OVER TIME             │   │  COST LEDGER               │   │
│  │                                  │   │                            │   │
│  │  10 ┤              ─ target 8.5  │   │   pod-hours used: 6.2/30   │   │
│  │   8 ┤            ▄▄ ▄▄  ▄        │   │   pod $:        $10.23     │   │
│  │   6 ┤   ▄▄ ▄▄ ▄▄                 │   │   judge $:       $2.45/50  │   │
│  │   4 ┤▄▄                          │   │   agent tok in:  142k      │   │
│  │   2 ┤                            │   │   agent tok out: 22k       │   │
│  │     └──┬──┬──┬──┬──┬──┬──┬       │   │                            │   │
│  │        1  2  3  4  5  6  7 iter  │   │  TOTAL: $13.41             │   │
│  │                                  │   │                            │   │
│  │  arcface (FC anchor):            │   │  GATES (this run)          │   │
│  │   1.0┤            ▄▄ ▄▄  ▄       │   │   arcface fails: 2 of 105  │   │
│  │   0.8┤   ▄▄ ▄▄ ▄▄                │   │   anatomy fails: 0 of 105  │   │
│  │   0.6┤▄▄          ─ gate 0.55    │   │                            │   │
│  │   0.4┤                           │   │                            │   │
│  │      └──┬──┬──┬──┬──┬──┬──┬      │   │                            │   │
│  │         1  2  3  4  5  6  7 iter │   │                            │   │
│  └──────────────────────────────────┘   └────────────────────────────┘   │
│                                                                          │
│  ┌──────────────────────────────────────────────────────────────────┐    │
│  │  WORLD MODEL (live)                                              │    │
│  │  ──────────────                                                  │    │
│  │  beliefs (active):  12      thoughts in flight:   1              │    │
│  │  understandings:     3      intuitions:           2              │    │
│  │  configurations:     4      best: mickmumpitz-tuned-007 (8.74)   │    │
│  │                                                                  │    │
│  │  TOP BELIEF (recency × confidence):                              │    │
│  │  > "PuLID id_weight 0.85 + shift 3.2 beats 0.92/2.01 on FC"      │    │
│  │    conf 0.65, evidence [iter 7]                                  │    │
│  │                                                                  │    │
│  │  CURRENT INTUITIONS:                                             │    │
│  │  > "for night-context prompts, prefer warmer color temp"  w 0.7  │    │
│  │  > "stack height > 2 LoRAs degrades face consistency"     w 0.6  │    │
│  └──────────────────────────────────────────────────────────────────┘    │
│                                                                          │
│  ┌──────────────────────────────────────────────────────────────────┐    │
│  │  POD STATUS                                                      │    │
│  │  ──────────                                                      │    │
│  │  pod abc123    A40    EU-RO-1    running    cumulative 6h 11m    │    │
│  │  comfyui :8188   reachable   last image 3 min ago                │    │
│  └──────────────────────────────────────────────────────────────────┘    │
│                                                                          │
└──────────────────────────────────────────────────────────────────────────┘
```

---

## 11. Putting it together — one run, top to bottom

```
TIME    EVENT
────    ─────

T+0     user runs `autoresearch run comfyui-character`
T+1s    CLI loads credentials.toml, validates spec
T+2s    orchestrator starts, opens per-project DB
T+5s    iter 1 PLANNED (forced: mickmumpitz-ccc-3.8 baseline)
T+30s   actor submits 15-image job to RunPod
T+1h15m judge ran on pod; winner.webp + scores.json POSTed (~150KB)
T+1h17m REFLECTING
T+1h18m reflector saves config "mickmumpitz-ccc-3.8"
                              (composite 8.31 ±0.35, arcface 0.71)
                              → DONE
T+1h18m iter 2 PLANNED (forced: qwen-image-edit-one-headshot)
        … (forced iters 2 and 3, same shape) …
T+4h    iter 4 PLANNED (PLANNER NOW FREE)
        - planner reads world model: 3 configs, ~6 initial beliefs
        - planner cites belief #2: "Flux backbones outscore SDXL on
          skin realism by ~0.4"
        - proposes a Flux-based modification of the best so far
T+5h30m iter 4 done. composite 8.55 (arcface 0.78).
                       belief #2 confidence rises to 0.75.
…
T+22h   iter 14. world model has 12 beliefs, 3 understandings,
        2 intuitions, 4 saved configs. best composite 8.74.
T+24h   iter 15. plateau detector: 5 iters without Δ > 1.5σ.
                 → exit "plateau"

OUTCOME:
   best config: "mickmumpitz-tuned-007"
                workflow.json + param_dict
                composite 8.74 ± 0.32 (arcface 0.79)
   total cost:  $24.10  (6 of 30 pod-hours, $9 of $50 judge)
   world model: queryable for the next run
   report:      ~/.aar/projects/comfyui-character/REPORT.md
                "we hit 8.74 ± 0.32 against an 8.5 target — passed,
                 but arcface plateaued at 0.79 (target 0.75 met but
                 no headroom). next investment: qfloat8 Elena retrain
                 (belief #8) — replace LoRA layer in
                mickmumpitz-tuned-007 and re-run."
```

That report is the deliverable. The world-model DB is the asset that
compounds. The framework is the thing that, by surviving this run, is
ready for the next problem.
