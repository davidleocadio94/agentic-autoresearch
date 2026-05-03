# Track A — Memory Effectiveness Audit

**Run:** 3032e9b6… (`sheet-music-omr`)  
**Iters:** 25

## Q1+Q2: per-iter memory + citation table

| iter | mem (prior/kept/open) | cited memory? | iter refs | fixture refs | lang | Δ score |
|------|------------------------|---------------|-----------|--------------|------|---------|
| 1 | 0/0/0 | ✓ | 1 | 01-pitch,C-major | ✗ | +0.778 |
| 2 | 1/1/1 | ✓ | 1 | 02-rhythm | ✗ | -0.111 |
| 3 | 2/1/2 | ✓ | 2 | 02-rhythm | ✓ | -0.222 |
| 4 | 3/1/3 | ✓ | — | 02-rhythm | ✓ | +0.111 |
| 5 | 4/2/4 | ✓ | 4 | 02-rhythm | ✗ | -0.056 |
| 6 | 5/2/5 | ✓ | 4,5 | 02-rhythm | ✓ | -0.125 |
| 7 | 6/2/6 | ✓ | 4 | 02-rhythm | ✓ | +0.069 |
| 8 | 7/3/7 | ✓ | 1,4,5,7 | 02-rhythm | ✗ | +0.333 |
| 9 | 8/4/8 | ✓ | 8 | — | ✗ | -0.222 |
| 10 | 9/4/9 | ✓ | 4,8,9 | 02-rhythm | ✓ | -0.111 |
| 11 | 10/4/10 | ✓ | 8,10 | 02-rhythm | ✗ | +0.000 |
| 12 | 11/4/11 | ✓ | 8,11 | 02-rhythm,quarter-eighth | ✓ | -0.111 |
| 13 | 12/4/12 | ✓ | 5,8 | 02-rhythm | ✓ | +0.222 |
| 14 | 13/5/13 | ✓ | 5,8,13 | 02-rhythm | ✓ | -0.111 |
| 15 | 14/5/14 | ✓ | 4,8,13,14 | 02-rhythm | ✗ | +0.111 |
| 16 | 15/6/15 | ✓ | 15 | 02-rhythm | ✗ | -0.111 |
| 17 | 16/6/16 | ✓ | 15,16 | — | ✓ | +0.000 |
| 18 | 17/6/17 | ✓ | — | 02-rhythm | ✗ | -0.222 |
| 19 | 18/6/18 | ✓ | 9,15 | 02-rhythm | ✗ | +0.111 |
| 20 | 19/7/19 | ✓ | 4,7,19 | 01-pitch,02-rhythm | ✓ | +0.111 |
| 21 | 20/8/20 | ✓ | 20 | 01-pitch,04-key | ✓ | +0.111 |
| 22 | 21/9/21 | ✓ | 20 | 04-key,D-major | ✗ | +0.111 |
| 23 | 22/10/22 | ✓ | 20,21,22 | 01-pitch,04-key | ✓ | -0.111 |
| 24 | 23/10/23 | ✓ | 4,20,21,22,23 | 01-pitch,02-rhythm | ✓ | -0.111 |
| 25 | 24/10/24 | ✗ | — | — | ✗ | +0.000 |

## Q3: did citing memory correlate with better deltas?

- iters citing memory: **23**
- iters NOT citing memory: **1**
- citing mean Δ: **-0.0145** (median -0.0556, stdev 0.1489)
- not-citing mean Δ: **+0.0000** (median +0.0000, stdev 0.0000)

**Δ-of-means: -0.0145 — memory citation has NO meaningful effect on outcomes**


## Q4: lesson→next-hypothesis chains

(Walking each (iter N lesson → iter N+1 hypothesis) pair)

### iter 1 → iter 2 (✓ cited)

**iter 1 lesson:**
> A single fully-worked few-shot example matching the evaluator's exact ground-truth format is dramatically higher-leverage than any amount of prose schema description for this OMR task — score jumped from 0.0000 to 0.7778 with one prompt edit. The prior zero-shot failure was structural (malformed/non-conforming XML), not perceptual: once Gemini saw the precise tag order, indentation, and divisions=…

