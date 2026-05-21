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
contexts, reaching **mean composite ≥ 8.5 / 10** on an ensemble eval
(ArcFace + DINOv2 + CLIP + anatomy + narrowed Opus rubric, joined by
gates), with **arcface_mean ≥ 0.75**, **composite std ≤ 0.4** across
15 candidates, and **≤ 1 gate failure of 15**, within **30 pod-hours
and a hard $50 judge-cost cap**.

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
4. Ensemble eval                 ArcFace + DINOv2 + CLIP + anatomy +
   (objective + narrowed LLM)    narrowed Opus rubric, joined by gates.
                                 Opus is 25% of weight, not 100%.
                                 Eval runs on the pod; only the winner
                                 image + scores vector return to the Mac.
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
      iters/
    comfyui-character/
      db.sqlite             # NEW — fresh world model for this problem
      iters/                # ONLY winner + scores per iter — see below
        iter_001/
          winner.webp       # ~150KB, the best of k=3 seeds
          scores.json       # 2KB, full rubric output incl. losing seeds' scores
          workflow.json     # the config that produced the winner
        iter_NNN/...
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
# in comfyui-experiments/spec.md frontmatter
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
                                       │ POLLING  │ judge ran ON THE POD,
                                       └────┬─────┘ scored 15 in place
                                            ▼ winner + scores.json ready
                                       ┌──────────┐
                                       │DOWNLOAD  │ pull JUST the winner
                                       └────┬─────┘ (~150KB) + scores.json
                                            ▼      (~2KB). nothing else.
                                       ┌──────────┐
                                       │REFLECTING│ fresh claude -p
                                       └────┬─────┘ updates world model
                                            ▼
                                       ┌──────────┐
                                       │   DONE   │
                                       └──────────┘
```

Key property: a 4-hour pod batch consumes ~0 Claude-session minutes
between submit and results, and ~150KB of Mac disk per iter. The judge
runs **on the pod** (where the 15 candidate images already live) — no
30MB round-trip just so a remote Claude can score images that were
generated on the pod in the first place. Three `claude -p` calls happen
per iter (actor for submit, judge on the pod for scoring, reflector for
world-model update), each short. The orchestrator (a single long-lived
Python process on the Mac) owns the polling and state transitions and
only ever sees the winner image.

---

## The eval: ensemble of objective metrics + narrowed Opus judge, joined by gates

A single LLM judge with a single weighted rubric is too gameable, especially
when the planner, actor, and judge are all Claude. The eval is therefore an
**ensemble of independent signals** — most of them objective (numerical,
model-based, no LLM) — joined by **hard gates** on deal-breakers and a
**weighted composite** on what survives the gates.

The whole eval runs on the pod, against the 15 candidate images ComfyUI
rendered to local disk. Only the winner + the scores vector come back to
the Mac.

### The five signals (first-run scope)

```
SIGNAL                          OBJECTIVE?   WHAT IT CATCHES
──────                          ──────────   ───────────────

1. ArcFace cosine               ✓            Identity match. The face
   distance vs identity.png                  consistency anchor. Hard
   (insightface, on the pod)                 to game without actually
                                             matching the identity.

2. DINOv2 patch similarity      ✓            Texture-level similarity
   to nearest Brooke ref                     to the aesthetic. Catches
                                             "looks Brooke-ish but skin
                                             wrong" — texture / pore
                                             realism by proxy.

3. CLIP image-image             ✓            Coarse aesthetic similarity
   similarity vs the 3                       to Brooke refs. Cheap,
   visible Brooke refs                       robust, low-resolution.

4. Anatomy sanity check         ✓            Finger count, eye keypoint
   (MediaPipe + heuristics)                  symmetry, no melted hands.
                                             A hard fail signal: an
                                             image with 7 fingers is
                                             never acceptable regardless
                                             of how pretty the face is.

5. Opus 4.7 narrowed rubric     ✗            What metrics can't catch:
   (the only LLM call,                       lighting coherence, scene
   on the pod, no                            plausibility, candid feel,
   project context)                          micro-expression realism.
                                             ~20% of the weight, not 100%.
