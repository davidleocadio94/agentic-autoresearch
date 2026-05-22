"""Local planner agent for ensemble (image-gen) problems.

Spawned as `claude -p` on the Mac. Reads the world-model snapshot
(beliefs, intuitions, refuted thoughts, saved configurations), the
seed.jsonl entries, and the budget state. Proposes ONE config_changes
dict the actor applies on the next pod iter.

Subscription-auth'd; zero API spend.

Output contract — a single JSON object fenced ```json ... ```:

{
  "category": "modify-current-best" | "try-reference-stack" |
              "replicate-with-different-seeds" | "swap-component",
  "rationale": "<must cite at least one belief/config by ID>",
  "config_changes": {"<param>": <new value>, ...},
  "parent_config": "<config name to fork from, or null>",
  "workflow_template": "<one of workflows/*.json>",
  "cites": ["belief:<id>", "config:<id>", "intuition:<id>"],
  "expected_delta": "<one phrase>"
}
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import time
from pathlib import Path

from agentic_autoresearch.agents.spawn import claude_binary, parse_json_block
from agentic_autoresearch.memory.world_model import WorldModel


PLANNER_DEADLINE = 5 * 60
PLANNER_MAX_TURNS = 15


PLANNER_PROMPT_TEMPLATE = """You are the PLANNER for an autoresearch loop on a ComfyUI character-consistency
problem. The framework has just completed iter {prev_iter} and is asking you what
to try in iter {iter_num}.

═════════════════════════════════════════════════════════
GOAL
═════════════════════════════════════════════════════════
Produce a saved ComfyUI configuration that hits:
  mean composite ≥ 8.5/10
  arcface_mean   ≥ 0.75
  std            ≤ 0.4

═════════════════════════════════════════════════════════
WORLD MODEL — what the loop has learned so far
═════════════════════════════════════════════════════════

ACTIVE BELIEFS (top by confidence × recency):
{beliefs_block}

EXTERNAL CLAIMS (ingested from reddit/arxiv/web — unverified, low conf):
{external_claims_block}

INTUITIONS (soft priors):
{intuitions_block}

SAVED CONFIGURATIONS (configs that produced concrete results):
{configurations_block}

REFUTED THOUGHTS (DO NOT re-propose these):
{refuted_thoughts_block}

═════════════════════════════════════════════════════════
AVAILABLE WORKFLOW TEMPLATES
═════════════════════════════════════════════════════════
{workflows_block}

═════════════════════════════════════════════════════════
RECENT REFLECTOR next_direction HINTS (most recent first):
═════════════════════════════════════════════════════════
{recent_directions_block}

═════════════════════════════════════════════════════════
YOUR JOB
═════════════════════════════════════════════════════════
Propose ONE experiment. Pick a category:

  - "modify-current-best": take the current best config and change
        ONE knob. Use when a belief tells you the knob is wrong.
        e.g. raise pulid_id_weight from 0.85 → 1.10.

  - "replicate-with-different-seeds": re-run a config with new seeds.
        Use when variance is suspiciously high (std > 0.5 of mean).

  - "try-reference-stack": run a fresh workflow_template. Use early
        in a campaign or when current-best is below ceiling.

  - "swap-component": substitute a model/lora/sampler. Bigger change;
        use when modify-current-best has plateaued.

You MUST cite at least one belief/config/intuition by ID in your
rationale. Be concrete: name the exact parameter and the exact value
you want changed.

OUTPUT — single JSON object, fenced ```json ... ```:

