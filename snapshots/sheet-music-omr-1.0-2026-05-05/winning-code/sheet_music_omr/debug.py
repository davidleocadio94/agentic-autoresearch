"""Iter-33+35 diagnostic instrumentation: dump per-measure primitive
evidence side-by-side with assembler decisions and ground-truth tokens.

Iter-33 added rhythm-only logging on the 2 stuck rhythm fixtures. The
trace produced was already conclusive — the assembler positionally
shifted both pitch and rhythm primitive answers by one slot. To let
iter-36 verify the diagnosis (and to capture the same evidence on
fixtures *outside* 02-rhythm/ in case the bug is more general),
iter-35 extends instrumentation in three ways:

  1. Enable on every fixture, not just 02-rhythm/. The artifact lives
     in the fixture directory as `primitive-debug.json` (renamed from
     `rhythm-debug.json` to reflect the broader scope).
  2. Log pitch-primitive calls — raw response + parsed answer per
     notehead — alongside the existing rhythm logs.
  3. Log assembler alignment metadata (`notehead_count`, `n_pitched`,
     `excess`, original vs trimmed zoom counts, the trim direction
     used). The trace already shows answers come from real noteheads
     OFFSET BY ONE FROM THE LEFT in the rhythm fixtures, so the
     iter-14 LEFT-trim assumption is the suspect; this iter does NOT
     change the trim behavior — only records what it sees.

This module does NOT change scores by design: it only observes. It
does NOT mutate primitive answers, candidate blocks, or assembler
behavior. Delta-near-zero is the expected outcome.
"""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class _RhythmCallRecord:
    measure_number: int
    page_index: int
    zoom_note_indices: tuple[int, ...]
    raw_response: str
    parsed_answers: list[dict]


@dataclass
class _PitchCallRecord:
    measure_number: int
    page_index: int
    zoom_note_index: int
    raw_response: str
    parsed_answer: dict


@dataclass
class _VerifyCallRecord:
    """Iter-44: per-detection VLM verify call.

    Records, for each pitch-zoom that was sent through the iter-43
    notehead-verify primitive, the original detector index, the
    detector x-position, the parsed verdict (`is_real`), and the
    confidence flag. Future planners read this to confirm whether
    surplus phantoms were correctly filtered (or, conversely, real
    noteheads were dropped) before the geometry trim ever ran.
    """

    original_index: int
    detection_x: int
    is_real: bool
    confident: bool


@dataclass
class _RhythmZipEntry:
    """Iter-44: per-pitched-note view of the rhythm-zip path.

    Captured after kept_indices are finalized — gives the planner a
    direct, side-by-side view of:
      * the post-trim slot index `i` (the index used by both pitch
        and rhythm primitives' positional zip into pitched_notes)
      * the original detector index that survived to slot `i`
      * the kept detection's x-position
      * an evenly-spaced expected x-position across [first_kept_x,
        last_kept_x] (target=n_pitched), and the residual
      * the candidate's pre-primitive (pitch, type, dot) at slot `i`
      * the rhythm-primitive's answer at slot `i`
      * the GT (pitch, type, dot) at slot `i` — when GT has the same
        number of pitched notes as the candidate and we can read it.

    No new model calls; everything is computed from existing inputs.
    """

    new_index: int
    original_index: int
    kept_x: int
    expected_x: float
    residual_x: float
    candidate_pitch: str
    candidate_type: str
    candidate_has_dot: bool
    rhythm_primitive_token: str
    rhythm_primitive_has_dot: bool
    rhythm_primitive_confident: bool
    gt_pitch: str
    gt_type: str
    gt_has_dot: bool


@dataclass
class _AlignmentRecord:
    notehead_count: int
    n_pitched: int
    excess: int
    original_pitch_zoom_count: int
    original_rhythm_zoom_count: int
    trimmed_pitch_zoom_count: int
    trimmed_rhythm_zoom_count: int
    trim_direction: str  # "left", "right", or "x_coord_phantom_drop"
    pitch_zoom_kept_indices: list[list[int]]
    rhythm_zoom_kept_indices: list[list[int]]
    kept_original_indices: list[int]  # iter-36: post-trim kept set
    # Iter-37: full geometry context so future planners can verify the
    # cutoff used by the x-coord phantom drop without re-running the
    # pipeline. `notehead_xs` is the full per-detection x-center list
    # (left-to-right). `music_start` is the cutoff (detections with
    # x < music_start are tagged as clef/key/time phantoms).
    notehead_xs: list[int] = field(default_factory=list)
    music_start: int = 0
    measure_x0: int = 0