```

Deliberately skipped for first run, on the future-work list:
SkinTextureNet (needs a labeled dataset), AI-detector classifier (arms
race, unstable), EXIF/JPEG-cycle realism (~200 LOC, marginal signal).

### Gates — hard rejection before any weighting

```
GATE                          THRESHOLD     WHY IT'S A GATE
────                          ─────────     ───────────────
arcface_cosine                ≥ 0.55        wrong person = useless
                                            regardless of beauty
anatomy_sanity                ≥ 0.90        melted hand = unshippable
ai_detector_confidence        ≤ 0.80        (future: when added)
                                            obvious AI = unshippable
```

Without gates, the loop optimizes weighted sums and produces
"beautiful pictures of the wrong person" or "beautiful pictures with
seven fingers." Gates fail fast on deal-breakers, then the weighted
composite tunes the niceties.

### Composite score — on what survives the gates

```
score_0_to_10 = 10 × (
    0.40 × arcface_normalized        # in [0, 1], (cos - 0.55) / 0.45
  + 0.25 × opus_rubric_0_to_1        # the LLM dimension, narrowed
  + 0.20 × dinov2_patch_mean         # already in [0, 1]
  + 0.15 × clip_image_mean           # already in [0, 1]
)
```

The Opus judge's NARROWED rubric (4 dimensions, equal weight,
internal avg):

```
1. lighting_coherence       1–10   shadows physically consistent,
                                   light direction matches scene
2. scene_plausibility       1–10   environment feels real, not stock
3. candid_authenticity      1–10   "candid phone photo" not "studio"
4. micro_expression         1–10   subtle muscle activation, real
                                   expression rather than mannequin
```

That's it for the LLM. No more "judge the whole image" — it scores
only what metrics can't reach.

### What flows where

```
POD                                           MAC
───                                           ───
/tmp/iter_NNN/{15 candidates}                 (nothing yet)
   │
   ▼
eval/ pipeline (on the pod):
  ─ arcface.py        → per-image cosine to identity.png
  ─ dinov2.py         → per-image patch similarity to Brooke refs
  ─ clip_sim.py       → per-image CLIP similarity to Brooke refs
  ─ anatomy.py        → per-image sanity score (MediaPipe)
  ─ opus_rubric.py    → ONE Anthropic call per image, narrowed rubric
                         (rubric.md pinned + checksummed per run)
  ─ compose.py        → applies gates, then weighted composite
                         emits scores.json (the full vector)
                         picks winner = highest composite score
                         among gate-passers
   │
   │ POSTs to orchestrator:                ┌─▶ ~/.aar/projects/<p>/
   ▼                                       │    iters/iter_NNN/
winner.webp     (~150 KB) ─────────────────┤      winner.webp
scores.json     (~3 KB)   ─────────────────┤      scores.json
workflow.json   (the config used)  ────────┘      workflow.json
                                                  per iter on Mac: ~150 KB
