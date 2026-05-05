"""PDF -> MusicXML via per-measure cropping + N=3 self-consistency
+ primitive-decomposition Stage-C correction (iter-17 baseline,
extended in iter-26 with a key-signature primitive).

Iter-26 baseline: iter-17's primitive decomposition restored verbatim
(Stage-A N=3 vote + Stage-B verify + Stage-C narrow per-notehead pitch
+ wide per-beam-group rhythm). Iter-26 ADDS one structural primitive
on top — the key-signature primitive — and threads its enumerated
letters into the per-notehead pitch primitive prompt as text. The
goal is to give the pitch primitive an explicit list of which
letters sound sharp/flat in the active key, so the d-major-m2
failure mode (Stage-A holistic relocates a C#5 to F#5 because both
letters are sharps in D major) can break out of "second-look
inertia": position-on-the-ruler determines the bare letter, key
context determines the alter, and the assembler applies the alter
mechanically.

Stage-C is gated on EXACT count alignment between detected noteheads
and the candidate's pitched notes. If counts disagree we skip Stage-C
for that measure (defensive — iter-12 catastrophic regression came
from blindly applying primitive overrides on misdetected counts).

If measure detection fails on a page, fall back to the iter-3
whole-page voting strategy for that page only.
"""

from __future__ import annotations

import os
import re
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from pathlib import Path

from google import genai
from google.genai import types as genai_types

from . import debug
from .assemble import (
    apply_key_signature_alters,
    apply_primitives,
    expected_duration_from_attributes,
    extract_candidate_pitched_durations,
    extract_key_fifths,
    extract_time_signature,
    music_start_x,
    override_key_fifths,
    select_real_notehead_indices,
)
from .preproc import MeasureCrop, detect_measure_crops
from .primitives import (
    canonical_letters,
    call_key_primitive,
    call_measure_rhythm_primitive,
    call_pitch_primitive,
    call_verify_primitive,
)
from .prompts import MEASURE_PROMPT, PAGE_PROMPT, VERIFY_PROMPT

DEFAULT_MODEL = "gemini-3.1-pro-preview"
DEFAULT_RENDER_DPI = 600
DEFAULT_RENDER_DPIS: tuple[int, ...] = (DEFAULT_RENDER_DPI,)  # for back-compat re-export
DEFAULT_VERIFY_DPI = DEFAULT_RENDER_DPI  # back-compat re-export
DEFAULT_FALLBACK_DPI = 400
DEFAULT_MAX_OUTPUT_TOKENS = 32_000
DEFAULT_TEMPERATURE = 0.4
DEFAULT_TIMEOUT_MS = 300_000
DEFAULT_N_SAMPLES = 3

PRICE_INPUT_PER_M = 2.00
PRICE_OUTPUT_PER_M = 12.00

_USAGE = {"input_tokens": 0, "output_tokens": 0, "calls": 0}


class GeminiOmrError(Exception):
    pass


def reset_usage() -> None:
    _USAGE["input_tokens"] = 0
    _USAGE["output_tokens"] = 0
    _USAGE["calls"] = 0


def usage_cost() -> dict:
    cost = (
        _USAGE["input_tokens"] / 1_000_000 * PRICE_INPUT_PER_M
        + _USAGE["output_tokens"] / 1_000_000 * PRICE_OUTPUT_PER_M
    )
    return {
        "input_tokens": _USAGE["input_tokens"],
        "output_tokens": _USAGE["output_tokens"],
        "calls": _USAGE["calls"],
        "cost_usd": round(cost, 6),
    }


def transcribe(pdf_path: Path) -> str:
    debug.enable(Path(pdf_path))
    try:
        return _pdf_to_musicxml(
            Path(pdf_path),
            model=DEFAULT_MODEL,
            render_dpi=DEFAULT_RENDER_DPI,
            n_samples=DEFAULT_N_SAMPLES,
        )
    finally:
        debug.flush()