**iter 2 hypothesis:**
> Score is stuck at 0.7778 with both 02-rhythm fixtures at zero — the iter-1 open hypothesis explicitly flagged image preprocessing or self-consistency for these stuck-at-zero rhythm fixtures, and we haven't tried it yet. Pitch/meter/key fixtures already pass, so the failure is rhythm-specific (likely flag/beam/dot detection on rendered Verovio PDFs at current DPI). I'll bump rasterization DPI (300→…

### iter 2 → iter 3 (✓ cited)

**iter 2 lesson:**
> Score regressed from 0.7778 → 0.6667, so revert. The hypothesis assumed rhythm-fixture failures were caused by flag/beam/dot detection on rendered Verovio PDFs and prescribed a preprocessing fix (DPI bump, tighter unsharp mask, N=3 self-consistency). The actor's own diagnosis revealed the root cause is pitch misreads (skip-pattern vs step-pattern) — a perception/prompting issue, not a rasterizatio…

**iter 3 hypothesis:**
> Score is stuck at 0.6667 with both 02-rhythm fixtures at zero, and iter 2's open hypothesis explicitly calls for dumping predicted vs reference MusicXML on those fixtures before guessing another fix. Iter 2 already tried a blind preproc tweak and lost (-0.111), so we should stop guessing. This iteration adds a debug dump (writes the raw Gemini response + parsed candidate XML to a debug/ dir per fi…

### iter 3 → iter 4 (✗ ignored)

**iter 3 lesson:**
> Diagnostic-only iterations that add no logic change can still cause score regressions because Gemini at temperature=0.2 has run-to-run variance — the 'no behavior change' assumption is false when the eval itself is stochastic. Future iters should (a) commit debug dumps to a separate branch or .gitignored path so they don't pollute the kept tree, (b) capture diagnostic output from the *baseline* ru…

**iter 4 hypothesis:**
> Two 02-rhythm fixtures have been stuck at zero across 4 iterations, and iters 2 and 3 both proposed dumping predicted vs reference MusicXML to identify the exact misread before changing the prompt — but neither actually did so (both were reverted for unrelated reasons). Without knowing whether the failure is a pitch misread, a duration token error, a note-count mismatch, or a structural XML differ…

### iter 4 → iter 5 (✓ cited)

**iter 4 lesson:**
> Diagnostic-only experiments are worth keeping when they (a) don't degrade the score and (b) produce concrete, reusable signal — here the per-measure signature dump revealed that 02-rhythm failures are a systematic pitch-shift-up-a-third misread (C-D-E-F-G-A → C-E-G-B-D-F), with rhythms entirely correct. This is not a duration-token bug, not a malformed-XML bug, and not a structural mismatch — it's…

**iter 5 hypothesis:**
> Iter 4's diagnostic shows rhythms are 100% correct on both stuck 02-rhythm fixtures, but pitches are systematically misread a third too high (C-D-E-F-G-A read as C-E-G-B-D-F). This is a staff-position reading error, not a duration/structural error. Iter 4's open hypothesis explicitly proposed adding a treble-clef pitch reference; the diagnostic now confirms that's the right fix. Add an explicit tr…

### iter 5 → iter 6 (✓ cited)

**iter 5 lesson:**
> Adding an explicit pitch reference chart and worked example to the prompt did not fix the staff-position misreading on 02-rhythm fixtures — it changed the error pattern (from systematic third-skipping C-E-G-B-D-F to a different wrong sequence C-F-G-A-C-D) but didn't yield correct pitches, while also coinciding with a regression elsewhere. This suggests the pitch errors are not from the model lacki…

**iter 6 hypothesis:**
> Iter 4's diagnostic confirmed rhythms are 100% correct on the two stuck 02-rhythm fixtures, but pitches are systematically misread a third too high — a textbook line/space confusion on the treble clef. The open hypothesis from iter 5 (and iter 4) explicitly calls for adding a treble-clef staff-line/space pitch-reading reference to the prompt, which directly targets this specific error pattern. Thi…

### iter 6 → iter 7 (✗ ignored)

**iter 6 lesson:**
> Iter 6 is the second consecutive prompt edit aimed at the 'pitches read a third too high on 02-rhythm' diagnostic, and the second consecutive regression (iter 5: -0.056, iter 6: -0.125 on the recorded run). The diff shows the model is still mis-mapping staff positions (E4→F4, B4→A4, D5→C5/D5) despite the explicit lines/spaces anchor and step-vs-third warning, so simply piling more pitch-reading te…

**iter 7 hypothesis:**
> Iter 4's diagnostic confirmed rhythms are correct on the two stuck 02-rhythm fixtures but pitches read a third too high — a systematic staff-line miscount, not random noise. Iters 5 and 6 tried prompt fixes and lost ground (likely regressing other fixtures), so instead of broad prompt rewrites I'll add a tightly-scoped, pitch-only worked few-shot example with an explicit treble-clef staff-line/spa…

### iter 7 → iter 8 (✓ cited)

**iter 7 lesson:**
> Score went up (0.3750 → 0.4444) so we keep, but the win is fragile and partly cosmetic: the prompt change added a treble-clef line/space lookup table and 'line vs space' guidance, which fixed nothing on the two stuck 02-rhythm fixtures (still 0.0) and instead the model now over-corrects — inserting spurious rests, splitting measure 1 into two measures, and reading arpeggios as stepwise motion. The…

**iter 8 hypothesis:**
> Three consecutive prompt-only attempts at the systematic 'third-too-high' pitch misread on the two stuck 02-rhythm fixtures have netted only +0.069 total (iter 7), and the prompt category is showing diminishing returns. Iter 4's diagnostic confirmed the bug is positional staff-line reading, which is fundamentally a vision/resolution problem — the model can't reliably count staff lines when the ren…

### iter 8 → iter 9 (✓ cited)

**iter 8 lesson:**
> Bumping render DPI from 300 to 600 produced the headline jump from 0.4444 to 0.7778, but the actor explicitly flagged high run-to-run variance (0.44–0.78) at the same DPI/temp due to Gemini sampling at temperature 0.2 — meaning a single eval run is not a reliable signal of true delta. Future iterations should (a) reduce sampling noise before drawing conclusions (set temperature to 0, or average N …

**iter 9 hypothesis:**
> Iter 8's preproc win (DPI 300→600 + retry) brought us to 0.7778, but we don't know how much was real signal vs sampling variance — Gemini at default temperature is non-deterministic. Iter 8's own open hypothesis flagged this exact concern. Before stacking another lever (more prompt edits, more preprocessing) on a noisy baseline, lock temperature to 0 in the Gemini client config so subsequent delta…

### iter 9 → iter 10 (✓ cited)

**iter 9 lesson:**
> Iter 9 confirms what iter 8's open hypothesis suspected: the 0.7778 score from the DPI-bump experiment was substantially noise-inflated by Gemini's non-zero default temperature (0.2). Locking temperature to 0.0 dropped the score to a stable 0.5556 — this is the *real* deterministic floor, not a regression. KEEP this change despite the apparent score drop, because every future experiment now produc…

**iter 10 hypothesis:**
> Score regressed from 0.7778 (iter 8) to 0.5556 after iter 9's revert, and the two 02-rhythm fixtures remain stuck at 0/2 despite four prompt attempts and a DPI bump. Before stacking another lever, lock temperature=0 in gemini_client (kills sampling variance flagged in iter 8/9) and re-run the iter 4 diagnostic dump under the current prompt+DPI=600 config to confirm the 'third-too-high' pattern is …

### iter 10 → iter 11 (✓ cited)

**iter 10 lesson:**
> Locking temperature=0 dropped the score (0.5556→0.4444) but the regression is informative, not a failure: it confirmed earlier scores above 0.4444 contained sampling luck on borderline pitch reads, and the deterministic diagnostic dump nailed the true failure mode on 02-rhythm — rhythms are perfect (3/2,1/2,3/2,1/2 dotted-quarter matches GT exactly) while every pitch is shifted up exactly a third …

**iter 11 hypothesis:**
> Iter 8's preproc win (DPI 600 + retry) is locked at 0.4444-0.7778 with the two 02-rhythm fixtures still deterministically misread a third too high — a systematic staff-line offset that four prompt attempts have failed to fix. The most promising untried lever is iter 10's open hypothesis: a labeled treble-staff few-shot showing each line/space pitch (E-G-B-D-F / F-A-C-E) annotated on an actual rend…

### iter 11 → iter 12 (✓ cited)

**iter 11 lesson:**
> The visual treble-staff reference + temperature=0 successfully fixed the long-standing 'third-too-high' pitch misread on both stuck 02-rhythm fixtures (02-dotted-quarter flipped 0→1.0, and 01-quarter-eighth's pitches are now C-D-E-F-G-A, exactly matching GT). This validates that a labeled visual anchor beats text-only positional rules — four prior prompt-only attempts (iters 5, 6, 7, 10) failed at…

**iter 12 hypothesis:**
> The 'third-too-high' misread on 02-rhythm/01-quarter-eighth has survived 5 prompt attempts including a treble-clef lookup table (iter 11), but no attempt has yet combined a *visual* labeled-staff reference image embedded in the prompt with temperature=0 — the open hypothesis from iters 5/10. Text-only descriptions of staff lines clearly aren't grounding Gemini's vision; a side-by-side image (trebl…

### iter 12 → iter 13 (✗ ignored)

**iter 12 lesson:**
> Iter 12 finally cracked the long-standing 'third-too-high' pitch misread on 02-rhythm/01-quarter-eighth — the labeled treble-staff reference image plus temp=0 produced the correct C-D-E-F-G sequence, validating the iter-5/10/11 hypothesis that visual grounding (not text descriptions of staff lines) is what Gemini needs for pitch reading. However, the change regressed overall score 0.4444→0.3333 be…

**iter 13 hypothesis:**
> Six prompt-only attempts at the systematic third-too-high misread on 02-rhythm fixtures have netted only +0.069 total, and the prompt category is exhausted (avg Δ=+0.093 with diminishing returns). The single highest-leverage open hypothesis from iter 5/7 — crop/tile the rendered page into single-staff sub-images before sending to Gemini — has never been tried, and it directly addresses the staff-l…

### iter 13 → iter 14 (✓ cited)

**iter 13 lesson:**
> Per-staff cropping via row-projection segmentation lifted score from 0.333 to 0.556 (+0.222), the second-largest preproc win after iter 8's DPI bump. The mechanism: giving Gemini a single-staff sub-image instead of a full page massively increases pixels-per-staff-line and removes the need to disambiguate vertical position across the page, which directly fixed the long-standing 02-rhythm/01-quarter…

**iter 14 hypothesis:**
> The preproc category has the best track record (avg Δ=+0.148, 2/3 wins) — both kept wins (iter 8 DPI bump, iter 13 per-staff cropping) attacked the third-too-high misread successfully, while six prompt attempts on the same bug netted only +0.069 and the last five all reverted. The score is currently stuck at 0.5556 with two 02-rhythm fixtures still failing, so the natural next preproc lever is ove…

### iter 14 → iter 15 (✓ cited)

**iter 14 lesson:**
> Overlaying pitch-label annotations (E-G-B-D-F line letters, F-A-C-E space letters, ledger extensions) directly onto the cropped per-staff strips regressed the score from 0.5556 to 0.4444 — the third-too-high misread persisted on 02-rhythm AND the previously-passing 04-key fixture lost both measures. This is the second failed visual-anchor attempt (iter 13's numeric overlay variant was the first KE…

**iter 15 hypothesis:**
> The 'third-too-high' misread on 02-rhythm fixtures has survived 6 prompt attempts (avg Δ=+0.093 with diminishing returns) and 4 preproc attempts (DPI bump + per-staff cropping already won, but iter 14 regressed). Iter 14's open hypothesis proposes a post-processing repair pass that detects the systematic third-too-high pattern and shifts pitches down a third when triggered — this is a fresh catego…

### iter 15 → iter 16 (✓ cited)

**iter 15 lesson:**
> Score went up (+0.1112, 0.4444→0.5556) so KEEP per default policy, but the win is fragile and partially illusory: the actor's own report admits the postproc never triggered on the regressed fixtures (02-rhythm/01, 03-meter, 04-key dropped) — those losses came from Gemini call variability in the primary transcription, while the gain came from 02-rhythm/02 finally passing. This means the determinist…

**iter 16 hypothesis:**
> Five open hypotheses across iters 1, 6, 8, 9, 11, 15 all flag the same root issue: sampling variance is masking real signal. We've had 16 iters of single-sample noise where +0.111 deltas could be luck and reverts could be unlucky draws. Pinning temperature=0 (and adding a 504/5xx retry if not already wired) costs ~5 min and makes every future delta trustworthy — a force-multiplier before we attack…

### iter 16 → iter 17 (✓ cited)

**iter 16 lesson:**
> Pinning temperature=0 looked like a free force-multiplier but it backfired: score dropped from 0.5556 to 0.4444, and the diff shows the model now emits collapsed/minified XML with a phantom measure-2 split on 02-rhythm/01-quarter-eighth — exactly the fixture that iter 15's postproc third-repair had just rescued. The lesson is twofold. First, temperature=0 is NOT a no-op for Gemini on this task: it…

**iter 17 hypothesis:**
> Six open hypotheses across iters 1, 6, 8, 9, 10, 11, 15, 16 all converge on the same diagnosis: sampling variance is masking real signal, making per-iter deltas untrustworthy (iter 16 even tried temp=0 alone and reverted at -0.111, suggesting noise dominates). Before stacking another prompt/preproc lever on a noisy baseline, lock determinism via temperature=0 AND 3-sample self-consistency majority…

### iter 17 → iter 18 (✗ ignored)

**iter 17 lesson:**
> Self-consistency majority-vote at N=3 with temperature=0 produced exactly the same aggregate score (0.4444) as the noisy baseline, at 3x the API cost (18 calls vs 6) and ~3x runtime — a clear net loss when the goal was a trustworthy baseline. Combined with iter 16's temp=0-alone result (Δ=-0.111), this is now strong evidence that the 6-fixture eval is too small for variance-reduction techniques to…

**iter 18 hypothesis:**
> Six open hypotheses (iters 1, 6, 8, 9, 10, 11, 15, 16) all converge on the same diagnosis: sampling variance at temperature>0 is masking real signal, making single-fixture deltas untrustworthy and causing iters 16-17 to revert legitimate changes. Before any more prompt/preproc tuning, lock determinism by setting Gemini temperature=0 in the client (and add a 3-sample majority-vote fallback only if …

### iter 18 → iter 19 (✗ ignored)

**iter 18 lesson:**
> Pinning Gemini temperature to 0.0 dropped the score from 0.4444 to 0.2222 (Δ=-0.222), the worst single-iter regression in the variance-mitigation arc (iters 9, 16, 17, 18). This is now strong evidence that the 'sampling variance is masking signal' framing — which six prior hypotheses converged on — is itself wrong, or at least incomplete: temperature>0 may actually be HELPING the model recover fro…

**iter 19 hypothesis:**
> Score has been stuck at 0.2222 with 4 consecutive rerun-category failures (avg Δ=-0.139), and 6+ open hypotheses across iters 8/9/10/11/15/16/17/18 all converge on the same root cause: temperature>0 sampling variance is masking real signal, making every single-fixture delta untrustworthy. Before trying ANY more prompt/preproc levers, we must fix the measurement instrument — pin Gemini temperature=…

### iter 19 → iter 20 (✓ cited)

**iter 19 lesson:**
> Pinning Gemini temperature to 0.0 was the highest-leverage change in 18 iterations: score jumped 0.2222→0.3333 and 02-rhythm/01-quarter-eighth flipped to 1.000 with a clean single-measure decode. The lesson is that fixing the measurement instrument BEFORE chasing prompt/preproc levers was correct — every prior single-fixture delta at temperature=0.2 was contaminated by sampling variance, which exp…

**iter 20 hypothesis:**
> Iter 19 just locked temperature=0 making deltas deterministic for the first time, but we still don't know what's actually failing on the 4 stuck fixtures (01-pitch, 04-key, plus the 02-rhythm pair that the third-repair postproc didn't fully rescue). Open hypothesis from iter 19 is the highest-value next move: dump predicted-vs-reference MusicXML for each failing fixture under the now-deterministic…

### iter 20 → iter 21 (✓ cited)

**iter 20 lesson:**
> Diagnostic iters that produce structured per-fixture artifacts (predicted vs ground-truth XML + measure-signature diffs) are worth keeping even when score is unchanged, because they convert vague 'stuck fixture' hypotheses into concrete, falsifiable failure modes. This run revealed two distinct, targeted bugs masked under one 'pitch error' bucket: (a) 01-pitch/02-octave-leap reads C5 as D5 — a sin…

**iter 21 hypothesis:**
> The diagnostic dump from iter 20 (deterministic at temp=0) is now committed and the top open hypothesis from iter 20 points to a specific deterministic failure: the row-projection per-staff crop from f0f74d3 likely shifts the reference staff-line position for 04-key (D-major), causing a key-signature/pitch miscalibration on the largest single still-failing fixture. Preproc has a healthy track reco…

### iter 21 → iter 22 (✗ ignored)

**iter 21 lesson:**
> Iter 21 confirms that tight per-staff crops without surrounding-page context destabilize the model's vertical pitch calibration — doubling BAND_VERTICAL_PAD_PX (200→400) plus a downscaled full-page fallback unlocked 04-key M1 (D-major D4/F#4/A4/D5) and 03-meter M2 for +0.111. The mechanism is that the model uses page whitespace above/below the staff as an absolute reference for staff-line position…

**iter 22 hypothesis:**
> Score is stuck at 0.5556 with the most-cited open hypothesis (iter 20) pointing at 04-key: the row-projection crop from f0f74d3 likely shifts the staff reference for D-major, breaking key-signature pitch reads. Preproc is our highest-yielding category that's still winning (3/5 wins, +0.089 avg), and the diagnostic dumps from iters 20-21 already give us a deterministic failure signature to target. …

### iter 22 → iter 23 (✓ cited)

**iter 22 lesson:**
> Iter 22 confirms the iter-20 diagnostic hypothesis: row-projection cropping was starving Gemini of vertical context needed to anchor staff-line positions, and bumping BAND_VERTICAL_PAD_PX from 400→600 (~3 staff-heights of whitespace above/below the detected band) unlocked +0.111 (5/9→6/9), with first-ever wins on 04-key and 03-meter and zero regressions on previously-passing fixtures. The crucial …

**iter 23 hypothesis:**
> Score is stuck at 0.6667 with two open hypotheses pointing at the same root cause: the row-projection/staff-anchoring crop in the preprocessor is causing pitch-band misalignment (04-key D-major from iter 20, and 01-pitch/02-octave-leap's new D5→F5 third-too-low misread from iter 21). Recent BAND_VERTICAL_PAD_PX bumps (400→600) helped but iter 22's open hypothesis explicitly proposes sweeping 700/8…

### iter 23 → iter 24 (✓ cited)

**iter 23 lesson:**
> Score dropped 0.667→0.556, so revert per default policy. Two distinct lessons here: (1) BAND_VERTICAL_PAD_PX has a sweet spot around 600 — the 700 sweep over-padded crops and shifted the staff anchor, regressing 01-pitch/02, 03-meter, and 04-key/m2; further pad increases beyond 600 should be considered exhausted unless paired with a compensating anchor recalibration. (2) Gemini at temperature=0 st…

**iter 24 hypothesis:**
> Score regressed in iter 23 and is stuck at 0.5556 with multiple open hypotheses (iters 20, 21, 22) all blaming the row-projection/staff-anchoring crop, but we're guessing at pad values without seeing what the cropped images actually look like for each failing fixture. Before another blind BAND_VERTICAL_PAD_PX sweep (iter 22's open hypothesis) or symmetric postproc work (iter 21), dump the actual p…

### iter 24 → iter 25 (✗ ignored)

**iter 24 lesson:**
> Diagnostic-only iterations that don't change pipeline inputs (this one only added file dumps + a one-line call) should be score-neutral by construction, since temp=0 was pinned in iter 19. The 0.5556→0.4444 swing here proves Gemini is NOT byte-deterministic at temp=0 the way iter 19 claimed — the manifest confirms image bytes and prompt are unchanged, yet 04-key flipped from 'third too high' to 's…

**iter 25 hypothesis:**
> Score regressed from 0.6667 to 0.4444 across iters 23-25 with three consecutive preproc tweaks to BAND_VERTICAL_PAD_PX (400→600→...) all reverting. The preproc category has avg_delta=+0.063 with diminishing/negative returns and we're flying blind — multiple open hypotheses (iters 23, 24) explicitly call for measuring the temp=0 noise floor and adding multi-run averaging before any more single-fixt…


## Q5: open hypotheses left dangling

- iter 25 proposed: Add multi-run averaging (N=3, report mean) to the eval harness itself so future iters compare means not single runs, then target the two deterministically-failing fixtures (02-rhythm/01-quarter-eighth…

**1 of 25 proposed_next entries never picked up by a later iter.**