/tmp/iter_NNN/ deleted
```

### scores.json shape (richer than before)

```json
{
  "composite": 8.7,
  "winner_index": 11,
  "per_image": [
    {
      "seed": 101, "prompt_idx": 0,
      "arcface": 0.78, "dinov2": 0.71, "clip": 0.66,
      "anatomy": 0.98, "opus_rubric": 0.81,
      "composite": 8.5,
      "gates_passed": ["arcface", "anatomy"],
      "gates_failed": []
    },
    "... 14 more ..."
  ],
  "per_signal": {
    "arcface":     {"mean": 0.75, "std": 0.04, "min": 0.61, "max": 0.84},
    "dinov2":      {"mean": 0.69, "std": 0.05, "min": 0.58, "max": 0.78},
    "clip":        {"mean": 0.64, "std": 0.06, "min": 0.50, "max": 0.74},
    "anatomy":     {"mean": 0.96, "std": 0.03, "min": 0.88, "max": 1.00},
    "opus_rubric": {"mean": 0.78, "std": 0.07, "min": 0.62, "max": 0.92},
    "composite":   {"mean": 8.3, "std": 0.4, "min": 7.1, "max": 8.7}
  },
  "gate_failures": {
    "arcface": 1,    // one seed missed the identity gate
    "anatomy": 0,
    "ai_detector": 0
  },
  "drift_check": {
    "anchor_1_composite": 7.9,  // historical: 7.8 → drift +0.1 (ok)
    "anchor_2_composite": 8.4,  // historical: 8.5 → drift -0.1 (ok)
    "anchor_threshold": 0.3
  },
  "rubric_checksum": "sha256:abc...",  // pin verification
  "metadata": {
    "judge_model": "claude-opus-4-7",
    "arcface_model": "buffalo_l",
    "dinov2_model": "dinov2_vitb14",
    "clip_model": "ViT-L-14/openai",
    "anatomy_model": "mediapipe_face_v0.10"
  }
}
```

This vector is what the reflector sees. It can now write beliefs like
"configs with high CLIP but low DINOv2 score high on opus_rubric but
fail the texture eye-test" — that's structurally richer than "score
went up 0.02."

### Targets, in this scoring scheme

```
TARGET                              CURRENT BEST (estimated re-scored)
──────                              ─────────────────────────────────
mean composite        ≥ 8.5/10      ~7.8 (your 4.94/5 → ~7.8/10 in
                                     the new scheme, give or take)
arcface_mean          ≥ 0.75        ~0.68 (FC was your bottleneck)
std (composite)       ≤ 0.4
gate_failures total   ≤ 1 of 15     (one bad seed allowed; two means
                                     the config is unreliable)
```

### Goodhart layers, all enforced

```
LAYER                                 HOW
─────                                 ───
rubric pinned                         rubric.md sha256 checksummed at run
                                       start; judge gets the checksum and
                                       refuses to score if mismatched
gates that bypass weighting           you can't rhetorically game ArcFace —
                                       it's a face-embedding distance
held-out anchor re-score              every 5 iters, two fixed images
                                       re-scored; |Δcomposite| > 0.3 pauses
no single signal > 40% weight         no one component can dominate
opus judge has no project context     fresh process, only rubric + images
opus judge is only 25% of weight      LLM gaming has limited blast radius
```

### Opt-out for debugging

`autoresearch run <p> --keep-all-candidates` streams every candidate
back to the Mac for that run. **Default off.** Only enable when
inspecting why a config produced a specific bad output.

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

## ComfyUI problem scaffold (external private repo)

The comfyui work lives in its own private repo,
`github.com/davidleocadio94/comfyui-experiments`, NOT under
`problems/` in this repo. The framework operates on any external
problem directory via `autoresearch run <path>`. This keeps the
framework public/portfolio-friendly and the experiment data private.

```
~/Documents/comfyui-experiments/   (private repo)
  spec.md                   # problem statement, eval, budget, kill
                            # conditions, requires_credentials
  eval/
    rubric.md               # the Opus-judged dimensions, checksummed.
                            # only the 4 narrowed dimensions (lighting,
                            # scene, candid_authenticity, micro_expression)
    arcface.py              # insightface buffalo_l, cosine vs identity.png
    dinov2.py               # patch similarity vs Brooke refs
    clip_sim.py             # CLIP image-image similarity
    anatomy.py              # MediaPipe hand/finger/eye sanity
    opus_rubric.py          # the LLM call: rubric.md + images → 4 scores
    compose.py              # gates + weighted composite → scores.json
                            # this is the entrypoint the framework calls
                            # ($EVAL_ENTRYPOINT in spec.md)
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
  judge_dollars: 50          # anthropic spend on Opus 4.7 (the LLM
                             # component of the eval) + planner/actor/
                             # reflector together
target:
  mean_composite: 8.5        # on [0, 10]
  arcface_mean: 0.75         # the FC anchor, above its 0.55 gate
  max_std: 0.4               # std across the 15 per-image composites
  max_gate_failures: 1       # of 15 candidates per iter