def _pdf_to_musicxml(
    pdf: Path, *, model: str, render_dpi: int, n_samples: int
) -> str:
    key = _resolve_api_key()
    client = genai.Client(api_key=key)
    crops = detect_measure_crops(pdf, dpi=render_dpi)
    if not crops:
        raise GeminiOmrError("preproc produced no crops")

    by_page: dict[int, list[MeasureCrop]] = defaultdict(list)
    for c in crops:
        by_page[c.page_index].append(c)

    measure_blocks: list[str] = []
    measure_number = 1
    cur_key_fifths = 0
    cur_key_letters: tuple[str, ...] = ()
    cur_time = (4, 4)
    cur_total_units = 16  # 4/4 default

    for page_index in sorted(by_page):
        page_crops = sorted(by_page[page_index], key=lambda c: c.measure_index_on_page)
        is_fallback = any(c.detection_failed for c in page_crops)
        if is_fallback:
            page_xml_samples = [
                _call_gemini_page(client, model, page_crops[0].png, page_index=page_index)
                for _ in range(n_samples)
            ]
            voted = _vote_measures_from_pages(page_xml_samples)
            for block in voted:
                renumbered = _renumber_measure(block, measure_number)
                measure_blocks.append(renumbered)
                measure_number += 1
        else:
            for crop in page_crops:
                samples = [
                    _call_gemini_measure(
                        client,
                        model,
                        crop.png,
                        page_index=page_index,
                        measure_index_on_page=crop.measure_index_on_page,
                        total_on_page=crop.total_on_page,
                        global_measure_number=measure_number,
                    )
                    for _ in range(n_samples)
                ]
                block = _vote_single_measure(
                    samples,
                    measure_number,
                    pick_last=crop.measure_index_on_page > 0,
                )
                if block is None:
                    raise GeminiOmrError(
                        f"no parseable measure on page {page_index + 1} "
                        f"measure {crop.measure_index_on_page + 1}"
                    )
                # Stage-B verification.
                verified = _verify_measure(
                    client,
                    model,
                    crop.png,
                    candidate_block=block,
                    target_number=measure_number,
                    page_index=page_index,
                )
                stage_b_block = verified or block

                # Iter-26: Stage-C key-signature primitive — runs only
                # on the FIRST measure of each page (where the key sig
                # is visible) and only when a key zoom was built.
                # Returns the enumerated letters so we can feed them
                # into pitch primitives this page.
                key_primitive_letters: tuple[str, ...] = ()
                if crop.key_zoom_png is not None and "<key>" in stage_b_block:
                    stage_b_block, key_primitive_letters = (
                        _maybe_apply_key_primitive(
                            client,
                            model,
                            key_zoom_png=crop.key_zoom_png,
                            candidate_block=stage_b_block,
                            page_index=page_index,
                        )
                    )

                # Refresh attribute cache from this block if it sets
                # attributes (only measure 1 does, by convention).
                fifths_in_block = extract_key_fifths(stage_b_block)
                if "<key>" in stage_b_block:
                    cur_key_fifths = fifths_in_block
                    if key_primitive_letters:
                        cur_key_letters = key_primitive_letters
                    else:
                        cur_key_letters = canonical_letters(cur_key_fifths)
                if "<time>" in stage_b_block:
                    cur_time = extract_time_signature(stage_b_block)
                    cur_total_units = _expected_units(cur_time)
                expected_units = (
                    expected_duration_from_attributes(stage_b_block)
                    if "<time>" in stage_b_block
                    else cur_total_units
                )

                # Stage-C primitive correction. Skip if no notehead
                # detection or the count doesn't align with the
                # candidate's pitched notes.
                final_block = _maybe_apply_primitives(
                    client,
                    model,
                    crop=crop,
                    candidate_block=stage_b_block,
                    target_number=measure_number,
                    page_index=page_index,
                    key_fifths=cur_key_fifths,
                    key_letters=cur_key_letters,
                    time_signature=cur_time,
                    expected_units=expected_units,
                )

                # Iter-26: enforce key-signature alters on every note
                # whose step matches the key sig but lacks an explicit
                # <alter>. Even when fifths is correct, the holistic
                # pass commonly forgets per-note alters.
                if cur_key_fifths != 0:
                    final_block = apply_key_signature_alters(
                        final_block, cur_key_fifths
                    )
                    final_block = _renumber_measure(final_block, measure_number)
                measure_blocks.append(final_block)
                measure_number += 1

    return _wrap_score(measure_blocks)


