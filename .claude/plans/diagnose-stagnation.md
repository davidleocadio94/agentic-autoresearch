# Debugging the framework

> Date: 2026-05-03. Status: planning, no code yet.
> Focus: **the framework**, not the music problem.

## The phenomenon

After 25 iters, ~12 hours, ~$43 of Claude credit, the loop's **best
score is iter 1's 0.778** — its very first 5-minute attempt. Iter 25
sits at 0.444. The loop spent 24 iters making things worse on average.

```
score
1.0 ┤
0.8 ┤ ●─                      ●──
    │   \                    /  \
0.6 ┤    \                  /    \      ●──
    │     \                /      \    /  \
0.4 ┤  ●───●          ●─●─●        ●──●    ●──●─●─●──●──●─●
    │  ↑                                              ↑
0.2 ┤  iter 1                                    iter 25 (-0.33 vs iter 1)
    └────────────────────────────────────────────────────
       1   3   5   7   9   11  13  15  17  19  21  23  25
```

This is the framework's most important failure to debug. If
agentic-autoresearch lets an agent destroy a winning iter and never
recover, no amount of "spec polish" will save future problems. The
question we need answered:

> **Is memory actually doing anything?** Or is each iter effectively
> blind to what came before, doing a noisy random walk that the
> reflector then over-corrects on?

The music problem is just the test case. We're using it to debug the
framework, then we'll fix the framework.

---

## The two-track diagnosis

### Track A — Audit memory effectiveness on the existing run

We have 25 iters of real data sitting in the DB. Before changing
anything, we mine them to answer: was memory ever consulted, what did
it suggest, did the agent follow the suggestion, did following the
suggestion help?

### Track B — Run a trivial problem and see if it converges

If the framework can't get to 1.0 on a problem so simple it has only
one obvious answer, the framework is broken regardless of the music
problem's difficulty. This is the control experiment.

Both tracks run independently. A is mostly reading; B is mostly waiting
for a small loop to finish.

---

## Track A — Audit memory effectiveness

The framework records, per iter:

