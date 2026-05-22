"""Local reflector agent.

Spawned as `claude -p` on the Mac, after a pod iter lands. Sees:
  - the iter's scores.json (4 objective metric signals)
  - a presigned S3 URL to winner.webp (and optionally 2 thumb URLs)
  - the seed.jsonl entry describing the workflow stack
  - the param dict (id_weight, cfg, steps, etc.)
  - a world-model snapshot

The reflector's job is to LOOK at the image, reconcile it with the
metrics, and emit structured findings the framework writes into the
world model.

Auth: uses whatever `claude` binary is on PATH. If invoked via the
user's local subscription auth (no ANTHROPIC_API_KEY set), this
billing is sunk into the subscription, not the API. That's the entire
point of doing reflection on the Mac instead of in opus_rubric.py.

Output contract: the reflector emits a single JSON object as its
final message:

{
  "summary": "<1-2 sentence story of what this iter taught us>",
  "winner_assessment": {
    "lighting_coherence":      {"score": 1-10, "note": "..."},
    "scene_plausibility":      {"score": 1-10, "note": "..."},
    "candid_authenticity":     {"score": 1-10, "note": "..."},
    "micro_expression":        {"score": 1-10, "note": "..."}
  },
  "tool_calls": [
    {"name": "confirm_thought", "args": {"thought_id": "..."}},
    {"name": "add_belief", "args": {"content": "...", "confidence": 0.65,
                                    "evidence_iters": ["iter-N"]}},
    {"name": "refute_thought", "args": {"thought_id": "...", "reason": "..."}},
    {"name": "add_understanding", "args": {"content": "...", "supporting_belief_ids": [...]}},
    {"name": "add_intuition", "args": {"content": "...", "weight": 0.6,
                                       "supporting_understanding_ids": [...]}}
  ],
  "next_direction": "<one-phrase suggestion for the planner>"
}

The orchestrator dispatches tool_calls against WorldModel after parsing.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from agentic_autoresearch.agents.spawn import claude_binary, parse_json_block
from agentic_autoresearch.memory.world_model import WorldModel


REFLECTOR_DEADLINE = 8 * 60   # 8 min upper bound (image fetch + thinking)
REFLECTOR_MAX_TURNS = 25


REFLECTOR_PROMPT_TEMPLATE = """You are the REFLECTOR for an autoresearch iteration on a ComfyUI image-generation
problem. The iter has already RUN — you are looking at its result.

═════════════════════════════════════════════════════════
ITER {iter_num} — what just ran
═════════════════════════════════════════════════════════

CATEGORY:     {category}
PARENT CONFIG: {parent_config}

WORKFLOW STACK (from references/seed.jsonl):
{stack_block}

PARAMETERS USED THIS ITER:
{params_block}

ENSEMBLE EVAL RESULT (4 objective metrics):
  composite mean:  {composite_mean}  (target ≥ {target_composite})
  arcface mean:    {arcface_mean}    (gate ≥ 0.55; {arcface_fails}/15 failed)
  dinov2 mean:     {dinov2_mean}
  clip mean:       {clip_mean}
  anatomy:         {anatomy_summary}
  gate failures:   {gate_failures_summary}

WINNER IMAGE (already pulled to local disk):
  {winner_local_path}

ARCHIVAL S3 URL (for later viewing; do NOT need to curl):
  {winner_url}

WORLD-MODEL SNAPSHOT:
{world_model_block}