def _expected_units(time_sig: tuple[int, int]) -> int:
    beats, beat_type = time_sig
    if beat_type <= 0 or 16 % beat_type != 0:
        return 16
    return beats * (16 // beat_type)


# ---------------------------------------------------------------------------
# Stage-C orchestration


def _maybe_apply_key_primitive(
    client,
    model: str,
    *,
    key_zoom_png: bytes,
    candidate_block: str,
    page_index: int,
) -> tuple[str, tuple[str, ...]]:
    """Iter-26: ask the model to enumerate key-sig accidentals on the
    isolated gutter zoom. Override the candidate's `<fifths>` only when
    the answer is `usable` (count matches abs(fifths) and letter order
    matches the canonical sharp/flat sequence) AND the answer differs
    from what's already in the candidate. Returns the enumerated
    letters either way (so callers can feed them into pitch primitives
    even when no override was needed).
    """

    def invoke(png: bytes, prompt: str, *, page_index: int) -> str:
        return _invoke(client, model, png, prompt, page_index=page_index)

    answer = call_key_primitive(invoke, key_zoom_png, page_index=page_index)
    if not answer.usable:
        return candidate_block, ()
    letters = tuple(letter.upper() for letter in answer.letters)
    current = extract_key_fifths(candidate_block)
    if answer.fifths == current:
        return candidate_block, letters
    return override_key_fifths(candidate_block, answer.fifths or 0), letters


def _maybe_apply_primitives(
    client,
    model: str,
    *,
    crop: MeasureCrop,
    candidate_block: str,
    target_number: int,
    page_index: int,
    key_fifths: int,
    key_letters: tuple[str, ...],
    time_signature: tuple[int, int],
    expected_units: int | None,
) -> str:
    """Run Stage-C only when count alignment is exact; otherwise skip.

    Iter-14 lesson: the notehead detector consistently over-counts on
    measure 1 because clef/key/time glyphs have notehead-shaped ink in
    similar y-bands.

    Iter-36 fix: the previous LEFT-trim of `excess` zooms was off-by-one
    on the rhythm fixtures — it consumed the first real notehead along
    with the clef/time phantoms. Replace with x-coord-based phantom
    dropping: detections whose x lies in the clef + key + time prefix
    region are dropped first, and only any RESIDUAL surplus is then
    cut from the left of the post-prefix region. When notehead_xs is
    empty (e.g. an old crop without geometry) we fall back to the
    pure LEFT-trim. If the detected count is LESS than pitched, we
    skip Stage-C (under-detect means we can't safely align).

    Iter-43: introduce a VLM verification primitive that runs BEFORE
    the geometry trim. For each detected notehead's pitch-zoom crop we
    ask Gemini "is this a real notehead?" and keep only YES answers.
    If the YES count matches `n_pitched` exactly we use the verify-
    selected indices directly. Otherwise we fall back to the iter-36
    geometry trim as before. This addresses the recurring failure mode
    where phantom detections in the clef/key/time region (or interior
    artifacts) misalign the pitch primitive's per-index answers and
    the assembler's positional mapping to candidate notes — every
    iter-36/38/39 trim heuristic was a post-hoc geometry fix; this is
    the first per-detection content-based filter.
    """
    notehead_count = crop.notehead_count
    if notehead_count == 0 or not crop.pitch_zooms:
        return candidate_block

    try:
        root = ET.fromstring(candidate_block)
    except ET.ParseError:
        return candidate_block
    pitched = [n for n in root.iter("note") if n.find("pitch") is not None]
    has_chord = any(n.find("chord") is not None for n in pitched)
    if has_chord:
        return candidate_block
    n_pitched = len(pitched)
    if n_pitched == 0:
        return candidate_block

    excess = notehead_count - n_pitched
    if excess < 0:
        return candidate_block

    from .preproc import PrimitiveZoom

    def invoke(png: bytes, prompt: str, *, page_index: int) -> str:
        return _invoke(client, model, png, prompt, page_index=page_index)

    # Iter-43: per-detection VLM verification. Run only when the count
    # is actually surplus — when it already matches n_pitched there is
    # nothing to filter, and a "no" verdict on a real notehead would be
    # strictly harmful.
    #
    # Iter-44: log per-detection verify result + the detector x-position
    # so the trace artifact shows whether the verify primitive's yes/no
    # calls are actually filtering the clef/key/time phantoms (or
    # mistakenly dropping real noteheads). Pure observation — no change
    # to which detections are kept.
    verify_kept: tuple[int, ...] | None = None
    verify_real_set: frozenset[int] | None = None
    verify_answers: list = []
    if excess > 0 and crop.pitch_zooms:
        for z in crop.pitch_zooms:
            if not z.note_indices:
                continue
            ans = call_verify_primitive(invoke, z, page_index=page_index)
            verify_answers.append(ans)
            orig_idx = z.note_indices[0]
            det_x = (
                crop.notehead_xs[orig_idx]
                if 0 <= orig_idx < len(crop.notehead_xs)
                else 0
            )
            debug.record_verify_call(
                measure_number=target_number,
                page_index=page_index,
                original_index=orig_idx,
                detection_x=det_x,
                is_real=ans.is_real,
                confident=ans.confident,
            )
        kept_from_verify = tuple(
            a.note_index for a in verify_answers if a.is_real
        )
        if len(kept_from_verify) == n_pitched:
            # Sort to keep left-to-right ordering deterministically.
            verify_kept = tuple(sorted(kept_from_verify))
        elif len(kept_from_verify) > n_pitched:
            # Iter-45: verify identified MORE real detections than
            # candidate notes — still surplus, but the verify=NO
            # detections are KNOWN phantoms. Drop them unconditionally
            # by passing the verify-real set down to the grid trim as
            # the allowed candidate pool. Iter-44 instrumentation
            # showed this is the smoking-gun fix: prior code threw
            # away the verify result whenever real_count != n_pitched
            # and grid-trim then re-admitted a verify=NO phantom that
            # shifted pitch primitive answers left by one.
            verify_real_set = frozenset(kept_from_verify)
        # If real_count < n_pitched: verify cut into real noteheads
        # (false negatives). Bail back to pure-geometry behavior —
        # leaving verify_real_set None preserves the iter-39 path.

    # Iter-36: pick which detected noteheads to keep. Use x-coord
    # phantom dropping when geometry is available; otherwise fall back
    # to the iter-14 LEFT-trim. Iter-43: prefer VLM verification when
    # it produced an exact-count match. Iter-45: when verify is surplus
    # but tags some phantoms, drop verify=NO detections then grid-trim
    # the survivors.
    kept_indices: tuple[int, ...] | None = verify_kept
    trim_direction = "vlm_verify" if verify_kept is not None else "left"
    music_start = 0
    if kept_indices is None and crop.notehead_xs and crop.line_spacing_hi > 0:
        music_start = music_start_x(
            measure_x0=crop.measure_x0,
            line_spacing_hi=crop.line_spacing_hi,
            is_first_on_page=crop.is_first_on_page,
            key_fifths=key_fifths,
        )
        candidate_durations = extract_candidate_pitched_durations(candidate_block)
        kept_indices = select_real_notehead_indices(
            notehead_xs=crop.notehead_xs,
            music_start=music_start,
            target_count=n_pitched,
            measure_x1=crop.measure_x1,
            allowed_indices=verify_real_set,
            candidate_durations=(
                candidate_durations
                if len(candidate_durations) == n_pitched
                else None
            ),
        )
        if kept_indices is not None:
            trim_direction = (
                "vlm_verify_then_grid"
                if verify_real_set is not None
                else "x_coord_phantom_drop"
            )
    if kept_indices is None:
        # Fallback: LEFT-trim the leading `excess` zooms. Iter-45:
        # when verify identified known phantoms, restrict the
        # left-trim pool to verify-real detections first so we
        # never re-admit a verify=NO phantom.
        if verify_real_set is not None:
            real_sorted = tuple(sorted(verify_real_set))
            if len(real_sorted) >= n_pitched:
                trim_excess = len(real_sorted) - n_pitched
                kept_indices = tuple(real_sorted[trim_excess:])
                trim_direction = "vlm_verify_then_left"
            else:
                kept_indices = tuple(range(excess, notehead_count))
        else:
            kept_indices = tuple(range(excess, notehead_count))

    if len(kept_indices) != n_pitched:
        return candidate_block

    # Build the index remap: original index -> position in the kept
    # set. Zooms whose note_indices are not all in the kept set are
    # filtered, with the partial-overlap rhythm-zoom rule retained
    # from iter-34 (forward only the kept answers from a partially-
    # kept beam group).
    kept_set = set(kept_indices)
    orig_to_new = {orig: new for new, orig in enumerate(kept_indices)}

    pitch_zooms = tuple(
        PrimitiveZoom(
            note_indices=(orig_to_new[z.note_indices[0]],),
            png=z.png,
        )
        for z in crop.pitch_zooms
        if z.note_indices and z.note_indices[0] in kept_set
    )

    # Iter-34 partial-overlap rule preserved: keep beam-group rhythm
    # zooms whose ANY index is in kept_set; remap kept indices and
    # silently drop indices belonging to dropped phantoms.
    rhythm_zooms = tuple(
        PrimitiveZoom(
            note_indices=tuple(
                orig_to_new[i] for i in z.note_indices if i in kept_set
            ),
            png=z.png,
        )
        for z in crop.rhythm_zooms
        if any(i in kept_set for i in z.note_indices)
    )
    rhythm_zooms = tuple(z for z in rhythm_zooms if z.note_indices)

    aligned_count = n_pitched
    if len(pitch_zooms) != aligned_count:
        return candidate_block

    # Iter-35/36: record the alignment decision (now including the
    # kept-indices selection used by the x-coord phantom drop) so the
    # trace artifact shows which detections survived and which were
    # tagged as phantoms.
    debug.record_alignment(
        measure_number=target_number,
        page_index=page_index,
        notehead_count=notehead_count,
        n_pitched=n_pitched,
        excess=excess,
        original_pitch_zoom_indices=[z.note_indices for z in crop.pitch_zooms],
        original_rhythm_zoom_indices=[z.note_indices for z in crop.rhythm_zooms],
        trimmed_pitch_zoom_indices=[z.note_indices for z in pitch_zooms],
        trimmed_rhythm_zoom_indices=[z.note_indices for z in rhythm_zooms],
        trim_direction=trim_direction,
        kept_original_indices=list(kept_indices),
        notehead_xs=tuple(crop.notehead_xs),
        music_start=music_start,
        measure_x0=crop.measure_x0,
    )

    pitch_answers = []
    for zoom in pitch_zooms:
        captured: dict[str, str] = {"raw": ""}

        def capture_invoke(png: bytes, prompt: str, *, page_index: int) -> str:
            text = invoke(png, prompt, page_index=page_index)
            captured["raw"] = text
            return text

        ans = call_pitch_primitive(
            capture_invoke,
            zoom,
            page_index=page_index,
            measure_number=target_number,
            key_fifths=key_fifths,
            key_letters=key_letters,
        )
        debug.record_pitch_call(
            measure_number=target_number,
            page_index=page_index,
            zoom_note_index=zoom.note_indices[0],
            raw_response=captured["raw"],
            parsed_answer=ans,
        )
        pitch_answers.append(ans)

    debug.record_pre_primitive_block(
        measure_number=target_number,
        page_index=page_index,
        candidate_block=candidate_block,
    )

    # Iter-41: ONE measure-wide rhythm call replaces per-beam-group
    # rhythm calls. The model returns n_pitched indexed answers in
    # left-to-right order against the candidate's pitched notes — no
    # detector-index remap. If the measure-rhythm crop wasn't built
    # (degenerate geometry), skip rhythm correction for the measure.
    rhythm_answers: list = []
    if crop.measure_rhythm_zoom_png is not None:
        captured: dict[str, str] = {"raw": ""}

        def capture_invoke(png: bytes, prompt: str, *, page_index: int) -> str:
            text = invoke(png, prompt, page_index=page_index)
            captured["raw"] = text
            return text

        rhythm_answers = call_measure_rhythm_primitive(
            capture_invoke,
            crop.measure_rhythm_zoom_png,
            n_notes=aligned_count,
            page_index=page_index,
            measure_number=target_number,
            time_beats=time_signature[0],
            time_beat_type=time_signature[1],
        )
        debug.record_rhythm_call(
            measure_number=target_number,
            page_index=page_index,
            zoom_note_indices=tuple(range(aligned_count)),
            raw_response=captured["raw"],
            parsed_answers=rhythm_answers,
        )

    # Iter-44: log the post-trim per-slot rhythm-zip view. Pure
    # observation — gives the next planner a side-by-side of detector
    # x-positions, candidate state, primitive answer, and GT for each
    # slot index used by the positional zip into pitched_notes.
    if debug.is_enabled():
        _record_rhythm_zip(
            candidate_block=candidate_block,
            kept_indices=kept_indices,
            notehead_xs=crop.notehead_xs,
            rhythm_answers=rhythm_answers,
            target_number=target_number,
            page_index=page_index,
        )

    merged = apply_primitives(
        candidate_block,
        pitch_answers=pitch_answers,
        rhythm_answers=rhythm_answers,
        notehead_count=aligned_count,
        expected_total_duration=expected_units,
        measure_number=target_number,
        page_index=page_index,
    )
    final = _renumber_measure(merged, target_number)
    debug.record_assembled_block(
        measure_number=target_number,
        page_index=page_index,
        final_block=final,
    )
    return final


# ---------------------------------------------------------------------------
# Iter-44 instrumentation helpers


def _record_rhythm_zip(
    *,
    candidate_block: str,
    kept_indices: tuple[int, ...],
    notehead_xs: tuple[int, ...],
    rhythm_answers: list,
    target_number: int,
    page_index: int,
) -> None:
    """Build and emit per-slot rhythm-zip entries for one measure.

    For each post-trim slot `i`:
      * `kept_x` is the detector x-position at `kept_indices[i]`
      * `expected_x` is the i-th evenly-spaced point across
        [first_kept_x, last_kept_x] (target=len(kept_indices));
        residual measures how far the kept detection sits from a
        uniform grid — large residuals point at phantom survivors.
      * candidate token is taken from the i-th pitched note in
        `candidate_block`.
      * rhythm-primitive answer is `rhythm_answers[i]` (None when the
        primitive didn't run for this measure).
      * GT token is the i-th pitched note in the GT measure with the
        matching `number=` attribute (when available and same count).
    """
    candidate_tokens = _extract_pitched_tokens_from_block(candidate_block)
    gt_tokens = _gt_pitched_tokens_for_measure(target_number)

    if kept_indices:
        kept_xs = [
            notehead_xs[i] if 0 <= i < len(notehead_xs) else 0
            for i in kept_indices
        ]
        first_x = kept_xs[0]
        last_x = kept_xs[-1]
        n = len(kept_indices)
        if n >= 2 and last_x > first_x:
            step = (last_x - first_x) / (n - 1)
            expected_xs = [first_x + step * i for i in range(n)]
        else:
            expected_xs = [float(x) for x in kept_xs]
    else:
        kept_xs = []
        expected_xs = []

    by_idx = {a.note_index: a for a in rhythm_answers}

    entries = []
    for i, orig_idx in enumerate(kept_indices):
        kept_x = kept_xs[i] if i < len(kept_xs) else 0
        exp_x = expected_xs[i] if i < len(expected_xs) else float(kept_x)
        residual = abs(kept_x - exp_x)
        cand = (
            candidate_tokens[i]
            if i < len(candidate_tokens)
            else {"pitch": "", "type": "", "has_dot": False}
        )
        ans = by_idx.get(i)
        gt = (
            gt_tokens[i]
            if i < len(gt_tokens)
            else {"pitch": "", "type": "", "has_dot": False}
        )
        entries.append(
            debug.make_rhythm_zip_entry(
                new_index=i,
                original_index=orig_idx,
                kept_x=int(kept_x),
                expected_x=float(exp_x),
                residual_x=float(residual),
                candidate_pitch=cand.get("pitch", ""),
                candidate_type=cand.get("type", ""),
                candidate_has_dot=bool(cand.get("has_dot", False)),
                rhythm_primitive_token=(ans.duration_token if ans is not None else ""),
                rhythm_primitive_has_dot=(ans.has_dot if ans is not None else False),
                rhythm_primitive_confident=(ans.confident if ans is not None else False),
                gt_pitch=gt.get("pitch", ""),
                gt_type=gt.get("type", ""),
                gt_has_dot=bool(gt.get("has_dot", False)),
            )
        )

    debug.record_rhythm_zip(
        measure_number=target_number,
        page_index=page_index,
        entries=entries,
    )


def _extract_pitched_tokens_from_block(block: str) -> list[dict]:
    try:
        root = ET.fromstring(block)
    except ET.ParseError:
        return []
    tokens: list[dict] = []
    for note_el in root.iter("note"):
        if note_el.find("pitch") is None:
            continue
        if note_el.find("chord") is not None:
            continue
        pitch_el = note_el.find("pitch")
        step = pitch_el.findtext("step", default="?").strip() if pitch_el is not None else "?"
        octv = pitch_el.findtext("octave", default="?").strip() if pitch_el is not None else "?"
        type_el = note_el.find("type")
        has_dot = note_el.find("dot") is not None
        tokens.append(
            {
                "pitch": f"{step}{octv}",
                "type": (type_el.text or "").strip() if type_el is not None else "",
                "has_dot": has_dot,
            }
        )
    return tokens


def _gt_pitched_tokens_for_measure(measure_number: int) -> list[dict]:
    gt = debug.gt_path()
    if gt is None or not gt.exists():
        return []
    try:
        tree = ET.parse(gt)
    except ET.ParseError:
        return []
    for measure_el in tree.getroot().iter("measure"):
        try:
            num = int(measure_el.attrib.get("number", ""))
        except ValueError:
            continue
        if num != measure_number:
            continue
        return _extract_pitched_tokens_from_block(
            ET.tostring(measure_el, encoding="unicode")
        )
    return []


# ---------------------------------------------------------------------------
# Gemini calls


def _call_gemini_measure(
    client,
    model: str,
    png: bytes,
    *,
    page_index: int,
    measure_index_on_page: int,
    total_on_page: int,
    global_measure_number: int,
) -> str:
    is_first = global_measure_number == 1
    include_hint = (
        "Include `<attributes>` (divisions, key, time, clef) inside this measure."
        if is_first
        else "Do NOT include `<attributes>` — it was set in measure 1."
    )
    prompt = MEASURE_PROMPT.format(
        measure_number=measure_index_on_page + 1,
        total_measures=total_on_page,
        include_attrs_hint=include_hint,
    )
    return _invoke(client, model, png, prompt, page_index=page_index)


def _call_gemini_page(client, model: str, png: bytes, *, page_index: int) -> str:
    return _invoke(client, model, png, PAGE_PROMPT, page_index=page_index)


def _verify_measure(
    client,
    model: str,
    png: bytes,
    *,
    candidate_block: str,
    target_number: int,
    page_index: int,
) -> str | None:
    prompt = VERIFY_PROMPT.format(candidate_block=candidate_block)
    try:
        text = _invoke(client, model, png, prompt, page_index=page_index)
    except GeminiOmrError:
        return None
    found = _extract_measures(text)
    if not found:
        try:
            ET.fromstring(text)
        except ET.ParseError:
            return None
        return _renumber_measure(text, target_number)
    block = found[-1][1]
    return _renumber_measure(block, target_number)


def _invoke(client, model: str, png: bytes, prompt: str, *, page_index: int) -> str:
    try:
        resp = client.models.generate_content(
            model=model,
            contents=[
                genai_types.Part.from_bytes(data=png, mime_type="image/png"),
                prompt,
            ],
            config=genai_types.GenerateContentConfig(
                max_output_tokens=DEFAULT_MAX_OUTPUT_TOKENS,
                temperature=DEFAULT_TEMPERATURE,
                http_options=genai_types.HttpOptions(timeout=DEFAULT_TIMEOUT_MS),
            ),
        )
    except Exception as e:
        raise GeminiOmrError(
            f"Gemini call failed on page {page_index + 1}: {e}"
        ) from e
    um = getattr(resp, "usage_metadata", None)
    if um is not None:
        _USAGE["input_tokens"] += int(getattr(um, "prompt_token_count", 0) or 0)
        _USAGE["output_tokens"] += int(
            getattr(um, "candidates_token_count", 0)
            or getattr(um, "response_token_count", 0)
            or 0
        )
        _USAGE["calls"] += 1
    text = getattr(resp, "text", None) or ""
    if not text.strip():
        raise GeminiOmrError(f"Empty response page {page_index + 1}")
    return _strip_fences(text)


# ---------------------------------------------------------------------------
# Helpers


def _resolve_api_key() -> str:
    key = os.environ.get("GEMINI_API_KEY")
    if key:
        return key
    try:
        from dotenv import load_dotenv

        for parent in [Path.cwd(), *Path.cwd().parents]:
            env = parent / ".env"
            if env.exists():
                load_dotenv(env)
                key = os.environ.get("GEMINI_API_KEY")
                if key:
                    return key
    except Exception:
        pass
    raise GeminiOmrError("GEMINI_API_KEY not set (.env or environment)")


def _strip_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text)
    return text.strip()