- `hypothesis` (PLANNER's rationale)
- `lesson` (REFLECTOR's takeaway)
- `proposed_next` (REFLECTOR's "try this next")
- `hypothesis_embed`, `lesson_embed` (Gemini embeddings, for similarity search)

The PLANNER prompt is built by `intuition.format_for_prompt(run_id)`,
which assembles 7 sections from the DB:

1. recent (last 5 iters)
2. similar (vector search over hypothesis_embed)
3. category stats (avg delta per category)
4. winners (kept iters sorted by delta)
5. open hypotheses (proposed_next not yet tried)
6. error curve (stuck-at-zero fixtures)
7. cross-run (other runs, n/a here — first run)

**The audit asks four concrete questions for each pair (iter N, iter N+1):**

### Q1. Did memory contain useful information?

For each iter N+1, look at the planner prompt that was actually built
(reconstructable by re-running `_build_plan_prompt` with `run_id` and
the score going into iter N+1). Check:
- Did "open hypotheses" contain a non-empty list?
- Did "winners" contain non-trivial entries?
- Did "category stats" show clear winners/losers?
- Were "stuck fixtures" listed?

If the prompt was empty/uninformative, memory was *empty* — not the
agent's fault for ignoring it. **Falsifies "memory is working" if
prompts are routinely empty.**

### Q2. Did the planner's hypothesis cite or reflect memory?

For each iter N+1's `hypothesis` text:
- Does it reference an iter number (e.g. "iter 8 found that ...")?
- Does it reference a stuck fixture by name?
- Does it pick up an open hypothesis from a prior iter?
- Or does it propose something with no reference to anything past?

Tally: **N out of 24 iters cited memory, M did not.** If M is high,
the planner is treating the prompt's memory section as wallpaper.

### Q3. Did following memory help, vs ignoring memory?

For iters that *did* cite memory: what was their score delta? Mean,
distribution. Same for iters that *didn't*. If memory-citing iters
are no better than memory-ignoring ones, the planner is consulting
memory but it's not actually useful information — i.e. the lessons
themselves are bad signals.

### Q4. Is the lesson→hypothesis chain coherent?

Walk iter 8's lesson → iter 9's hypothesis → iter 9's score → iter
9's lesson → iter 10's hypothesis. Trace 3-4 chains end to end. We're
looking for:
- A lesson saying X works → next hypothesis tests X again → contradicting result
- A lesson saying X doesn't work → next hypothesis tries X anyway
- A lesson with no actionable claim
- A hypothesis ignoring the most relevant lesson

This is qualitative but where the smoking gun lives. If the lessons
are bullshit ("we learned more iteration is needed"), planners can't
do anything with them.

### Deliverable

A markdown table:

| iter | cited memory? | which iter(s) cited? | followed advice? | Δ score |
|------|---------------|----------------------|------------------|---------|
| 2    | yes/no        | "iter 1, fixtures"   | yes/no           | -0.111  |
| ...  | ...           | ...                  | ...              | ...     |

Plus a **2-3 paragraph qualitative finding** about whether memory is
the bottleneck.

---

## Track B — Run a trivially simple problem to confirm the loop converges

If the framework's loop machinery is sound, a problem with one obvious
answer should converge in 1-3 iters. If it doesn't, the bug is in the
loop, not the music problem.

We already have **`problems/hello-world/`** bundled — target.txt must
contain `"hello, world!"`. We tested this manually before and it
converged to 1.0 in iter 1. **But that was a one-shot trivial test.**
The autoresearch loop's *memory machinery* isn't really exercised by a
1-iter convergence — there's no "previous iter" for memory to draw on.

So we need a problem that:
1. Has 3-5 obvious "stages" of improvement (so memory has multiple iters
   to draw from)
2. Has a deterministic eval (no LLM noise)
3. Has only one good answer at each stage
4. Should converge in 5-10 iters if the framework works

### Proposal: `problems/text-substitution/`

```
spec.md says: "make output.txt match expected.txt"
expected.txt:  "the quick brown fox jumps over the lazy dog"
output.txt:    starts as "the slow yellow fox sits next to the angry cat"

eval/score.py: word-by-word match → score in [0, 1]
```

Each iter, the agent reads expected, edits output, runs eval.
- Iter 1: probably gets ~3-5 words right (e.g. "the", "fox", "the")
- Iter 2: should remember iter 1's mistakes, fix more words
- ...
- Iter ~5: should be at 1.0

If the framework can't converge on **literally string-matching one
target line**, the loop is broken in a way that has nothing to do
with OMR or Gemini.

This is also the right fixture for testing memory: the agent has to
remember *which words it already fixed* and not break them on the
next iter.

### Even simpler control: `problems/integer-target/`

```
spec.md says: "make output.txt contain the integer 42"
output.txt:    starts as "0"
eval/score.py: 1.0 if int(output.txt) == 42 else max(0, 1 - abs(int(output.txt) - 42) / 100)
```

This converges in 1 iter or never. Useful only as smoke. **Skip if
text-substitution is feasible.**

---

## Open questions to answer before doing the work

1. **For Track A**, can we reconstruct historical planner prompts after
   the fact? Yes — `_build_plan_prompt(spec, run_id, score)` is
   idempotent and reads from DB. But: it's *current* DB state, so a
   prompt for iter 5 reconstructed today would include iters 6-25 in
   memory. We'd need to filter to "iters where iter_num < N" — small
   modification to the audit script.

2. **For Track B**, how should we wire `expected.txt` so the agent
   knows what to do without hand-holding? Options:
   - Eval prints the diff so the agent reads it
   - Spec.md tells the agent the goal in plain English
   - Both — easiest for agents to succeed

3. **Acceptable fail modes for Track B**: If text-substitution converges
   to 1.0 in <10 iters, the framework's loop is fine and the problem
   is the music task's noise + the agent's noise-attribution. If
   text-substitution *also* drifts down from a good iter 1, the
   framework itself is broken.

---

## Decision flow

```
Track A finding              Track B finding              Conclusion
───────────────────          ─────────────────            ──────────────────
memory empty                 converges to 1.0             framework's prompt
or uncited                                                doesn't surface memory
                                                          aggressively enough
                                                          → fix prompts

memory cited but             converges to 1.0             reflectors are
lessons are noise                                         attributing noise to
                                                          signal → fix reflector
                                                          (variance threshold,
                                                          confidence flags)

memory works fine            drifts on text-sub           framework's keep/revert
                             too                          policy is broken or
                                                          "kept iter is the new
                                                          baseline" tracking is
                                                          wrong
                                                          → fix orchestrator

memory works fine            converges                    music problem is just
                                                          hard for OMR + noisy
                                                          → fix omr.py separately,
                                                          framework is OK
```

## Order of operations

1. Track A audit script (1-2h work to write, runs in seconds)
2. Read findings, write up qualitative summary
3. Build `problems/text-substitution/` (15 min)
4. Run it under the framework, watch dashboard
5. Cross-reference Track A and Track B findings against the decision
   flow above
6. Write the *fix plan* in a separate doc once we know what to fix
