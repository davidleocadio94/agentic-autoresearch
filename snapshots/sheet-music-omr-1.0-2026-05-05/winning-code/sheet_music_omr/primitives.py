"""Per-primitive sub-questions for Stage-C correction.

Iter-17 introduced a primitive-decomposition pass on top of the
iter-10 baseline (Stage-A multi-sample vote + Stage-B verify):

    Stage-C-pitch:  for each detected notehead, ask the model the
                    pitch (step + octave) using a NARROW per-notehead
                    zoom. Zoom isolates ONE notehead → focused vision.
    Stage-C-rhythm: for each beam group, ask the model the duration
                    of every notehead in the group using a WIDE
                    beam-group zoom. Wide zoom shows beam/flag context.

Iter-26 adds a third Stage-C primitive — the KEY-SIGNATURE primitive.
The model is given the strip between the clef and the first notehead
and asked to enumerate accidentals left-to-right. The answer is
parsed into (count, letters, fifths) and validated against the
canonical sharp/flat order. When usable it (a) overrides the
candidate's `<fifths>` and (b) is injected as a one-line ACTIVE KEY
caption into every per-notehead pitch primitive prompt for the page,
so the pitch primitive sees the active key while reading the
notehead. This addresses the d-major-m2 stuck case where Stage-A
holistic relocates a C#5 to F#5 because both letters are sharped in
the key.

The two zoom geometries (pitch vs rhythm) are intentionally different.
Iter-14 proved the narrow notehead-only zoom is great for pitch but
corrupts rhythm. So pitch goes narrow, rhythm goes wide.

This module ONLY makes calls and returns parsed answers. It does not
mutate XML — that's the assembler's job.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass

from .preproc import PrimitiveZoom
from .prompts import (
    KEY_SIGNATURE_PRIMITIVE_PROMPT,
    MEASURE_RHYTHM_PRIMITIVE_PROMPT,
    NOTEHEAD_VERIFY_PROMPT,
    PITCH_PRIMITIVE_PROMPT,
    RHYTHM_PRIMITIVE_PROMPT,
)

# Iter-42: N=3 self-consistency majority voting at the primitive level.
# Per-call sample noise has been the dominant noise source the last
# several iters (sigma ~= 0.125, mostly conf 0.0). Voting at the
# primitive level collapses that noise BEFORE it propagates downstream
# to the assembler. Open hypothesis from iter-36, validated structurally
# at the measure-crop level in iter-13.
PRIMITIVE_VOTE_N = 3


@dataclass(frozen=True)
class PitchAnswer:
    note_index: int
    step: str  # "A".."G" or "" if unreadable
    octave: int  # -1 if unreadable
    confident: bool  # True iff the model gave a clean answer

    @property
    def usable(self) -> bool:
        return (
            self.confident
            and self.step in "ABCDEFG"
            and 0 <= self.octave <= 8
        )


@dataclass(frozen=True)
class KeyAnswer:
    """Iter-26: parsed answer from the key-signature primitive.

    `fifths` is the canonical -7..7 integer for the key signature, or
    None when the model said "unknown" (the count/ordering didn't
    match a canonical major/minor key signature). `letters` is the
    enumerated accidental letters in left-to-right order (used as a
    structural cross-check via `usable` and as text for the
    downstream pitch primitive prompt).
    """

    count: int
    accidental_kind: str  # "sharp" or "flat" or "" if count==0/unknown
    letters: tuple[str, ...]
    fifths: int | None

    @property
    def usable(self) -> bool:
        if self.fifths is None:
            return False
        if self.fifths == 0:
            return self.count == 0
        canonical = _canonical_letters(self.fifths)
        if canonical is None:
            return False
        if self.count != abs(self.fifths):
            return False
        return tuple(letter.upper() for letter in self.letters) == canonical


_SHARP_ORDER = ("F", "C", "G", "D", "A", "E", "B")
_FLAT_ORDER = ("B", "E", "A", "D", "G", "C", "F")


def _canonical_letters(fifths: int) -> tuple[str, ...] | None:
    if fifths == 0:
        return ()
    if 1 <= fifths <= 7:
        return _SHARP_ORDER[:fifths]
    if -7 <= fifths <= -1:
        return _FLAT_ORDER[: abs(fifths)]
    return None


def canonical_letters(fifths: int) -> tuple[str, ...]:
    """Public helper: canonical letter sequence for a key signature."""
    result = _canonical_letters(fifths)
    return result if result is not None else ()


@dataclass(frozen=True)
class VerifyAnswer:
    """Iter-43: parsed answer from the per-detection verify primitive.

    `is_real` is True iff the model said the glyph at the center of
    the crop is a real notehead. `confident` is True iff the response
    parsed cleanly to yes/no; an unparseable answer falls back to
    `is_real=True` (treat as real, defer to downstream guards) but
    `confident=False` so callers can detect parse failures and
    fall back to the geometry trim path.
    """

    note_index: int
    is_real: bool
    confident: bool


@dataclass(frozen=True)
class RhythmAnswer:
    note_index: int
    duration_token: str  # "quarter" / "eighth" / "16th" / "half" / "whole"
    has_dot: bool
    confident: bool

    VALID_TOKENS = ("whole", "half", "quarter", "eighth", "16th")

    @property
    def usable(self) -> bool:
        return self.confident and self.duration_token in self.VALID_TOKENS


def call_key_primitive(
    invoke,
    key_zoom_png: bytes,
    *,
    page_index: int,
) -> KeyAnswer:
    """Iter-26: ask the model to enumerate key-sig accidentals on the
    isolated gutter zoom and return a parsed KeyAnswer. The structural
    cross-check (letters match canonical order for the parsed fifths)
    is enforced via KeyAnswer.usable on the caller side.
    """
    try:
        text = invoke(key_zoom_png, KEY_SIGNATURE_PRIMITIVE_PROMPT, page_index=page_index)
    except Exception:
        return KeyAnswer(count=0, accidental_kind="", letters=(), fifths=None)
    return _parse_key(text)


_FIFTHS_TO_MAJOR = {
    0: "C major",
    1: "G major",
    2: "D major",
    3: "A major",
    4: "E major",
    5: "B major",
    6: "F# major",
    7: "C# major",
    -1: "F major",
    -2: "Bb major",
    -3: "Eb major",
    -4: "Ab major",
    -5: "Db major",
    -6: "Gb major",
    -7: "Cb major",
}


def key_caption(fifths: int, letters: tuple[str, ...] = ()) -> str:
    """Iter-26: short authoritative one-line caption for the active key.

    Returns strings like "C major (no sharps or flats)", "D major (F#,
    C#)", "Bb major (Bb, Eb)". This goes into the pitch-primitive
    prompt as the ACTIVE KEY line so the pitch reader sees the key
    while reading the notehead.
    """
    if not letters and fifths != 0:
        derived = _canonical_letters(fifths)
        if derived:
            letters = derived
    name = _FIFTHS_TO_MAJOR.get(fifths, f"fifths={fifths}")
    if fifths == 0 or not letters:
        return f"{name} (no sharps or flats)"
    if fifths > 0:
        joined = ", ".join(f"{l}#" for l in letters)
    else:
        joined = ", ".join(f"{l}b" for l in letters)
    return f"{name} ({joined})"


def call_verify_primitive(
    invoke,
    zoom: PrimitiveZoom,
    *,
    page_index: int,
) -> VerifyAnswer:
    """Iter-43: ask whether the centered glyph in a pitch-zoom crop is
    a real notehead. Returns VerifyAnswer with `is_real` ∈ {True, False}
    and `confident=True` when the response parsed cleanly.

    On any exception or unparseable response we return
    `is_real=True, confident=False` — fail-open so a model glitch
    never silently drops a real notehead. Callers can use the
    `confident` flag to decide whether to trust the verify pass or
    fall back to geometry.
    """
    assert len(zoom.note_indices) == 1
    note_index = zoom.note_indices[0]
    try:
        text = invoke(zoom.png, NOTEHEAD_VERIFY_PROMPT, page_index=page_index)
    except Exception:
        return VerifyAnswer(note_index=note_index, is_real=True, confident=False)
    return _parse_verify(text, note_index)


def _parse_verify(text: str, note_index: int) -> VerifyAnswer:
    text = text.strip()
    text = re.sub(r"```[a-zA-Z]*\n?", "", text)
    text = text.replace("```", "")
    if not text:
        return VerifyAnswer(note_index=note_index, is_real=True, confident=False)
    answer_match = re.search(
        r"^\s*answer\s*[:=\-]\s*(yes|no)\b",
        text,
        re.IGNORECASE | re.MULTILINE,
    )
    if answer_match:
        verdict = answer_match.group(1).lower()
        return VerifyAnswer(
            note_index=note_index,
            is_real=(verdict == "yes"),
            confident=True,
        )
    lower = text.lower()
    if re.search(r"\byes\b", lower) and not re.search(r"\bno\b", lower):
        return VerifyAnswer(note_index=note_index, is_real=True, confident=True)
    if re.search(r"\bno\b", lower) and not re.search(r"\byes\b", lower):
        return VerifyAnswer(note_index=note_index, is_real=False, confident=True)
    return VerifyAnswer(note_index=note_index, is_real=True, confident=False)


def call_pitch_primitive(
    invoke,
    zoom: PrimitiveZoom,
    *,
    page_index: int,
    measure_number: int,
    key_fifths: int,
    key_letters: tuple[str, ...] = (),
) -> PitchAnswer:
    """Run the pitch sub-question on a single-notehead zoom.

    Iter-26: the active key caption (derived from the Stage-C key
    primitive when usable, otherwise from `key_fifths`) is injected as
    TEXT at the top of the prompt — the model sees the active key
    while reading the notehead.

    Iter-42: N=3 self-consistency majority voting. We invoke the model
    PRIMITIVE_VOTE_N times and pick the most common (step, octave)
    among the confident answers. Ties broken by the LAST sampled
    answer (most recent reading). If no confident answer in any
    sample, return unconfident.
    """
    assert len(zoom.note_indices) == 1
    note_index = zoom.note_indices[0]
    if not key_letters and key_fifths != 0:
        derived = _canonical_letters(key_fifths)
        if derived:
            key_letters = derived
    caption = key_caption(key_fifths, key_letters)
    prompt = PITCH_PRIMITIVE_PROMPT.format(key_caption=caption)

    samples: list[PitchAnswer] = []
    for _ in range(PRIMITIVE_VOTE_N):
        try:
            text = invoke(zoom.png, prompt, page_index=page_index)
            samples.append(_parse_pitch(text, note_index))
        except Exception:
            samples.append(
                PitchAnswer(note_index=note_index, step="", octave=-1, confident=False)
            )
    return _vote_pitch(samples, note_index)


def _vote_pitch(samples: list[PitchAnswer], note_index: int) -> PitchAnswer:
    """Pick the majority (step, octave) among confident samples.

    Ties broken by the LAST occurrence (most recent sample, which
    benefits from any drift toward correctness across repeats — and
    avoids penalizing a sample purely on order). If zero confident
    samples, return unconfident.
    """
    confident = [s for s in samples if s.confident and s.step in "ABCDEFG"]
    if not confident:
        return PitchAnswer(note_index=note_index, step="", octave=-1, confident=False)
    counts: Counter[tuple[str, int]] = Counter()
    last_seen: dict[tuple[str, int], int] = {}
    for i, s in enumerate(confident):
        key = (s.step, s.octave)
        counts[key] += 1
        last_seen[key] = i
    best_count = max(counts.values())
    winners = [k for k, c in counts.items() if c == best_count]
    winner = max(winners, key=lambda k: last_seen[k])
    step, octave = winner
    return PitchAnswer(note_index=note_index, step=step, octave=octave, confident=True)


def call_measure_rhythm_primitive(
    invoke,
    measure_rhythm_png: bytes,
    *,
    n_notes: int,
    page_index: int,
    measure_number: int,
    time_beats: int,
    time_beat_type: int,
) -> list[RhythmAnswer]:
    """Iter-41: ONE rhythm call per measure, returning N answers.

    Iter-42: N=3 self-consistency majority voting. We invoke the model
    PRIMITIVE_VOTE_N times and vote per-note-index across samples on
    the (duration_token, has_dot) tuple. Iter-41 confirmed the rhythm
    primitive returns PERFECT answers when sampled cleanly — voting
    suppresses the per-call sample noise that has been propagating
    downstream.
    """
    if n_notes <= 0:
        return []
    note_indices = tuple(range(n_notes))
    prompt = MEASURE_RHYTHM_PRIMITIVE_PROMPT.format(
        n_notes=n_notes,
        time_caption=f"{time_beats}/{time_beat_type}",
    )

    samples: list[list[RhythmAnswer]] = []
    for _ in range(PRIMITIVE_VOTE_N):
        try:
            text = invoke(measure_rhythm_png, prompt, page_index=page_index)
            samples.append(_parse_rhythm(text, note_indices))
        except Exception:
            samples.append(
                [
                    RhythmAnswer(
                        note_index=idx,
                        duration_token="",
                        has_dot=False,
                        confident=False,
                    )
                    for idx in note_indices
                ]
            )
    return _vote_rhythm(samples, note_indices)


def _vote_rhythm(
    samples: list[list[RhythmAnswer]], note_indices: tuple[int, ...]
) -> list[RhythmAnswer]:
    """Per-index majority vote on (duration_token, has_dot) across samples.

    For each note position, look at all samples' confident answers.
    Pick the most common (token, dot) tuple; ties broken by the LAST
    sample. If a position has zero confident answers across all
    samples, return unconfident for that position.
    """
    voted: list[RhythmAnswer] = []
    for pos, idx in enumerate(note_indices):
        confident: list[RhythmAnswer] = []
        for sample in samples:
            if pos < len(sample):
                ans = sample[pos]
                if ans.usable:
                    confident.append(ans)
        if not confident:
            voted.append(
                RhythmAnswer(
                    note_index=idx,
                    duration_token="",
                    has_dot=False,
                    confident=False,
                )
            )
            continue
        counts: Counter[tuple[str, bool]] = Counter()
        last_seen: dict[tuple[str, bool], int] = {}
        for i, ans in enumerate(confident):
            key = (ans.duration_token, ans.has_dot)
            counts[key] += 1
            last_seen[key] = i
        best_count = max(counts.values())
        winners = [k for k, c in counts.items() if c == best_count]
        winner = max(winners, key=lambda k: last_seen[k])
        token, has_dot = winner
        voted.append(
            RhythmAnswer(
                note_index=idx,
                duration_token=token,
                has_dot=has_dot,
                confident=True,
            )
        )
    return voted


def call_rhythm_primitive(
    invoke,
    zoom: PrimitiveZoom,
    *,
    page_index: int,
    measure_number: int,
    time_beats: int,
    time_beat_type: int,
) -> list[RhythmAnswer]:
    """Run the rhythm sub-question on a beam-group zoom (wide).

    Returns ONE RhythmAnswer per notehead in the group. The model sees
    the full beam structure between noteheads so it can count beams
    correctly (the iter-14 lesson).
    """
    n = len(zoom.note_indices)
    if n == 0:
        return []
    prompt = RHYTHM_PRIMITIVE_PROMPT.format(
        n_notes=n,
        time_caption=f"{time_beats}/{time_beat_type}",
    )
    try:
        text = invoke(zoom.png, prompt, page_index=page_index)
    except Exception:
        return [
            RhythmAnswer(
                note_index=idx, duration_token="", has_dot=False, confident=False
            )
            for idx in zoom.note_indices
        ]
    return _parse_rhythm(text, zoom.note_indices)


# ---------------------------------------------------------------------------
# Parsers


_PITCH_TOKEN_RE = re.compile(r"\b([A-G])\s*([0-8])\b", re.IGNORECASE)
_ANSWER_LINE_RE = re.compile(
    r"^\s*(?:line\s*4\s*[\-:]?\s*)?answer\s*[:=\-]\s*(.+?)\s*$",
    re.IGNORECASE | re.MULTILINE,
)


def _parse_pitch(text: str, note_index: int) -> PitchAnswer:
    """Iter-30: pitch primitive now answers in a 4-line chain-of-thought
    (POSITION / LETTER / KEY-CHECK / ANSWER). Prefer the ANSWER line;
    if absent, fall back to the LAST pitch token in the response (the
    final answer when the model omits the label) — and only fall back
    to the first token if no later token exists. Bare-token responses
    from the legacy prompt still work via the last-token fallback.
    """
    text = text.strip()
    text = re.sub(r"```[a-zA-Z]*\n?", "", text)
    text = text.replace("```", "")
    if not text:
        return PitchAnswer(note_index=note_index, step="", octave=-1, confident=False)
    payload_lower = text.strip().lower()
    if payload_lower.startswith("unread") or payload_lower.startswith("unsure"):
        return PitchAnswer(note_index=note_index, step="", octave=-1, confident=False)

    answer_match = _ANSWER_LINE_RE.search(text)
    if answer_match:
        answer_payload = answer_match.group(1).strip()
        if answer_payload.lower().startswith("unread") or answer_payload.lower().startswith("unsure"):
            return PitchAnswer(note_index=note_index, step="", octave=-1, confident=False)
        m = _PITCH_TOKEN_RE.search(answer_payload)
        if m:
            step = m.group(1).upper()
            try:
                octave = int(m.group(2))
            except ValueError:
                return PitchAnswer(
                    note_index=note_index, step="", octave=-1, confident=False
                )
            return PitchAnswer(
                note_index=note_index, step=step, octave=octave, confident=True
            )

    matches = list(_PITCH_TOKEN_RE.finditer(text))
    if not matches:
        return PitchAnswer(note_index=note_index, step="", octave=-1, confident=False)
    m = matches[-1]
    step = m.group(1).upper()
    try:
        octave = int(m.group(2))
    except ValueError:
        return PitchAnswer(note_index=note_index, step="", octave=-1, confident=False)
    return PitchAnswer(note_index=note_index, step=step, octave=octave, confident=True)


_RHYTHM_TOKENS = {
    "whole": "whole",
    "half": "half",
    "quarter": "quarter",
    "eighth": "eighth",
    "8th": "eighth",
    "16th": "16th",
    "sixteenth": "16th",
}


def _parse_rhythm(text: str, note_indices: tuple[int, ...]) -> list[RhythmAnswer]:
    text = text.strip()
    text = re.sub(r"```[a-zA-Z]*\n?", "", text)
    text = text.replace("```", "")
    answers: dict[int, RhythmAnswer] = {}
    for line in text.splitlines():
        s = line.strip().lower()
        if not s:
            continue
        m_idx = re.match(r"^\s*(\d+)\s*[:\.\)\-]\s*(.+)$", s)
        if m_idx:
            try:
                ordinal = int(m_idx.group(1))
            except ValueError:
                continue
            payload = m_idx.group(2)
            note_idx_in_group = ordinal - 1
            if not (0 <= note_idx_in_group < len(note_indices)):
                continue
            note_index = note_indices[note_idx_in_group]
        else:
            continue
        token = ""
        for kw, canon in _RHYTHM_TOKENS.items():
            if kw in payload:
                token = canon
                break
        has_dot = ("dot" in payload) and ("no dot" not in payload) and (
            "undotted" not in payload
        )
        if not token:
            answers[note_index] = RhythmAnswer(
                note_index=note_index,
                duration_token="",
                has_dot=False,
                confident=False,
            )
        else:
            answers[note_index] = RhythmAnswer(
                note_index=note_index,
                duration_token=token,
                has_dot=has_dot,
                confident=True,
            )
    return [
        answers.get(
            idx,
            RhythmAnswer(
                note_index=idx, duration_token="", has_dot=False, confident=False
            ),
        )
        for idx in note_indices
    ]


_KEY_COUNT_RE = re.compile(
    r"^\s*count\s*[:=]\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE
)
_KEY_LETTERS_RE = re.compile(
    r"^\s*letters?\s*[:=]\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE
)
_KEY_FIFTHS_RE = re.compile(
    r"^\s*fifths\s*[:=]\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE
)


def _parse_key(text: str) -> KeyAnswer:
    text = text.strip()
    text = re.sub(r"```[a-zA-Z]*\n?", "", text)
    text = text.replace("```", "")
    if not text:
        return KeyAnswer(count=0, accidental_kind="", letters=(), fifths=None)

    count_match = _KEY_COUNT_RE.search(text)
    letters_match = _KEY_LETTERS_RE.search(text)
    fifths_match = _KEY_FIFTHS_RE.search(text)

    count = 0
    accidental_kind = ""
    if count_match:
        payload = count_match.group(1).strip().lower()
        if payload.startswith("0") or payload in ("none", "no", "no key"):
            count = 0
        else:
            num_match = re.match(r"^\s*(\d+)\b", payload)
            if num_match:
                try:
                    count = int(num_match.group(1))
                except ValueError:
                    count = 0
            if "sharp" in payload:
                accidental_kind = "sharp"
            elif "flat" in payload:
                accidental_kind = "flat"

    letters: tuple[str, ...] = ()
    if letters_match:
        payload = letters_match.group(1).strip().lower()
        if payload not in ("none", "no", ""):
            extracted = re.findall(r"[a-g]", payload)
            letters = tuple(letter.upper() for letter in extracted)

    fifths: int | None = None
    if fifths_match:
        payload = fifths_match.group(1).strip().lower()
        if payload.startswith("unk") or payload in ("?", ""):
            fifths = None
        else:
            sign = 1
            if payload.startswith("-"):
                sign = -1
                payload = payload[1:]
            elif payload.startswith("+"):
                payload = payload[1:]
            num_match = re.match(r"^(\d+)", payload)
            if num_match:
                try:
                    fifths = sign * int(num_match.group(1))
                    if not (-7 <= fifths <= 7):
                        fifths = None
                except ValueError:
                    fifths = None

    return KeyAnswer(
        count=count,
        accidental_kind=accidental_kind,
        letters=letters,
        fifths=fifths,
    )