_MEASURE_RE = re.compile(
    r"<measure[^>]*\bnumber=\"(\d+)\"[^>]*>.*?</measure>", re.DOTALL
)


def _extract_measures(page_xml: str) -> list[tuple[int, str]]:
    out: list[tuple[int, str]] = []
    for m in _MEASURE_RE.finditer(page_xml):
        try:
            num = int(m.group(1))
        except ValueError:
            continue
        out.append((num, m.group(0)))
    return out


def _measure_signature(block: str) -> tuple:
    try:
        el = ET.fromstring(block)
    except ET.ParseError:
        return ("__unparseable__", block.strip())

    events: list[tuple] = []
    voice = 1
    for child in el.iter():
        tag = child.tag
        if tag == "note":
            v_el = child.find("voice")
            if v_el is not None and v_el.text:
                try:
                    voice = int(v_el.text)
                except ValueError:
                    voice = 1
            dur = child.findtext("duration", default="").strip()
            is_chord = child.find("chord") is not None
            if child.find("rest") is not None:
                token = "rest"
            else:
                pitch = child.find("pitch")
                if pitch is None:
                    token = "?"
                else:
                    step = pitch.findtext("step", default="?").strip()
                    octv = pitch.findtext("octave", default="?").strip()
                    alter = pitch.findtext("alter", default="0").strip() or "0"
                    token = f"{step}{alter}{octv}"
            events.append((voice, token, dur, is_chord))
        elif tag in ("backup", "forward"):
            dur = child.findtext("duration", default="").strip()
            events.append((tag, dur))
    attrs_el = el.find("attributes")
    attrs_key: tuple = ()
    if attrs_el is not None:
        divisions = attrs_el.findtext("divisions", default="").strip()
        fifths = ""
        key_el = attrs_el.find("key")
        if key_el is not None:
            fifths = key_el.findtext("fifths", default="").strip()
        beats = ""
        beat_type = ""
        time_el = attrs_el.find("time")
        if time_el is not None:
            beats = time_el.findtext("beats", default="").strip()
            beat_type = time_el.findtext("beat-type", default="").strip()
        attrs_key = ("attrs", divisions, fifths, beats, beat_type)
    return (attrs_key, tuple(events))


