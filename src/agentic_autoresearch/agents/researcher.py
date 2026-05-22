"""Local researcher agent.

Spawned as `claude -p` on the Mac. Activates when the loop is stuck
(plateau detected) or when the planner explicitly flags
"needs_research". Has the WebSearch + WebFetch + Write + Edit tools
so it can:

  1. Search the live web (reddit, arxiv, community blogs) for
     workflows / techniques relevant to the current failure mode.
  2. Read promising pages.
  3. Write a new workflow JSON template into
     <experiment_repo>/workflows/<new_name>.json
  4. Append a new entry to <experiment_repo>/references/seed.jsonl
     describing the stack + linking the source.
  5. Write external_claim beliefs into the world model so the
     planner can cite them.

Subscription-auth'd (claude -p). Zero API spend on the call itself.
WebSearch tokens count against the subscription's chat allowance.

Output contract — a single JSON object fenced ```json ... ```:

{
  "summary": "<one paragraph: what blockers and what was added>",
  "added_workflows": [
    {
      "name":       "qwen-edit-headshot",
      "file":       "workflows/qwen-edit-headshot.json",
      "source_url": "https://...",
      "stack":      "qwen-image-edit + lightning-lora + facedetailer",
      "note":       "From MyAIForce 2026-04 post claiming ~100% FC"
    }
  ],
  "added_claims":   <int>,
  "search_count":   <int>
}

The orchestrator dispatches by:
  - re-ingesting sources.yaml to absorb anything the researcher wrote
  - asking the planner on the NEXT iter (it'll see the new workflows
    + new external_claim beliefs in its snapshot)
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import time
from pathlib import Path

from agentic_autoresearch.agents.spawn import claude_binary, parse_json_block
from agentic_autoresearch.memory.world_model import WorldModel


RESEARCHER_DEADLINE = 15 * 60   # 15 min — broader scope than reflector
RESEARCHER_MAX_TURNS = 60       # let it do many searches if needed
RESEARCHER_MAX_SEARCHES = 20    # soft cap reminded in prompt


RESEARCHER_PROMPT_TEMPLATE = """You are the RESEARCHER for an autoresearch loop on a ComfyUI character-consistency
problem. The framework's loop has plateaued or hit a blocker that the
current workflow templates can't solve. Your job: find new ideas on the
live web, validate them, and add concrete artifacts to the experiment.

═════════════════════════════════════════════════════════
THE PROBLEM
═════════════════════════════════════════════════════════
Goal: photoreal character consistency in ComfyUI.
Target: composite ≥ 8.5/10, arcface_mean ≥ 0.75, std ≤ 0.4.

CURRENT BEST CONFIGURATION:
{best_config_block}

CURRENT WORKFLOWS WE CAN ALREADY TEST:
{workflows_block}

═════════════════════════════════════════════════════════
WHAT'S BLOCKING US — failure-mode beliefs (top of stack)
═════════════════════════════════════════════════════════
{blockers_block}

PLANNER / REFLECTOR RECENT next_direction HINTS:
{recent_directions_block}

