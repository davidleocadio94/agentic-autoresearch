# agentic-autoresearch

Plug in a `spec.md`, get a working repo. The framework runs a Plan/Act/Reflect
loop with `claude -p` headless agents until your eval reaches the target score.

```
PROBLEM SPEC                                     WORKING REPO
(spec.md: eval, budget,                ──────▶  (passes the eval,
 constraints)                                    satisfies guardrails)
```

## Install

```bash
uv sync
```

## Quickstart

```bash
# scaffold a new problem
autoresearch new my-problem

# edit my-problem/spec.md and my-problem/eval/score.py

# run
autoresearch run my-problem

# self-test on bundled hello-world
autoresearch self-test
```

## Pieces

```
src/agentic_autoresearch/
  orchestrator/  Plan/Act/Reflect loop, git worktrees, state machine
  agents/        claude -p spawn helpers (stream-json, deadlines)
  memory/        sqlite + sqlite-vec, runs/iterations/artifacts
  obs/           FastAPI + htmx dashboard at localhost:8765
  guardrails/    constraints enforcement (immutable paths, dep limits, size)
  resume/        heartbeat + state-machine-driven resume
  cli/           autoresearch new/run/resume/stop/self-test
problems/
  hello-world/   bundled self-test
```

See `.claude/plans/autoresearch-framework.md` (carried over from the source
repo) for the full design rationale.