def _vote_single_measure(
    samples: list[str], target_number: int, *, pick_last: bool
) -> str | None:
    candidates: list[tuple[tuple, str]] = []
    for sample in samples:
        found = _extract_measures(sample)
        if found:
            block = found[-1][1] if pick_last else found[0][1]
            sig = _measure_signature(block)
            candidates.append((sig, block))
        else:
            try:
                ET.fromstring(sample)
                sig = _measure_signature(sample)
                candidates.append((sig, sample))
            except ET.ParseError:
                continue

    if not candidates:
        return None

    sig_counter: Counter = Counter(sig for sig, _ in candidates)
    winning_sig, _ = sig_counter.most_common(1)[0]
    for sig, block in candidates:
        if sig == winning_sig:
            return _renumber_measure(block, target_number)
    return None


def _vote_measures_from_pages(samples: list[str]) -> list[str]:
    per_measure: dict[int, list[tuple[tuple, str]]] = defaultdict(list)
    order: list[int] = []
    seen: set[int] = set()
    for sample in samples:
        for num, block in _extract_measures(sample):
            sig = _measure_signature(block)
            per_measure[num].append((sig, block))
            if num not in seen:
                order.append(num)
                seen.add(num)

    chosen: list[str] = []
    for num in order:
        cands = per_measure[num]
        sig_counter: Counter = Counter(sig for sig, _ in cands)
        winning_sig, _ = sig_counter.most_common(1)[0]
        for sig, block in cands:
            if sig == winning_sig:
                chosen.append(block)
                break
    return chosen


def _renumber_measure(block: str, new_number: int) -> str:
    return re.sub(
        r'(<measure\b[^>]*\bnumber=)"\d+"',
        rf'\1"{new_number}"',
        block,
        count=1,
    )


def _wrap_score(measure_blocks: list[str]) -> str:
    if not measure_blocks:
        raise GeminiOmrError("no measures produced")
    part_list = '<part-list><score-part id="P1"><part-name>Music</part-name></score-part></part-list>'
    body = "\n".join(f"    {m}" for m in measure_blocks)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<score-partwise version="4.0">\n'
        f"  {part_list}\n"
        '  <part id="P1">\n'
        f"{body}\n"
        "  </part>\n"
        "</score-partwise>\n"
    )