@dataclass
class _RhythmAssemblerDecision:
    """Iter-37: per-note assembler outcome for the rhythm pass.

    Records, for each kept (post-trim) pitched note in a measure,
    whether the rhythm-primitive answer was applied, and if not, why.
    Future planners read this to confirm whether a wrong final
    duration came from a wrong primitive answer or from the assembler
    skipping a correct one (e.g. budget overflow).
    """

    new_index: int  # position in the post-trim pitched-notes list
    candidate_token: str  # candidate <type> at the start of the rhythm pass
    candidate_duration: int
    candidate_has_dot: bool
    primitive_token: str  # parsed token from rhythm primitive ("" if none)
    primitive_has_dot: bool
    primitive_confident: bool
    decision: str  # "applied", "skipped_overflow", "skipped_no_answer", "skipped_invalid_token"
    final_duration: int
    final_has_dot: bool


@dataclass
class _MeasureRecord:
    measure_number: int
    page_index: int
    rhythm_calls: list[_RhythmCallRecord] = field(default_factory=list)
    pitch_calls: list[_PitchCallRecord] = field(default_factory=list)
    verify_calls: list[_VerifyCallRecord] = field(default_factory=list)
    alignment: _AlignmentRecord | None = None
    candidate_pre_primitive_tokens: list[dict] = field(default_factory=list)
    assembled_tokens: list[dict] = field(default_factory=list)
    rhythm_decisions: list[_RhythmAssemblerDecision] = field(default_factory=list)
    rhythm_zip: list[_RhythmZipEntry] = field(default_factory=list)


@dataclass
class _Session:
    pdf_path: Path
    artifact_path: Path
    gt_path: Path
    measures: list[_MeasureRecord] = field(default_factory=list)


_SESSION: _Session | None = None

_FIXTURE_PARENTS = (
    "01-pitch",
    "02-rhythm",
    "03-meter",
    "04-key",
    "05-real",
)


def is_enabled() -> bool:
    return _SESSION is not None


def gt_path() -> Path | None:
    """Iter-44: expose the active session's GT path so callers can do
    side-by-side per-measure GT lookups during instrumentation. Returns
    None when no fixture session is active.
    """
    if _SESSION is None:
        return None
    return _SESSION.gt_path


def enable(pdf_path: Path) -> None:
    """Turn on diagnostic logging for this transcription. Active only
    when the PDF lives under a known fixture directory; otherwise no-op.

    Iter-35: extended from 02-rhythm/-only to all fixture types so we
    can compare assembler alignment behavior across pitch/key/meter
    fixtures (which currently score well) and the stuck rhythm
    fixtures. The artifact path is `primitive-debug.json`.
    """
    global _SESSION
    _SESSION = None
    pdf = Path(pdf_path).resolve()
    parts = pdf.parts
    if not any(p in parts for p in _FIXTURE_PARENTS):
        return
    fixture_dir = pdf.parent
    gt_path = fixture_dir / "ground-truth.musicxml"
    artifact_path = fixture_dir / "primitive-debug.json"
    _SESSION = _Session(
        pdf_path=pdf, artifact_path=artifact_path, gt_path=gt_path
    )


def record_rhythm_call(
    *,
    measure_number: int,
    page_index: int,
    zoom_note_indices: tuple[int, ...],
    raw_response: str,
    parsed_answers,
) -> None:
    if _SESSION is None:
        return
    measure = _ensure_measure(measure_number, page_index)
    measure.rhythm_calls.append(
        _RhythmCallRecord(
            measure_number=measure_number,
            page_index=page_index,
            zoom_note_indices=tuple(zoom_note_indices),
            raw_response=raw_response,
            parsed_answers=[
                {
                    "note_index": a.note_index,
                    "duration_token": a.duration_token,
                    "has_dot": a.has_dot,
                    "confident": a.confident,
                }
                for a in parsed_answers
            ],
        )
    )


def record_pitch_call(
    *,
    measure_number: int,
    page_index: int,
    zoom_note_index: int,
    raw_response: str,
    parsed_answer,
) -> None:
    """Iter-35: log pitch-primitive raw output + parsed answer per
    notehead. Used to verify whether the answer came from the expected
    notehead or whether assembler indexing is off by one.
    """
    if _SESSION is None:
        return
    measure = _ensure_measure(measure_number, page_index)
    measure.pitch_calls.append(
        _PitchCallRecord(
            measure_number=measure_number,
            page_index=page_index,
            zoom_note_index=zoom_note_index,
            raw_response=raw_response,
            parsed_answer={
                "note_index": parsed_answer.note_index,
                "step": parsed_answer.step,
                "octave": parsed_answer.octave,
                "confident": parsed_answer.confident,
            },
        )
    )