═════════════════════════════════════════════════════════
YOUR JOB
═════════════════════════════════════════════════════════
1. Use the Read tool on {winner_local_path} to see the winner image.
   (Don't curl anything; the orchestrator already pulled it.)

2. LOOK at the image. Score the four narrowed dimensions metrics can't reach:
   - lighting_coherence       (1-10): are shadows physically consistent?
   - scene_plausibility       (1-10): does the environment feel real?
   - candid_authenticity      (1-10): does it look like a phone photo or AI?
   - micro_expression         (1-10): asymmetric eye crinkles, real muscles?

3. Reconcile what you SAW with the metric numbers. Examples of
   high-signal reflections:
   - "arcface 0.36 but the face DOES resemble identity in bone structure;
      the low score is because PuLID softened features, not because the
      identity is wrong. Try id_weight=1.1."
   - "dinov2 0.71 (high texture similarity) but micro_expression 4: the
      skin matches Brooke's pore texture but the smile is mannequin-stiff.
      Add a prompt term emphasizing eye crinkles."

4. CALL WORLD-MODEL TOOLS via the JSON below. Be SPECIFIC and tie
   beliefs to evidence (the iter id + what you saw).

OUTPUT — single JSON object, fenced in ```json ... ```:

```json
{{
  "summary": "<1-2 sentence story>",
  "winner_assessment": {{
    "lighting_coherence":     {{"score": 7, "note": "..."}},
    "scene_plausibility":     {{"score": 8, "note": "..."}},
    "candid_authenticity":    {{"score": 6, "note": "..."}},
    "micro_expression":       {{"score": 5, "note": "..."}}
  }},
  "tool_calls": [
    {{"name": "add_belief",
      "args": {{"content": "<specific learning, cite iter_{iter_num}>",
                "confidence": 0.6,
                "evidence_iters": ["{iter_id}"]}}}}
  ],
  "next_direction": "<one phrase the planner can act on>"
}}
```
"""


def build_prompt(*, iter_num: int, iter_id: str, category: str,
                 parent_config: str | None,
                 stack_entry: dict,
                 params: dict,
                 scores: dict,
                 target_composite: float,
                 world_model_snapshot: dict,
                 winner_local_path: str) -> str:
    """Assemble the REFLECTOR prompt from per-iter context."""
    ps = scores.get("per_signal", {})

    def _stats_line(key: str, fmt: str = "{:.3f}") -> str:
        s = ps.get(key)
        if not s:
            return "n/a"
        return f"{fmt.format(s['mean'])} (std {fmt.format(s['std'])})"

    composite_mean = _stats_line("composite", "{:.2f}")
    arcface_mean = _stats_line("arcface_cosine")
    dinov2_mean = _stats_line("dinov2")
    clip_mean = _stats_line("clip")
    anatomy_summary = _stats_line("anatomy_sanity")
    gf = scores.get("gate_failures") or {}
    arcface_fails = gf.get("arcface", "?")
    gate_failures_summary = ", ".join(f"{k}: {v}" for k, v in gf.items())

    stack_block = "\n".join(
        f"  {k}: {json.dumps(v) if isinstance(v, (dict, list)) else v}"
        for k, v in (stack_entry or {}).items()
    ) or "  (no seed.jsonl entry)"

    params_block = "\n".join(f"  {k}: {v}" for k, v in (params or {}).items()) or "  (none)"

    s3 = scores.get("s3") or {}
    winner_url = s3.get("winner_url") or "(no S3 url — scp fallback required)"

    thumbs = s3.get("thumbs") or []
    if thumbs:
        thumbs_block = "\n".join(
            f"  {t['name']}: {t['url']}" for t in thumbs[:3]
        )
    else:
        thumbs_block = "  (none)"

    wm_lines = []
    for b in (world_model_snapshot.get("beliefs") or [])[:5]:
        wm_lines.append(f"  belief #{b['id']} conf={b['confidence']:.2f}: {b['content']}")
    for b in (world_model_snapshot.get("external_claims") or [])[:3]:
        wm_lines.append(f"  external #{b['id']}: {b['content']}")
    for i in (world_model_snapshot.get("intuitions") or [])[:3]:
        wm_lines.append(f"  intuition w={i['weight']:.2f}: {i['content']}")
    world_model_block = "\n".join(wm_lines) or "  (world model is empty — this is iter 1)"

    return REFLECTOR_PROMPT_TEMPLATE.format(
        iter_num=iter_num,
        iter_id=iter_id,
        category=category,
        parent_config=parent_config or "(none — fresh forced iter)",
        stack_block=stack_block,
        params_block=params_block,
        composite_mean=composite_mean,
        target_composite=target_composite,
        arcface_mean=arcface_mean,
        arcface_fails=arcface_fails,
        dinov2_mean=dinov2_mean,
        clip_mean=clip_mean,
        anatomy_summary=anatomy_summary,
        gate_failures_summary=gate_failures_summary,
        winner_local_path=winner_local_path,
        winner_url=winner_url,
        world_model_block=world_model_block,
    )


def reflect(*, project: str, iter_num: int, iter_id: str,
            category: str, parent_config: str | None,
            stack_entry: dict, params: dict,
            scores: dict, target_composite: float,
            winner_local_path: str,
            log_path: Path | None = None) -> dict | None:
    """Run the local reflector via claude -p. Returns the parsed reflection
    dict, or None on failure.

    The reflector's tool_calls are NOT dispatched here — caller does that
    via dispatch_tool_calls() so the orchestrator has full control.
    """
    # Snapshot the world model so the reflector knows what we already believe.
    with WorldModel(project) as wm:
        snapshot = wm.snapshot(top_k_beliefs=8)

    prompt = build_prompt(
        iter_num=iter_num,
        iter_id=iter_id,
        category=category,
        parent_config=parent_config,
        stack_entry=stack_entry,
        params=params,
        scores=scores,
        target_composite=target_composite,
        world_model_snapshot=snapshot,
        winner_local_path=winner_local_path,
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
        "--max-turns", str(REFLECTOR_MAX_TURNS),
    ]

    # Use a fresh tmpdir as cwd so the reflector's curl/Read happen in
    # isolation. The Read tool is allowed on absolute paths anyway.
    with tempfile.TemporaryDirectory(prefix="aar-reflector-") as td:
        proc = subprocess.Popen(
            cmd,
            cwd=td,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        events: list[dict] = []
        final_message: str | None = None
        deadline = time.monotonic() + REFLECTOR_DEADLINE
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
                    print(f"[reflector] iter {iter_num}: deadline exceeded, killing")
                    proc.kill()
                    return None
        finally:
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()

        # Clean up any tmp winner image the reflector forgot to delete
        for p in Path("/tmp").glob(f"iter_{iter_num}_winner.*"):
            try:
                p.unlink()
            except Exception:
                pass

    if not final_message:
        # Try to read from the last assistant message in events
        for evt in reversed(events):
            if evt.get("type") == "assistant":
                msg = evt.get("message", {})
                blocks = msg.get("content", [])
                for b in blocks:
                    if b.get("type") == "text":
                        final_message = b.get("text")
                        break
                if final_message:
                    break

    if not final_message:
        print(f"[reflector] iter {iter_num}: no final message")
        return None

    parsed = parse_json_block(final_message)
    if parsed is None:
        print(f"[reflector] iter {iter_num}: could not parse JSON from final message")
        return None
    return parsed


def dispatch_tool_calls(*, project: str, reflection: dict, iter_id: str) -> int:
    """Apply the reflector's tool_calls to the world model.

    Returns the count of successful calls.
    """
    if not reflection:
        return 0
    tool_calls = reflection.get("tool_calls") or []
    n = 0
    with WorldModel(project) as wm:
        for tc in tool_calls:
            name = tc.get("name")
            args = tc.get("args") or {}
            try:
                if name == "add_belief":
                    wm.add_belief(
                        content=args["content"],
                        confidence=float(args.get("confidence", 0.5)),
                        evidence_iters=args.get("evidence_iters", [iter_id]),
                        parent_thought_id=args.get("parent_thought_id"),
                        source_url=args.get("source_url"),
                    )
                elif name == "confirm_thought":
                    wm.confirm_thought(args["thought_id"], iter_id=iter_id)
                elif name == "refute_thought":
                    wm.refute_thought(
                        args["thought_id"], reason=args.get("reason", ""), iter_id=iter_id
                    )
                elif name == "confirm_belief":
                    wm.confirm_belief(args["belief_id"], new_evidence_iter=iter_id)
                elif name in ("contradict_belief", "refute_belief"):
                    # Reflector sometimes uses 'refute_belief'; alias both.
                    wm.contradict_belief(args["belief_id"], reason=args.get("reason", args.get("content", "")))
                elif name == "add_understanding":
                    wm.add_understanding(
                        content=args["content"],
                        supporting_belief_ids=args.get("supporting_belief_ids", []),
                        confidence=float(args.get("confidence", 0.6)),
                    )
                elif name == "add_intuition":
                    wm.add_intuition(
                        content=args["content"],
                        weight=float(args.get("weight", 0.5)),
                        supporting_understanding_ids=args.get("supporting_understanding_ids", []),
                    )
                else:
                    print(f"[reflector dispatch] unknown tool {name!r}; skipped")
                    continue
                n += 1
            except Exception as e:
                print(f"[reflector dispatch] tool {name} failed: {e}")
    return n