```json
{{
  "category": "modify-current-best",
  "rationale": "<2-4 sentences. MUST cite a belief/config by ID.>",
  "config_changes": {{"<param>": <value>, ...}},
  "parent_config": "<config name or null>",
  "workflow_template": "<filename from AVAILABLE WORKFLOW TEMPLATES>",
  "cites": ["belief:<id>", "config:<id>", "intuition:<id>"],
  "expected_delta": "<short phrase like '+0.05 arcface'>"
}}
```
"""


def _format_beliefs(beliefs: list, limit: int = 10) -> str:
    lines = []
    for b in beliefs[:limit]:
        lines.append(
            f"  belief:{b['id']} conf={b['confidence']:.2f} ev={len(b.get('evidence_iters', []))}iter "
            f"→ {b['content']}"
        )
    return "\n".join(lines) or "  (none yet)"


def _format_configurations(configs: list, limit: int = 8) -> str:
    lines = []
    for c in configs[:limit]:
        params = c.get("param_dict") or {}
        param_summary = ", ".join(f"{k}={v}" for k, v in list(params.items())[:5])
        lines.append(
            f"  config:{c['id']} name='{c['name']}' "
            f"composite={c.get('composite_mean', 0):.2f}±{c.get('best_score_std', 0):.2f} "
            f"arcface={c.get('arcface_mean', 0):.2f}\n"
            f"    params: {param_summary}"
        )
    return "\n".join(lines) or "  (none)"


def _format_intuitions(intuitions: list) -> str:
    lines = []
    for i in intuitions:
        lines.append(f"  intuition:{i['id']} w={i['weight']:.2f} → {i['content']}")
    return "\n".join(lines) or "  (none)"


def _format_refuted(thoughts: list) -> str:
    lines = []
    for t in thoughts[:8]:
        lines.append(f"  refuted:{t['id']} → {t['content']} | reason: {t.get('refute_reason', '')[:120]}")
    return "\n".join(lines) or "  (none)"


def _format_external_claims(claims: list) -> str:
    lines = []
    for b in claims[:5]:
        lines.append(f"  external:{b['id']} → {b['content'][:160]}")
    return "\n".join(lines) or "  (none)"


def _format_workflows(spec_repo: Path) -> str:
    """List workflows/ + their seed.jsonl entries if present."""
    workflows_dir = spec_repo / "workflows"
    seed_path = spec_repo / "references" / "seed.jsonl"
    seed_by_workflow: dict[str, dict] = {}
    if seed_path.exists():
        for line in seed_path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
                wf = Path(e.get("workflow_template", "")).name
                if wf:
                    seed_by_workflow[wf] = e
            except json.JSONDecodeError:
                continue
    lines = []
    if workflows_dir.exists():
        for wf in sorted(workflows_dir.glob("*.json")):
            entry = seed_by_workflow.get(wf.name) or {}
            note = entry.get("note") or entry.get("stack") or ""
            lines.append(f"  {wf.name}  ({entry.get('name', '?')}) — {note}")
    return "\n".join(lines) or "  (no workflows declared)"


def _format_directions(iters_root: Path, n: int = 3) -> str:
    """Collect recent reflector next_direction strings from disk."""
    if not iters_root.exists():
        return "  (none — no iters yet)"
    items: list[tuple[int, str]] = []
    for d in sorted(iters_root.iterdir()):
        ref = d / "reflection.json"
        if ref.exists():
            try:
                obj = json.loads(ref.read_text())
                items.append((int(d.name.split("_")[-1]), obj.get("next_direction", "")))
            except Exception:
                continue
    items.sort(reverse=True)
    lines = [f"  iter_{n:04d}: {d}" for n, d in items[:n] if d]
    return "\n".join(lines) or "  (no reflector next_directions yet)"


def build_planner_prompt(*, project: str, iter_num: int, spec_repo: Path,
                         iters_root: Path) -> str:
    with WorldModel(project) as wm:
        snapshot = wm.snapshot(top_k_beliefs=12)
        # snapshot only returns top configs via best_configuration; manually
        # fetch the rest for the planner.
        c = wm._get()
        rows = c.execute(
            "SELECT * FROM configurations ORDER BY best_score DESC LIMIT 8"
        ).fetchall()
        configs = []
        for r in rows:
            configs.append({
                "id": r["id"], "name": r["name"],
                "composite_mean": r["composite_mean"] or r["best_score"],
                "best_score_std": r["best_score_std"] or 0.0,
                "arcface_mean": r["arcface_mean"] or 0.0,
                "param_dict": json.loads(r["param_dict"] or "{}"),
            })

    return PLANNER_PROMPT_TEMPLATE.format(
        prev_iter=iter_num - 1,
        iter_num=iter_num,
        beliefs_block=_format_beliefs(snapshot.get("beliefs", [])),
        external_claims_block=_format_external_claims(snapshot.get("external_claims", [])),
        intuitions_block=_format_intuitions(snapshot.get("intuitions", [])),
        configurations_block=_format_configurations(configs),
        refuted_thoughts_block=_format_refuted(snapshot.get("refuted_thoughts", [])),
        workflows_block=_format_workflows(spec_repo),
        recent_directions_block=_format_directions(iters_root, n=3),
    )


def plan(*, project: str, iter_num: int, spec_repo: Path, iters_root: Path,
         log_path: Path | None = None) -> dict | None:
    """Spawn claude -p planner. Returns parsed hypothesis or None."""
    prompt = build_planner_prompt(
        project=project, iter_num=iter_num,
        spec_repo=spec_repo, iters_root=iters_root,
    )
    if log_path is not None:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(prompt + "\n\n──── claude -p OUTPUT ────\n")

    bin_ = claude_binary()
    cmd = [
        bin_, "-p", prompt,
        "--dangerously-skip-permissions",
        "--output-format", "stream-json",
        "--verbose",
        "--max-turns", str(PLANNER_MAX_TURNS),
    ]
    with tempfile.TemporaryDirectory(prefix="aar-planner-") as td:
        proc = subprocess.Popen(
            cmd, cwd=td,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1,
        )
        events: list[dict] = []
        final_message: str | None = None
        deadline = time.monotonic() + PLANNER_DEADLINE
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                if log_path is not None:
                    with open(log_path, "a") as f:
                        f.write(line)
                line = line.strip()
                if not line:
                    continue
                try:
                    evt = json.loads(line)
                except json.JSONDecodeError:
                    continue
                events.append(evt)
                if evt.get("type") == "result":
                    final_message = evt.get("result")
                if time.monotonic() > deadline:
                    print(f"[planner] iter {iter_num}: deadline exceeded")
                    proc.kill()
                    return None
        finally:
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()

    if not final_message:
        for evt in reversed(events):
            if evt.get("type") == "assistant":
                msg = evt.get("message", {})
                for b in msg.get("content", []):
                    if b.get("type") == "text":
                        final_message = b.get("text")
                        break
                if final_message:
                    break

    if not final_message:
        print(f"[planner] iter {iter_num}: no final message")
        return None
    parsed = parse_json_block(final_message)
    if parsed is None:
        print(f"[planner] iter {iter_num}: could not parse JSON from final message")
        return None
    # Record as a thought so future planners see what was proposed.
    try:
        with WorldModel(project) as wm:
            wm.propose_thought(
                content=parsed.get("rationale", "")[:500],
            )
    except Exception:
        pass
    return parsed
