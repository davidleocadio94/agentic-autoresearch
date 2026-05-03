"""`autoresearch new <name>` — scaffold a new problem repo."""

from __future__ import annotations

from pathlib import Path

SPEC_TEMPLATE = """\
---
name: {name}
exit:
  target_score: 1.0
  max_hours: 24
  max_iters: 200
constraints:
  language: python
  manager: uv
  forbid_gui: true
  max_repo_bytes: 2_000_000_000
  forbidden_deps: []
  immutable_paths:
    - eval/score.py
    - eval/fixtures/**
eval:
  kind: custom
  scorer: eval/score.py
---

# What we're building

(Describe the problem in 1-3 sentences. The PLANNER reads this every iter
to keep the experiments grounded in what you actually want.)

# How to evaluate

The framework runs `python eval/score.py --json <output>` once per iteration.
That script must write a JSON file with at least:

```json
{{"score": 0.0, "per_fixture": {{"name": 0.0}}}}
```

Score must be in [0.0, 1.0]. The loop stops when score >= target_score.

# Optional: starter notes for agents

(Anything you've already tried, things to avoid, hints. The PLANNER reads
this on every iter — so this is the right place to leave breadcrumbs.)
"""

SCORER_TEMPLATE = '''\
"""Eval scorer — replace this stub with your real metric.

Required output: a JSON file with shape:
    {"score": 0.0..1.0, "per_fixture": {"<fixture-name>": 0.0..1.0, ...}}
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def score() -> tuple[float, dict[str, float]]:
    # TODO: implement your real eval here
    return 0.0, {}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", required=True, type=Path)
    args = ap.parse_args()
    overall, per_fixture = score()
    args.json.parent.mkdir(parents=True, exist_ok=True)
    args.json.write_text(json.dumps({"score": overall, "per_fixture": per_fixture}, indent=2))
    print(f"score: {overall:.4f}")


if __name__ == "__main__":
    main()
'''

GITIGNORE = """\
__pycache__/
.venv/
.python-version
.ruff_cache/
.pytest_cache/

# framework runtime
.agentic-autoresearch-worktrees/
.autoresearch_eval.json
"""


def scaffold(name: str, parent: Path) -> Path:
    """Create <parent>/<name>/ with spec.md + eval skeleton."""
    repo = parent / name
    if repo.exists():
        raise FileExistsError(f"{repo} already exists")
    (repo / "eval" / "fixtures").mkdir(parents=True)
    (repo / "spec.md").write_text(SPEC_TEMPLATE.format(name=name))
    (repo / "eval" / "score.py").write_text(SCORER_TEMPLATE)
    (repo / "eval" / "fixtures" / ".gitkeep").touch()
    (repo / ".gitignore").write_text(GITIGNORE)
    return repo
