"""Merge primitive answers into a candidate `<measure>` block.

Iter-17 design: Stage-A produces a candidate measure (via voting),
Stage-B verifies it against the holistic crop, and Stage-C primitives
(pitch + rhythm) read individual noteheads and beam groups in tight
zooms. The assembler takes the Stage-B output PLUS the primitive
answers and produces a final `<measure>` block.

Iter-36 structural changes (the planner's hypothesis): two stuck
fixtures (both rhythm fixtures + d-major-m2) all share root causes
DOWNSTREAM of perception. The perception is consistent and confident
across iters 31-35; the bottleneck is the assembler.

  * The LEFT-trim heuristic (drop `excess` zooms from the left) was
    off-by-one on the rhythm fixtures: iter-35 trace shows 9 detected
    noteheads but n_pitched=6, excess=3, and the left-trim consumed
    the first real notehead (C4 quarter) along with the clef/time
    phantoms. Replace with x-coord-based phantom dropping:
    `align_zooms_by_xcoords` drops detections whose x-position lies
    in the clef/key/time prefix region, leaving real noteheads alone.
    Falls back to LEFT-trim when x-coords aren't usable.

  * Iter-38: when surplus detections SURVIVE the prefix drop (e.g.
    a phantom in the interior of the measure), the prior fallback
    LEFT-trim still consumed the leading real notehead. Iter-37's
    instrumentation confirmed this on the rhythm fixtures. Replace
    the surplus-drop with a positional-distance trim against an
    evenly-spaced x-grid of `target_count` slots; iteratively drop
    the candidate whose nearest-slot residual is largest.

  * Iter-39: the iter-38 grid spanned [music_start, measure_x1] —
    those endpoints include whitespace BEFORE the first notehead
    and AFTER the last, so the slot positions were biased away
    from the actual music. On the quarter-eighth fixture this
    dropped a real interior notehead and kept a phantom. Anchor
    the grid on [first_candidate_x, last_candidate_x] of the
    POST-PREFIX survivors instead — the bracketing detections
    are essentially always real (they bound the music), so the
    slot endpoints are correct by construction; phantoms inside
    the span sit in the gaps between expected slots.

  * The adjacency guard rejected confident primitive overrides that
    spanned more than 1 letter (d-major-m2: F5→C5 is 3 letters apart).
    Gate the guard on primitive confidence: a confident primitive
    answer overrides the candidate even on big jumps. The narrow
    per-notehead pitch zoom is more reliable than holistic Stage-A.
    The guard remains active for non-confident primitives.

Strict guards retained (learned the hard way in iter-12 and iter-15):

  * Pitch primitives apply ONLY when the post-trim notehead count
    EXACTLY equals the candidate's `<note>` count. If they differ, we
    don't know how to align primitives → notes; skip the entire pitch
    pass for this measure.
  * Pitch primitives apply ONLY when the candidate has `<pitch>`
    (skip rests). The mapping is positional: i-th detected notehead ↔
    i-th non-rest `<note>`.
  * Rhythm primitives apply only when count matches AND the answer
    is confident. We update `<duration>` and `<type>` (and `<dot/>`)
    in place using divisions=4. We DO NOT change pitches in this pass.
  * If applying a rhythm correction would make the measure's total
    duration overflow the time signature, that single answer is
    skipped (iter-34 per-note revert).

Mechanical alters are still applied via `apply_key_signature_alters`
once we know the key with confidence.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET

from . import debug
from .primitives import PitchAnswer, RhythmAnswer

DIVISIONS = 4

DURATION_VALUES = {
    "whole": 16,
    "half": 8,
    "quarter": 4,
    "eighth": 2,
    "16th": 1,
}


# Iter-36: clef + key + time prefix region width, in line-spacings.
# Treble clef glyph: ~4 line-spacings. Key signature: ~0.6 line-spacings
# per accidental (Verovio default). Time signature: ~1.5 line-spacings.
# Conservative margin of 0.5 keeps the boundary BEHIND the first real
# notehead even when the engraver tightens the layout. Real noteheads
# in measure 1 sit at x ≥ measure_x0 + this offset.
_CLEF_WIDTH_SPACINGS = 4.0
_KEY_PER_ACCIDENTAL_SPACINGS = 0.6
_TIME_SIG_WIDTH_SPACINGS = 1.5
_MUSIC_START_MARGIN_SPACINGS = 0.5


def music_start_x(
    *,
    measure_x0: int,
    line_spacing_hi: float,
    is_first_on_page: bool,
    key_fifths: int,
) -> int:
    """Estimate the left edge of the actual music in pixel coords.

    For the first measure on a page the staff carries clef + key
    signature + time signature glyphs which the notehead detector
    can flag as phantom noteheads. Real noteheads sit to the right
    of all three. For later measures there are no such glyphs, so
    `measure_x0` (the left barline) IS the music start.
    """
    if line_spacing_hi <= 0:
        return measure_x0
    if not is_first_on_page:
        return measure_x0
    prefix_spacings = (
        _CLEF_WIDTH_SPACINGS
        + abs(key_fifths) * _KEY_PER_ACCIDENTAL_SPACINGS
        + _TIME_SIG_WIDTH_SPACINGS
        - _MUSIC_START_MARGIN_SPACINGS
    )
    return measure_x0 + int(round(prefix_spacings * line_spacing_hi))


def select_real_notehead_indices(
    *,
    notehead_xs: tuple[int, ...],
    music_start: int,
    target_count: int,
    measure_x1: int = 0,
    allowed_indices: frozenset[int] | None = None,
    candidate_durations: tuple[int, ...] | None = None,
) -> tuple[int, ...] | None:
    """Pick which detected noteheads are real (drop clef/key/time phantoms).

    Returns the original indices into `notehead_xs` of the kept
    noteheads, in left-to-right order. Returns None if no clean
    alignment is possible (count still wrong after drops).

    Iter-45: optional `allowed_indices` is the set of detector indices
    the upstream VLM verify primitive flagged as is_real=True. When
    provided, candidates are restricted to this set BEFORE the prefix
    drop and grid trim — so verify=NO detections are dropped
    unconditionally even if downstream geometry alone would have kept
    them. This is the planner's iter-45 fix: iter-44's instrumentation
    showed verify correctly tags phantoms but the prior code path
    discarded the verify result whenever real_count != n_pitched and
    fell back to geometry-only trim, which re-admitted a phantom.

    Iter-46: optional `candidate_durations` are the Stage-A durations
    for each pitched note (length == target_count). When provided AND
    durations are non-uniform, slot positions are weighted by
    cumulative duration instead of evenly spaced. Music engravers space
    noteheads in proportion to their durations (a dotted-quarter takes
    ~3x the horizontal space of an eighth), so a uniform grid biases
    away from the real note xs on dotted/mixed rhythms. Iter-44 trace
    showed dotted-quarter+eighth fixture (real xs spaced 6:2:6:2)
    losing a real interior notehead to the uniform grid trim. Falls
    back to uniform spacing when durations are unavailable, all-equal,
    or sum to zero.

    Algorithm:
      0. (Iter-45) If `allowed_indices` is given, restrict the
         starting candidate set to those indices.
      1. Drop any detection whose x is left of `music_start` (the
         clef/key/time prefix).
      2. If the remaining count equals `target_count`, return them.
      3. If MORE remain than expected, perform a positional-distance
         trim. Iter-39 narrows the grid span: instead of anchoring
         to [music_start, measure_x1] (which spans whitespace before
         and after the music and pulls slots toward edges that no
         real notehead reaches), use [first_candidate_x,
         last_candidate_x] of the POST-PREFIX survivors. Real
         noteheads occupy that span; phantoms within it sit AWAY
         from any expected slot. The first and last survivors are
         very likely real (they bracket the music) so anchoring on
         them is safe. Iteratively drop the candidate whose
         nearest-slot residual is largest. The signature still
         accepts `measure_x1` for backward compatibility but it is
         no longer used for the grid endpoints.
      4. If FEWER remain (we cut into real noteheads), bail out —
         skipping Stage-C is safer than misalignment.
    """
    del measure_x1  # iter-39: no longer used; grid is anchored on candidates
    if target_count <= 0:
        return None
    candidates = [
        i for i, x in enumerate(notehead_xs)
        if x >= music_start
        and (allowed_indices is None or i in allowed_indices)
    ]
    if len(candidates) == target_count:
        return tuple(candidates)
    if len(candidates) > target_count:
        if target_count >= 2:
            grid_x0 = notehead_xs[candidates[0]]
            grid_x1 = notehead_xs[candidates[-1]]
            if grid_x1 > grid_x0:
                return _grid_distance_trim(
                    candidates=candidates,
                    notehead_xs=notehead_xs,
                    grid_x0=grid_x0,
                    grid_x1=grid_x1,
                    target_count=target_count,
                    candidate_durations=candidate_durations,
                )
        surplus = len(candidates) - target_count
        return tuple(candidates[surplus:])
    return None


def _duration_weighted_slots(
    *,
    grid_x0: int,
    grid_x1: int,
    target_count: int,
    candidate_durations: tuple[int, ...] | None,
) -> list[float]:
    """Slot centers in [grid_x0, grid_x1], weighted by note durations.

    With N=target_count notes whose durations are d_0..d_{N-1}, the
    i-th notehead's onset sits at x ≈ grid_x0 + (sum_{j<i} d_j) / sum *
    span — the cumulative duration BEFORE that note as a fraction of
    the full measure. grid_x1 corresponds to the END of the measure
    (the next barline / right edge), so the last real notehead's slot
    is at sum_excluding_last / sum, NOT at grid_x1 itself.

    This is critical for phantom-trim: when a barline (or right-edge
    artifact) is detected as a phantom notehead, it sits at grid_x1
    while the last real notehead sits inboard. A uniform grid (or one
    that pinned the last slot to grid_x1) wrongly identifies the real
    notehead as off-grid.

    Falls back to uniform spacing if durations are missing, have wrong
    length, are non-positive, sum to zero, or are all equal (in which
    case weighting reduces to uniform anyway).
    """
    span = grid_x1 - grid_x0
    n = target_count

    def uniform() -> list[float]:
        step = span / (n - 1)
        return [grid_x0 + i * step for i in range(n)]

    if candidate_durations is None or len(candidate_durations) != n:
        return uniform()
    if any(d <= 0 for d in candidate_durations):
        return uniform()
    if all(d == candidate_durations[0] for d in candidate_durations):
        return uniform()
    total = sum(candidate_durations)
    if total <= 0:
        return uniform()
    slots: list[float] = [float(grid_x0)]
    cumulative = 0
    for d in candidate_durations[:-1]:
        cumulative += d
        slots.append(grid_x0 + (cumulative / total) * span)
    return slots


def _grid_distance_trim(
    *,
    candidates: list[int],
    notehead_xs: tuple[int, ...],
    grid_x0: int,
    grid_x1: int,
    target_count: int,
    candidate_durations: tuple[int, ...] | None = None,
) -> tuple[int, ...]:
    """Drop surplus candidates by largest residual to a duration-weighted grid.

    Build `target_count` slot centers across [grid_x0, grid_x1]
    (target_count >= 2 enforced by caller). Slots are duration-
    weighted when `candidate_durations` is provided; otherwise evenly
    spaced. While there are too many candidates, drop the one whose
    distance to its nearest slot is the largest. The survivors are
    returned in left-to-right (original) order.

    Iter-39: the grid endpoints are the first and last POST-PREFIX
    candidate x-positions, NOT the measure barlines. This pins the
    grid to the actual music span — phantoms sit in the gaps between
    real notes, while the bracketing candidates (which are almost
    always real noteheads) anchor the slots.

    Iter-46: slot centers are weighted by cumulative durations rather
    than uniformly spaced. On dotted-quarter+eighth (6:2:6:2) the
    uniform grid placed slots at relative [0, 1/3, 2/3, 1] but real
    onsets are at [0, 6/16, 8/16, 14/16] — a uniform grid pulled the
    interior real notehead out of any slot's neighborhood and dropped
    it as a phantom. Duration-weighted slots align with engraver-
    expected onsets.
    """
    slots = _duration_weighted_slots(
        grid_x0=grid_x0,
        grid_x1=grid_x1,
        target_count=target_count,
        candidate_durations=candidate_durations,
    )

    def residual(idx: int) -> float:
        x = notehead_xs[idx]
        return min(abs(x - s) for s in slots)

    survivors = list(candidates)
    while len(survivors) > target_count:
        worst = max(range(len(survivors)), key=lambda k: residual(survivors[k]))
        survivors.pop(worst)
    return tuple(survivors)


def extract_candidate_pitched_durations(candidate_block: str) -> tuple[int, ...]:
    """Parse `<duration>` from each pitched note in a candidate block.

    Returns one int per `<note>` with a `<pitch>` child, in document
    order. Notes missing/malformed `<duration>` get 0. Used by the
    iter-46 duration-weighted grid trim to slot phantom drops against
    engraver-spaced onsets.
    """
    try:
        root = ET.fromstring(candidate_block)
    except ET.ParseError:
        return ()
    durations: list[int] = []
    for note_el in root.iter("note"):
        if note_el.find("pitch") is None:
            continue
        if note_el.find("chord") is not None:
            continue
        d_str = (note_el.findtext("duration") or "0").strip() or "0"
        try:
            durations.append(int(d_str))
        except ValueError:
            durations.append(0)
    return tuple(durations)


def apply_primitives(
    candidate_block: str,
    *,
    pitch_answers: list[PitchAnswer],
    rhythm_answers: list[RhythmAnswer],
    notehead_count: int,
    expected_total_duration: int | None,
    measure_number: int | None = None,
    page_index: int | None = None,
) -> str:
    """Apply primitive corrections to a `<measure>` block.

    Returns the candidate block (possibly corrected). Falls back to
    the original on any structural mismatch — never raises.

    `expected_total_duration` is the expected sum of `<duration>` in
    the measure (e.g. 16 for 4/4, 12 for 3/4). If None we skip the
    rhythm-sum sanity check.

    Iter-37: optional `measure_number` / `page_index` enable per-note
    rhythm-decision instrumentation via `debug.record_rhythm_decision`.
    No behavior change when omitted.
    """
    try:
        root = ET.fromstring(candidate_block)
    except ET.ParseError:
        return candidate_block

    pitched_notes = [n for n in root.iter("note") if n.find("pitch") is not None]
    has_chord = any(n.find("chord") is not None for n in pitched_notes)

    pitch_applied_root = root
    if (
        not has_chord
        and notehead_count == len(pitched_notes)
        and pitch_answers
    ):
        pitch_applied_root = _apply_pitch(root, pitched_notes, pitch_answers)

    pitched_after = [
        n for n in pitch_applied_root.iter("note") if n.find("pitch") is not None
    ]
    has_chord_after = any(
        n.find("chord") is not None for n in pitched_after
    )

    rhythm_applied_root = pitch_applied_root
    if (
        not has_chord_after
        and notehead_count == len(pitched_after)
        and rhythm_answers
    ):
        rhythm_applied_root = _apply_rhythm(
            pitch_applied_root,
            pitched_after,
            rhythm_answers,
            expected_total_duration,
            measure_number=measure_number,
            page_index=page_index,
        )

    return _serialize_measure(rhythm_applied_root, candidate_block)


def _apply_pitch(
    root: ET.Element,
    pitched_notes: list[ET.Element],
    answers: list[PitchAnswer],
) -> ET.Element:
    """Overwrite `<step>` and `<octave>` for each pitched note.

    Mapping is positional: i-th pitched note ↔ i-th notehead. Apply
    only `usable` (confident + valid step + valid octave) answers;
    non-usable answers are skipped entirely.

    Iter-36: the previous strict adjacency guard rejected confident
    primitive overrides whose step disagreed by more than one letter
    (the d-major-m2 case: candidate F5, primitive C5). Per planner
    hypothesis, the narrow per-notehead pitch zoom is more reliable
    than holistic Stage-A — when the primitive is confident, accept
    the answer regardless of step distance. The `usable` gate already
    requires confidence + a valid pitch token, so non-confident
    misdetections still cannot corrupt candidates.
    """
    by_idx = {a.note_index: a for a in answers}
    for i, note_el in enumerate(pitched_notes):
        ans = by_idx.get(i)
        if ans is None or not ans.usable:
            continue
        pitch_el = note_el.find("pitch")
        if pitch_el is None:
            continue
        step_el = pitch_el.find("step")
        oct_el = pitch_el.find("octave")
        if step_el is None or oct_el is None:
            continue
        cur_step = (step_el.text or "").strip().upper()
        try:
            cur_oct = int((oct_el.text or "").strip())
        except ValueError:
            cur_oct = -1
        new_step = ans.step
        new_oct = ans.octave

        if cur_step == new_step and cur_oct == new_oct:
            continue
        if cur_step == new_step and cur_oct != new_oct:
            oct_el.text = str(new_oct)
            continue
        # Step disagreement. Confident primitive → trust it; the alter
        # for the new step is reapplied later by
        # `apply_key_signature_alters` if it falls in the active key.
        step_el.text = new_step
        oct_el.text = str(new_oct)
        alter_el = pitch_el.find("alter")
        if alter_el is not None:
            pitch_el.remove(alter_el)
    return root


def _apply_rhythm(
    root: ET.Element,
    pitched_notes: list[ET.Element],
    answers: list[RhythmAnswer],
    expected_total_duration: int | None,
    *,
    measure_number: int | None = None,
    page_index: int | None = None,
) -> ET.Element:
    """Overwrite `<duration>`, `<type>`, and `<dot/>` per-note.

    Iter-34: the prior all-or-nothing revert (if total != expected,
    wipe the entire rhythm pass) was throwing away correct per-note
    answers whenever a single answer pushed the measure over budget.
    Replace with per-note application: each candidate answer is
    accepted only if (a) its token is valid AND (b) applying it would
    not push the measure's running total beyond
    `expected_total_duration`. Notes without a usable answer keep
    their candidate duration, as do notes whose proposed duration
    would overflow the remaining budget.

    Iter-37: emit a `record_rhythm_decision` per note so the trace
    artifact shows exactly which primitive answer was applied vs
    skipped, with the candidate state at the start and the final
    state for that note. Future planners use this to localise where
    a wrong final duration came from.
    """
    initial_total = _measure_total_duration(root)
    by_idx = {a.note_index: a for a in answers}
    record = (
        measure_number is not None
        and page_index is not None
        and debug.is_enabled()
    )

    for i, note_el in enumerate(pitched_notes):
        type_el = note_el.find("type")
        cand_type = (type_el.text or "").strip() if type_el is not None else ""
        cand_dot = note_el.find("dot") is not None
        dur_el = note_el.find("duration")
        cur_dur_str = (dur_el.text or "").strip() if dur_el is not None else "0"
        try:
            cur_dur = int(cur_dur_str or "0")
        except ValueError:
            cur_dur = 0

        ans = by_idx.get(i)

        def _emit(decision: str, *, final_dur: int, final_dot: bool) -> None:
            if not record:
                return
            debug.record_rhythm_decision(
                measure_number=measure_number,  # type: ignore[arg-type]
                page_index=page_index,  # type: ignore[arg-type]
                new_index=i,
                candidate_token=cand_type,
                candidate_duration=cur_dur,
                candidate_has_dot=cand_dot,
                primitive_token=(ans.duration_token if ans is not None else ""),
                primitive_has_dot=(ans.has_dot if ans is not None else False),
                primitive_confident=(ans.confident if ans is not None else False),
                decision=decision,
                final_duration=final_dur,
                final_has_dot=final_dot,
            )

        if ans is None:
            _emit("skipped_no_answer", final_dur=cur_dur, final_dot=cand_dot)
            continue
        if not ans.usable:
            _emit("skipped_invalid_token", final_dur=cur_dur, final_dot=cand_dot)
            continue
        if ans.duration_token not in DURATION_VALUES:
            _emit("skipped_invalid_token", final_dur=cur_dur, final_dot=cand_dot)
            continue
        base = DURATION_VALUES[ans.duration_token]
        new_dur = int(round(base * 1.5)) if ans.has_dot else base

        if expected_total_duration is not None:
            tentative_total = initial_total - cur_dur + new_dur
            if tentative_total > expected_total_duration:
                _emit("skipped_overflow", final_dur=cur_dur, final_dot=cand_dot)
                continue

        if (
            cand_type == ans.duration_token
            and cand_dot == ans.has_dot
            and cur_dur == new_dur
        ):
            _emit("no_change_already_match", final_dur=new_dur, final_dot=ans.has_dot)
            continue

        if dur_el is None:
            dur_el = ET.SubElement(note_el, "duration")
        dur_el.text = str(new_dur)
        if type_el is None:
            type_el = ET.SubElement(note_el, "type")
        type_el.text = ans.duration_token
        existing_dot = note_el.find("dot")
        if ans.has_dot and existing_dot is None:
            ET.SubElement(note_el, "dot")
        elif not ans.has_dot and existing_dot is not None:
            note_el.remove(existing_dot)

        initial_total = initial_total - cur_dur + new_dur
        _emit("applied", final_dur=new_dur, final_dot=ans.has_dot)

    return root


def _measure_total_duration(root: ET.Element) -> int:
    """Sum `<duration>` over all non-chord top-level notes (incl. rests)."""
    total = 0
    for note_el in root.iter("note"):
        if note_el.find("chord") is not None:
            continue
        d = note_el.findtext("duration", default="0").strip() or "0"
        try:
            total += int(d)
        except ValueError:
            pass
    return total


def _serialize_measure(root: ET.Element, original_block: str) -> str:
    del original_block
    return ET.tostring(root, encoding="unicode")


_SHARP_ORDER = ("F", "C", "G", "D", "A", "E", "B")
_FLAT_ORDER = ("B", "E", "A", "D", "G", "C", "F")


def key_signature_letters(fifths: int) -> tuple[set[str], int]:
    """Return (set of letters affected by the key sig, alter sign)."""
    if fifths > 0:
        return (set(_SHARP_ORDER[: min(fifths, 7)]), 1)
    if fifths < 0:
        return (set(_FLAT_ORDER[: min(abs(fifths), 7)]), -1)
    return (set(), 0)


def override_key_fifths(block: str, new_fifths: int) -> str:
    """Replace the `<fifths>` value inside the first `<key>` element.

    Iter-26: used by Stage-C key primitive to override the holistic
    pass's key-signature reading with the structural primitive's
    answer. If the block has no `<key>` element we leave it alone.
    """
    pattern = re.compile(r"(<fifths>)\s*[+-]?\d+\s*(</fifths>)")
    return pattern.sub(rf"\g<1>{new_fifths}\g<2>", block, count=1)


def apply_key_signature_alters(block: str, fifths: int) -> str:
    """Add `<alter>` to every `<pitch>` whose step matches the key
    signature, IF the pitch lacks an explicit `<alter>`. Notes that
    already have `<alter>` (set explicitly by the model — typically a
    natural sign or a deliberate accidental) are left alone.

    Iter-26: this is the deterministic counterpart to the model-driven
    key primitive. Even when the model correctly reads `<fifths>2</fifths>`
    on the holistic pass, it commonly forgets to apply the alter to
    individual noteheads. Once we know the key signature with high
    confidence (validated by the primitive), we enforce alters
    mechanically.
    """
    if fifths == 0:
        return block
    affected, alter_value = key_signature_letters(fifths)
    if not affected:
        return block
    try:
        root = ET.fromstring(block)
    except ET.ParseError:
        return block

    changed = False
    for pitch_el in root.iter("pitch"):
        step_el = pitch_el.find("step")
        if step_el is None or not step_el.text:
            continue
        step = step_el.text.strip().upper()
        if step not in affected:
            continue
        existing_alter = pitch_el.find("alter")
        if existing_alter is not None:
            continue
        new_alter = ET.SubElement(pitch_el, "alter")
        new_alter.text = str(alter_value)
        children = list(pitch_el)
        pitch_el.clear()
        ordered = []
        for tag in ("step", "alter", "octave"):
            for c in children:
                if c.tag == tag:
                    ordered.append(c)
                    break
        for c in children:
            if c not in ordered:
                ordered.append(c)
        for c in ordered:
            pitch_el.append(c)
        changed = True

    if not changed:
        return block
    return ET.tostring(root, encoding="unicode")


def expected_duration_from_attributes(block: str) -> int | None:
    """Inspect `<attributes><time>` to compute beats * divisions.

    Returns None if the block has no time signature (most measures
    after measure 1 won't, since attributes are only set in m1).
    """
    try:
        root = ET.fromstring(block)
    except ET.ParseError:
        return None
    time_el = root.find(".//time")
    if time_el is None:
        return None
    beats = time_el.findtext("beats", default="").strip()
    beat_type = time_el.findtext("beat-type", default="").strip()
    try:
        b = int(beats)
        bt = int(beat_type)
    except ValueError:
        return None
    if bt <= 0:
        return None
    total_units = b * (16 // bt) if (16 % bt == 0) else None
    return total_units


def extract_key_fifths(block: str) -> int:
    try:
        root = ET.fromstring(block)
    except ET.ParseError:
        return 0
    fifths = root.findtext(".//key/fifths", default="0").strip()
    try:
        return int(fifths)
    except ValueError:
        return 0


def extract_time_signature(block: str) -> tuple[int, int]:
    try:
        root = ET.fromstring(block)
    except ET.ParseError:
        return (4, 4)
    beats = root.findtext(".//time/beats", default="4").strip()
    beat_type = root.findtext(".//time/beat-type", default="4").strip()
    try:
        return (int(beats), int(beat_type))
    except ValueError:
        return (4, 4)