def record_alignment(
    *,
    measure_number: int,
    page_index: int,
    notehead_count: int,
    n_pitched: int,
    excess: int,
    original_pitch_zoom_indices: list[tuple[int, ...]],
    original_rhythm_zoom_indices: list[tuple[int, ...]],
    trimmed_pitch_zoom_indices: list[tuple[int, ...]],
    trimmed_rhythm_zoom_indices: list[tuple[int, ...]],
    trim_direction: str = "left",
    kept_original_indices: list[int] | None = None,
    notehead_xs: tuple[int, ...] | list[int] | None = None,
    music_start: int = 0,
    measure_x0: int = 0,
) -> None:
    """Iter-35/36/37: record the trim/alignment decision the assembler made.

    `kept_original_indices` (iter-36) lists the ORIGINAL detector
    indices that survived trimming, in left-to-right order.

    Iter-37 adds the geometry context that drove the kept selection:
    `notehead_xs` (left-to-right x-centers of every detection),
    `music_start` (the x-cutoff: detections with x < music_start are
    tagged as clef/key/time prefix phantoms), and `measure_x0` (the
    measure's left barline). With these, a future planner can verify
    whether the cutoff is too aggressive or too lenient by comparing
    `notehead_xs` against `music_start` directly.
    """
    if _SESSION is None:
        return
    measure = _ensure_measure(measure_number, page_index)
    measure.alignment = _AlignmentRecord(
        notehead_count=notehead_count,
        n_pitched=n_pitched,
        excess=excess,
        original_pitch_zoom_count=len(original_pitch_zoom_indices),
        original_rhythm_zoom_count=len(original_rhythm_zoom_indices),
        trimmed_pitch_zoom_count=len(trimmed_pitch_zoom_indices),
        trimmed_rhythm_zoom_count=len(trimmed_rhythm_zoom_indices),
        trim_direction=trim_direction,
        pitch_zoom_kept_indices=[
            list(idx) for idx in original_pitch_zoom_indices
        ],
        rhythm_zoom_kept_indices=[
            list(idx) for idx in original_rhythm_zoom_indices
        ],
        kept_original_indices=list(kept_original_indices or []),
        notehead_xs=list(notehead_xs or []),
        music_start=int(music_start),
        measure_x0=int(measure_x0),
    )


def record_verify_call(
    *,
    measure_number: int,
    page_index: int,
    original_index: int,
    detection_x: int,
    is_real: bool,
    confident: bool,
) -> None:
    """Iter-44: log one VLM verify per-detection call.

    Each call corresponds to one pitch-zoom (one detected notehead).
    Use the captured list to verify whether the verify primitive's
    yes/no calls actually drop the clef/key/time phantoms vs real
    noteheads.
    """
    if _SESSION is None:
        return
    measure = _ensure_measure(measure_number, page_index)
    measure.verify_calls.append(
        _VerifyCallRecord(
            original_index=original_index,
            detection_x=detection_x,
            is_real=is_real,
            confident=confident,
        )
    )


def record_rhythm_zip(
    *,
    measure_number: int,
    page_index: int,
    entries: list,
) -> None:
    """Iter-44: log the post-trim rhythm-zip per-slot view.

    `entries` is a list of `_RhythmZipEntry` already constructed by
    the caller (transcribe.py owns the candidate/GT lookup since
    debug.py shouldn't depend on assembler internals). One entry per
    post-trim pitched note in left-to-right slot order.
    """
    if _SESSION is None:
        return
    measure = _ensure_measure(measure_number, page_index)
    measure.rhythm_zip = list(entries)


def make_rhythm_zip_entry(
    *,
    new_index: int,
    original_index: int,
    kept_x: int,
    expected_x: float,
    residual_x: float,
    candidate_pitch: str,
    candidate_type: str,
    candidate_has_dot: bool,
    rhythm_primitive_token: str,
    rhythm_primitive_has_dot: bool,
    rhythm_primitive_confident: bool,
    gt_pitch: str,
    gt_type: str,
    gt_has_dot: bool,
) -> _RhythmZipEntry:
    return _RhythmZipEntry(
        new_index=new_index,
        original_index=original_index,
        kept_x=kept_x,
        expected_x=expected_x,
        residual_x=residual_x,
        candidate_pitch=candidate_pitch,
        candidate_type=candidate_type,
        candidate_has_dot=candidate_has_dot,
        rhythm_primitive_token=rhythm_primitive_token,
        rhythm_primitive_has_dot=rhythm_primitive_has_dot,
        rhythm_primitive_confident=rhythm_primitive_confident,
        gt_pitch=gt_pitch,
        gt_type=gt_type,
        gt_has_dot=gt_has_dot,
    )