═════════════════════════════════════════════════════════
WHAT THE LOOP ALREADY KNOWS (don't re-derive these)
═════════════════════════════════════════════════════════

ACTIVE BELIEFS (high-conf, already learned):
{learned_block}

EXTERNAL CLAIMS already ingested (avoid duplicates):
{existing_externals_block}

═════════════════════════════════════════════════════════
YOUR JOB
═════════════════════════════════════════════════════════

1. Read the blockers. Identify the SPECIFIC technique-shaped gap
   (e.g. "we have Flux+PuLID but no second-pass FaceDetailer node
    wired in; the loop wants one").

2. CONSTRUCT YOUR OWN SEARCH QUERIES from the blockers above. Don't
   use a fixed query list — derive specific, narrow queries from
   what's actually missing. Examples of GOOD queries:

      "ComfyUI FaceDetailer Impact Pack workflow PuLID Flux"
      "Flux face-detailer second-pass 2026 site:civitai.com"
      "Duchenne smile prompt ComfyUI 2026 reddit"

   Examples of BAD queries (too broad):
      "character consistency"
      "ComfyUI workflow"

   Use WebSearch + WebFetch. Cap yourself at ~{max_searches} total
   searches; quality over quantity. The framework prefers these
   domains (per the experiment's source registry):

{preferred_domains_block}

   You don't HAVE to stay on those — discover new sources too — but
   weight them higher when ranking results.

3. For each new workflow you want to add:
   - It must be a TEMPLATE (with placeholders like {{prompt}},
     {{seed}}, {{cfg}}, {{id_weight}}, {{face_image}}, {{steps}}).
     Look at the existing workflows/*.json in the experiment repo to
     match the templating style.
   - Drop the JSON file into:
       {experiment_repo}/workflows/<name>.json
     where <name> uses kebab-case (e.g. "qwen-edit-headshot").
   - The orchestrator will validate JSON parses + node classes are
     known. If you reference a non-standard custom node, list it
     in the seed.jsonl entry's `requires_nodes` array.

4. Append a JSONL line to:
       {experiment_repo}/references/seed.jsonl
   With shape:
     {{"name":"...", "stack":"...", "source":"<url>",
       "claimed_fc":"<value or string>", "params":{{...sensible defaults...}},
       "workflow_template":"workflows/<name>.json",
       "requires_nodes":["NodeClassName1", "NodeClassName2"],
       "note":"<one sentence on what this stack does and why we want it>"}}

5. Output the JSON described below. The orchestrator parses it.

═════════════════════════════════════════════════════════
EXPERIMENT REPO STRUCTURE
═════════════════════════════════════════════════════════
The repo you're modifying is at:
  {experiment_repo}

Workflows go in: {experiment_repo}/workflows/
seed.jsonl is:   {experiment_repo}/references/seed.jsonl

You have Write + Edit tools. The Write tool overwrites files —
NEVER overwrite an existing workflow. Use a new filename.

═════════════════════════════════════════════════════════
OUTPUT — single JSON fenced ```json ... ```
═════════════════════════════════════════════════════════
```json
{{
  "summary": "<one paragraph: the blocker(s) you targeted and what you added>",
  "added_workflows": [
    {{"name":"qwen-edit-headshot",
      "file":"workflows/qwen-edit-headshot.json",
      "source_url":"https://...",
      "stack":"qwen-image-edit + facedetailer + lightning lora",
      "requires_nodes":["FaceDetailer","CLIPLoaderGGUF","ModelSamplingAuraFlow"],
      "note":"From <source>; claims to handle our soft-detail blocker"}}
  ],
  "added_claims": 3,
  "search_count": 8
}}
```
"""


def _format_best_config(project: str) -> str:
    with WorldModel(project) as wm:
        best = wm.best_configuration()
    if best is None:
        return "  (none — no iters scored yet)"
    return (
        f"  name: {best.name}\n"
        f"  composite: {best.best_score:.2f} ± {best.best_score_std or 0:.2f}\n"
        f"  arcface_mean: {best.arcface_mean or 0:.2f}\n"
        f"  workflow_template: {(best.param_dict or {}).get('workflow_template', '?')}"
    )


def _format_workflows(experiment_repo: Path) -> str:
    workflows = sorted((experiment_repo / "workflows").glob("*.json"))
    lines = [f"  {wf.name}" for wf in workflows]
    return "\n".join(lines) or "  (none)"


def _format_blockers(project: str, limit: int = 8) -> str:
    """Surface the top failure-mode beliefs by confidence."""
    with WorldModel(project) as wm:
        beliefs = wm.active_beliefs(limit=20)
    failure_keywords = (
        "failure", "block", "regress", "plateau", "cannot", "can't",
        "lacks", "missing", "fail", "weak", "below", "soft", "mannequin",
        "needs", "requires", "no ",
    )
    blockers = [b for b in beliefs if any(k in b.content.lower() for k in failure_keywords)]
    if not blockers:
        blockers = beliefs
    blockers = blockers[:limit]
    return "\n".join(
        f"  belief:{b.id} conf={b.confidence:.2f} → {b.content[:280]}"
        for b in blockers
    ) or "  (none)"


def _format_learned(project: str, limit: int = 6) -> str:
    with WorldModel(project) as wm:
        beliefs = wm.active_beliefs(limit=limit)
    return "\n".join(
        f"  belief:{b.id} conf={b.confidence:.2f} → {b.content[:200]}"
        for b in beliefs
    ) or "  (none)"


def _format_external_claims(project: str, limit: int = 10) -> str:
    with WorldModel(project) as wm:
        claims = wm.external_claims(limit=limit)
    return "\n".join(
        f"  external:{b.id} {b.source_url or ''}\n    {b.content[:200]}"
        for b in claims
    ) or "  (none)"


def _format_directions(iters_root: Path, n: int = 4) -> str:
    if not iters_root.exists():
        return "  (none)"
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
    return "\n".join(f"  iter_{n:04d}: {d}" for n, d in items[:n] if d) or "  (none)"


def _format_preferred_domains(experiment_repo: Path) -> str:
    sources = experiment_repo / "sources.yaml"
    if not sources.exists():
        return "  (no preferred domains configured)"
    import yaml
    try:
        cfg = yaml.safe_load(sources.read_text()) or {}
    except Exception:
        return "  (sources.yaml malformed)"
    domains = cfg.get("researcher_preferred_domains") or []
    return "\n".join(f"  - {d}" for d in domains) or "  (none configured)"


def research(*, project: str, experiment_repo: Path, iters_root: Path,
             log_path: Path | None = None,
             planner_question: str | None = None) -> dict | None:
    """Spawn claude -p researcher. Returns parsed JSON output or None.

    `planner_question` (optional): a specific research question the
    planner emitted via its needs_research field. When provided, the
    researcher prioritizes answering this over the general blockers list.
    """
    blockers_block = _format_blockers(project)
    if planner_question:
        blockers_block = (
            f"  *** PLANNER EXPLICITLY ASKS: {planner_question} ***\n"
            f"  (Treat this as the top priority. Other blockers below for context.)\n\n"
            + blockers_block
        )
    prompt = RESEARCHER_PROMPT_TEMPLATE.format(
        best_config_block=_format_best_config(project),
        workflows_block=_format_workflows(experiment_repo),
        blockers_block=blockers_block,
        recent_directions_block=_format_directions(iters_root),
        learned_block=_format_learned(project),
        existing_externals_block=_format_external_claims(project),
        experiment_repo=str(experiment_repo),
        max_searches=RESEARCHER_MAX_SEARCHES,
        preferred_domains_block=_format_preferred_domains(experiment_repo),
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
        "--max-turns", str(RESEARCHER_MAX_TURNS),
    ]
    # cwd = experiment_repo so Write/Edit tools resolve relative paths there.
    proc = subprocess.Popen(
        cmd,
        cwd=str(experiment_repo),
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1,
    )
    events: list[dict] = []
    final_message: str | None = None
    deadline = time.monotonic() + RESEARCHER_DEADLINE
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
                print(f"[researcher] deadline {RESEARCHER_DEADLINE}s exceeded")
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
        print(f"[researcher] no final message")
        return None
    parsed = parse_json_block(final_message)
    if parsed is None:
        print(f"[researcher] could not parse JSON from final message")
        return None
    return parsed


def validate_and_register_workflows(*, parsed: dict, experiment_repo: Path,
                                     project: str) -> list[str]:
    """After the researcher returns, validate each added workflow:
      - JSON parses
      - file actually exists where claimed
      - records the stack as an external_claim belief in the world model

    Returns list of names of workflows that were validated + registered.
    """
    added = parsed.get("added_workflows") or []
    registered: list[str] = []
    for wf in added:
        name = wf.get("name") or ""
        file = wf.get("file") or ""
        path = experiment_repo / file
        if not path.exists():
            print(f"[researcher] workflow file missing: {path}")
            continue
        # JSON parse check
        try:
            json.loads(path.read_text())
        except json.JSONDecodeError as e:
            print(f"[researcher] {file} is not valid JSON: {e}; removing")
            path.unlink()
            continue
        # Register as external_claim in world model.
        try:
            with WorldModel(project) as wm:
                wm.add_belief(
                    content=(
                        f"Researcher added workflow '{name}' "
                        f"({wf.get('stack', '?')}). Note: {wf.get('note', '')}"
                    ),
                    confidence=0.3,  # external; not yet tested
                    evidence_iters=[],
                    source_url=wf.get("source_url") or None,
                )
        except Exception as e:
            print(f"[researcher] could not register belief for {name}: {e}")
        registered.append(name)
    return registered