kill_conditions:
  - budget_exhausted
  - no_improvement_over_2_sigma_for: 5  # iterations
  - drift_absolute: 0.3                  # composite drift on held-out
                                          # anchors, re-scored every 5 iters
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
    │ writes once: comfyui-experiments/spec.md (in the private repo)
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

### Framework work (problem-agnostic, future problems inherit)

1. **Credentials store + CLI verb.** `autoresearch credentials add <svc>`.
   Touches nothing else. Verifiable by `autoresearch credentials list`.
2. **Per-project DB namespacing.** Move `~/.aar/db.sqlite` →
   `~/.aar/projects/omr/db.sqlite`. Existing OMR self-test must still
   pass. This is the migration gate — no other framework work merges
   until it does.
3. **World-model schema.** Add the five tables (beliefs / thoughts /
   understandings / intuitions / configurations) to the per-project DB.
   Backfill OMR's existing lessons as `beliefs` with `confidence=0.7,
   evidence_iters=[iter#]` so the schema starts populated.
4. **MCP server for world-model writes.** Tools the reflector calls:
   `add_belief`, `confirm_thought`, `refute_thought`, `add_understanding`,
   `add_intuition`, `save_configuration`. Embeddings computed server-side.
5. **Non-blocking ACT + RunPod lifecycle.** State machine
   (`awaiting_remote` / `polling`), pod-leak guards, headless start/stop.
   Self-test with a fake remote (local sleep) before pointing at real
   GPUs.
6. **Ingest layer (first cut).** `sources.yaml` schema + fetchers for
   reddit JSON, arxiv, and curated web domains. Writes
   `status='external_claim'` beliefs with `confidence=0.3`. Skip X
   scraping in the first cut.

### Comfyui-specific work

7. **Ensemble eval modules.** Six small files in
   `comfyui-experiments/eval/` (external private repo):
   - `arcface.py` — insightface buffalo_l (already on the volume)
   - `dinov2.py` — facebookresearch DINOv2 ViT-B/14
   - `clip_sim.py` — open_clip ViT-L/14
   - `anatomy.py` — MediaPipe + hand/eye heuristics
   - `opus_rubric.py` — single Anthropic call per image, narrowed rubric
   - `compose.py` — gates + weighted composite → scores.json (entrypoint)

   Each module is independently unit-testable on a tiny fixture set
   (10 already-scored historical comfyui images). Calibration step:
   verify the composite correlates positively with your historical
   Gemini scores on those fixtures before trusting it to drive a run.

8. **Pod-side runtime (`judge.py` becomes `runtime.py`).**
   Coordinates ComfyUI submission → image collection → eval pipeline
   → POST results to orchestrator. Shipped to the pod on startup.

9. **Comfyui-character problem scaffold.** spec.md, eval/rubric.md,
   prompts.json, references/ (visible + held-out + identity),
   workflows/ (3 validated 2026 stacks), `src/comfyui_character/runner.py`.
   Forced first 3 iterations on the validated stacks.

10. **First end-to-end run.** Budget 30 pod-hours, $50 cap. Walk away.

Steps 1–6 are framework work — any future problem inherits them. Steps
7–9 are the comfyui-specific scaffold. Step 10 is the test of whether
the framework now generalizes beyond OMR.

---

## What "done" looks like

For the run: a `configurations` row with mean composite ≥ 8.5/10,
arcface_mean ≥ 0.75, std ≤ 0.4, ≤1 gate failure across 15 candidates,
fully reproducible from the saved workflow JSON + param dict. **Or** a
refuted hypothesis from the world model explaining exactly which
ceiling we hit and where the next investment should go.

For the framework: six new abstractions, each demanded by a real run,
each scoped problem-agnostically. The next problem — whatever it is —
inherits async-act, per-project memory, world model, MCP-driven world-
model writes, ingest, and headless credentials for free. The eval is
problem-specific by design (it's *the* problem-specific thing), but
the contract (`compose.py` writes scores.json) is reusable.