def record_rhythm_decision(
    *,
    measure_number: int,
    page_index: int,
    new_index: int,
    candidate_token: str,
    candidate_duration: int,
    candidate_has_dot: bool,
    primitive_token: str,
    primitive_has_dot: bool,
    primitive_confident: bool,
    decision: str,
    final_duration: int,
    final_has_dot: bool,
) -> None:
    """Iter-37: record the assembler's per-note rhythm decision.

    Called once per pitched (post-trim) note during the rhythm pass.
    `decision` is one of:
      * "applied"               — primitive answer overrode candidate
      * "skipped_overflow"      — answer would push measure past time-sig budget
      * "skipped_no_answer"     — no primitive answer for this index (rhythm
                                  zoom didn't cover it)
      * "skipped_invalid_token" — primitive returned non-confident or empty
      * "no_change_already_match" — primitive matches candidate already

    Future planners cross-reference this against the rhythm-primitive
    raw outputs to localise whether a wrong final duration came from
    the model or from the assembler.
    """
    if _SESSION is None:
        return
    measure = _ensure_measure(measure_number, page_index)
    measure.rhythm_decisions.append(
        _RhythmAssemblerDecision(
            new_index=new_index,
            candidate_token=candidate_token,
            candidate_duration=candidate_duration,
            candidate_has_dot=candidate_has_dot,
            primitive_token=primitive_token,
            primitive_has_dot=primitive_has_dot,
            primitive_confident=primitive_confident,
            decision=decision,
            final_duration=final_duration,
            final_has_dot=final_has_dot,
        )
    )


def record_pre_primitive_block(
    *, measure_number: int, page_index: int, candidate_block: str
) -> None:
    if _SESSION is None:
        return
    measure = _ensure_measure(measure_number, page_index)
    measure.candidate_pre_primitive_tokens = _extract_rhythm_tokens(candidate_block)


def record_assembled_block(
    *, measure_number: int, page_index: int, final_block: str
) -> None:
    if _SESSION is None:
        return
    measure = _ensure_measure(measure_number, page_index)
    measure.assembled_tokens = _extract_rhythm_tokens(final_block)


def flush() -> None:
    """Write the accumulated debug record to disk and reset session."""
    global _SESSION
    if _SESSION is None:
        return
    gt_tokens_per_measure = _extract_gt_rhythm_tokens(_SESSION.gt_path)
    recorded_numbers = {m.measure_number for m in _SESSION.measures}
    payload = {
        "pdf_path": str(_SESSION.pdf_path),
        "gt_path": str(_SESSION.gt_path),
        "measures": [
            {
                "measure_number": m.measure_number,
                "page_index": m.page_index,
                "alignment": (
                    {
                        "notehead_count": m.alignment.notehead_count,
                        "n_pitched": m.alignment.n_pitched,
                        "excess": m.alignment.excess,
                        "original_pitch_zoom_count": m.alignment.original_pitch_zoom_count,
                        "original_rhythm_zoom_count": m.alignment.original_rhythm_zoom_count,
                        "trimmed_pitch_zoom_count": m.alignment.trimmed_pitch_zoom_count,
                        "trimmed_rhythm_zoom_count": m.alignment.trimmed_rhythm_zoom_count,
                        "trim_direction": m.alignment.trim_direction,
                        "pitch_zoom_kept_indices": m.alignment.pitch_zoom_kept_indices,
                        "rhythm_zoom_kept_indices": m.alignment.rhythm_zoom_kept_indices,
                        "kept_original_indices": m.alignment.kept_original_indices,
                        "notehead_xs": m.alignment.notehead_xs,
                        "music_start": m.alignment.music_start,
                        "measure_x0": m.alignment.measure_x0,
                    }
                    if m.alignment is not None
                    else None
                ),
                "rhythm_decisions": [
                    {
                        "new_index": d.new_index,
                        "candidate_token": d.candidate_token,
                        "candidate_duration": d.candidate_duration,
                        "candidate_has_dot": d.candidate_has_dot,
                        "primitive_token": d.primitive_token,
                        "primitive_has_dot": d.primitive_has_dot,
                        "primitive_confident": d.primitive_confident,
                        "decision": d.decision,
                        "final_duration": d.final_duration,
                        "final_has_dot": d.final_has_dot,
                    }
                    for d in m.rhythm_decisions
                ],
                "verify_calls": [
                    {
                        "original_index": v.original_index,
                        "detection_x": v.detection_x,
                        "is_real": v.is_real,
                        "confident": v.confident,
                    }
                    for v in m.verify_calls
                ],
                "rhythm_zip": [
                    {
                        "new_index": e.new_index,
                        "original_index": e.original_index,
                        "kept_x": e.kept_x,
                        "expected_x": e.expected_x,
                        "residual_x": e.residual_x,
                        "candidate_pitch": e.candidate_pitch,
                        "candidate_type": e.candidate_type,
                        "candidate_has_dot": e.candidate_has_dot,
                        "rhythm_primitive_token": e.rhythm_primitive_token,
                        "rhythm_primitive_has_dot": e.rhythm_primitive_has_dot,
                        "rhythm_primitive_confident": e.rhythm_primitive_confident,
                        "gt_pitch": e.gt_pitch,
                        "gt_type": e.gt_type,
                        "gt_has_dot": e.gt_has_dot,
                    }
                    for e in m.rhythm_zip
                ],
                "pitch_calls": [
                    {
                        "zoom_note_index": c.zoom_note_index,
                        "raw_response": c.raw_response,
                        "parsed_answer": c.parsed_answer,
                    }
                    for c in m.pitch_calls
                ],
                "rhythm_calls": [
                    {
                        "zoom_note_indices": list(c.zoom_note_indices),
                        "raw_response": c.raw_response,
                        "parsed_answers": c.parsed_answers,
                    }
                    for c in m.rhythm_calls
                ],
                "candidate_pre_primitive_tokens": m.candidate_pre_primitive_tokens,
                "assembled_tokens": m.assembled_tokens,
                "gt_tokens": gt_tokens_per_measure.get(m.measure_number, []),
            }
            for m in _SESSION.measures
        ],
        "gt_only_measures": {
            str(num): tokens
            for num, tokens in gt_tokens_per_measure.items()
            if num not in recorded_numbers
        },
    }
    try:
        _SESSION.artifact_path.write_text(json.dumps(payload, indent=2))
    except OSError:
        pass
    _SESSION = None


def _ensure_measure(measure_number: int, page_index: int) -> _MeasureRecord:
    assert _SESSION is not None
    for m in _SESSION.measures:
        if m.measure_number == measure_number:
            return m
    rec = _MeasureRecord(measure_number=measure_number, page_index=page_index)
    _SESSION.measures.append(rec)
    return rec


def _extract_rhythm_tokens(measure_block: str) -> list[dict]:
    """Pull (type, duration, has_dot, is_rest, pitch) per note from a
    `<measure>` block. Used for both candidate and assembled snapshots.
    """
    try:
        root = ET.fromstring(measure_block)
    except ET.ParseError:
        return []
    tokens: list[dict] = []
    for note_el in root.iter("note"):
        is_rest = note_el.find("rest") is not None
        type_el = note_el.find("type")
        dur_el = note_el.find("duration")
        has_dot = note_el.find("dot") is not None
        pitch_el = note_el.find("pitch")
        pitch_repr = ""
        if pitch_el is not None:
            step = pitch_el.findtext("step", default="?").strip()
            octv = pitch_el.findtext("octave", default="?").strip()
            pitch_repr = f"{step}{octv}"
        tokens.append(
            {
                "type": (type_el.text or "").strip() if type_el is not None else "",
                "duration": (dur_el.text or "").strip() if dur_el is not None else "",
                "has_dot": has_dot,
                "is_rest": is_rest,
                "pitch": pitch_repr,
            }
        )
    return tokens


def _extract_gt_rhythm_tokens(gt_path: Path) -> dict[int, list[dict]]:
    """Walk the GT MusicXML and extract per-measure rhythm tokens in
    the same shape as `_extract_rhythm_tokens` for side-by-side compare.
    """
    if not gt_path.exists():
        return {}
    try:
        tree = ET.parse(gt_path)
    except ET.ParseError:
        return {}
    out: dict[int, list[dict]] = {}
    root = tree.getroot()
    for measure_el in root.iter("measure"):
        num_attr = measure_el.attrib.get("number", "")
        try:
            num = int(num_attr)
        except ValueError:
            continue
        block = ET.tostring(measure_el, encoding="unicode")
        out[num] = _extract_rhythm_tokens(block)
    return out
