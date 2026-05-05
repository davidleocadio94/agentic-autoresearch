"""Prompts for the OMR pipeline.

Iter-26: restored iter-17's lean prompts (Stage-A measure transcribe,
Stage-B verify, Stage-C narrow per-notehead pitch primitive + wide
beam-group rhythm primitive), then added a Stage-C key-signature
primitive prompt and updated the pitch primitive prompt to include
the active key as TEXT. The verbose iter-18..25 prompt expansions
(numbered ruler explanation, position-line cross-check, banner
overlay reference, anti-instructions about not relocating noteheads)
are stripped out — they failed to budge the score and may have been
introducing noise.
"""

from __future__ import annotations

MEASURE_PROMPT = """\
You are looking at the start of a single staff line of sheet music. The
image spans from the very beginning of the staff (clef + key signature
+ time signature) through the RIGHT barline of measure {measure_number}
(of {total_measures} total measures on this line).

Your job: transcribe ONLY measure {measure_number} — the LAST measure
in the image, the one whose right edge is the rightmost barline. Use
the clef, key signature, and time signature from the LEFT of the image
to interpret the notes. Do NOT transcribe earlier measures.

## Side rulers (left + right gutters)

The image has thin WHITE GUTTERS on each side of the music. Each
gutter contains short horizontal TICK MARKS aligned to specific Y
coordinates of the treble staff, with a single-letter pitch label
next to each tick. Trace HORIZONTALLY from each notehead to the
ruler to determine its pitch letter and octave.

Top-to-bottom labels (BOLD = staff line, gray = space/anchor):

  C   two ledger lines above       (C6)
  B   between 1st and 2nd ledgers  (B5)
  A   one ledger line above        (A5)
  G   space directly above staff   (G5)
  F   top staff line               (F5) — BOLD
  E   top space                    (E5)
  D   4th line                     (D5) — BOLD
  C   3rd space                    (C5)
  B   middle line                  (B4) — BOLD
  A   2nd space                    (A4)
  G   2nd line                     (G4) — BOLD
  F   1st space                    (F4)
  E   bottom staff line            (E4) — BOLD
  D   space directly below staff   (D4)
  C   one ledger line below        (C4 = middle C)
  B   between 1st and 2nd ledgers  (B3)
  A   two ledger lines below       (A3)

## How to derive each note

  pitch:    Trace the notehead horizontally to the side ruler. The tick
            it lines up with gives you the letter AND octave. Each
            notehead is independent — never extrapolate from neighbors.
  alter:    Apply the key signature globally. `<alter>1</alter>` for
            sharps, `<alter>-1</alter>` for flats.
  duration: Use `<divisions>4</divisions>` (one quarter = 4):
              whole = 16, half = 8, dotted-half = 12,
              quarter = 4, dotted-quarter = 6,
              eighth = 2, dotted-eighth = 3, sixteenth = 1.
  type:     "whole" / "half" / "quarter" / "eighth" / "16th".
  dot:      Include `<dot/>` if there is an augmentation dot directly
            to the right of the notehead.

## Rhythm — count flags and beams carefully

  - 0 flags + no beam = quarter (or whole/half if open notehead).
  - 1 flag (curly hook on stem tip) OR connected by ONE beam = eighth.
  - 2 flags OR connected by TWO beams = sixteenth.
  - A beamed group of 2 eighths still means TWO separate eighth notes,
    one per notehead.
  - A dotted-quarter + eighth pair fills 2 quarter-beats together.
  - The total duration of the measure must equal the time signature
    (4 quarter-beats for 4/4, 3 for 3/4). If your notes don't add up,
    recount flags and check for dots.

## Octave-leap reminders

  - C4 sits ONE LEDGER LINE BELOW the staff (NOT in the bottom space).
  - C5 sits in the THIRD SPACE from the bottom (NOT in the top space).
  - When you see a leap between C4 and C5, both are "C" but one is
    BELOW the staff and the other is in the MIDDLE.
  - D4 is the half-line space DIRECTLY BELOW the bottom line E4.

## Key signature

  - 0 sharps/flats → `<fifths>0</fifths>`.
  - N sharps → `<fifths>+N</fifths>` (2 sharps = D major = +2).
  - Sharp order: F C G D A E B. Flat order: B E A D G C F.
  - A key-signature accidental applies to EVERY octave of that letter.

## Output format

Output ONLY the body of a single `<measure>` element, like:

  <measure number="N">
    <attributes>...</attributes>   <!-- only if this is measure 1 -->
    <note>...</note>
    <note>...</note>
  </measure>

{include_attrs_hint}

Output ONLY the `<measure>` element for measure {measure_number}. No
`<part>`, no `<part-list>`, no `<?xml ?>`, no markdown fences, no
commentary. Use exact MusicXML 4.0 tag names.
"""

# Stage B: verification / correction pass. Takes (crop + Stage-A
# output) and is asked to check two specific failure modes against
# the image — pitch octave and flag/beam counts — and return a
# corrected `<measure>` block. Returns the SAME block if no changes.
VERIFY_PROMPT = """\
You are verifying a candidate transcription of ONE measure of sheet
music against the image. You see the same per-measure crop with the
side rulers (single-letter pitch ticks in the gutters) that the
transcriber saw, plus the candidate `<measure>` block below.

The transcriber's failure modes are well-known and you must check
EXACTLY these two things:

## Check 1: pitch octave (every note)

For each `<note>` with a `<pitch>`, look at the corresponding
notehead in the image (left to right) and trace HORIZONTALLY to the
side ruler. The tick it aligns with gives the correct letter AND
octave. Pay special attention to:

  - notes BELOW the staff: D4 (just below bottom line), C4 (one
    ledger below = middle C), B3, A3. These are NOT D5/C5/B4/A4.
  - notes ABOVE the staff: G5 (space above top), A5 (one ledger
    above), B5, C6.
  - octave LEAPS: a C4 → C5 leap is a full octave; both noteheads
    are "C" but on very different ruler ticks.

If the candidate's `<step>` is correct but `<octave>` is wrong, fix
the octave. If the step is also wrong, fix that too. Each notehead
is independent — never extrapolate from a neighbor's pitch.

## Check 2: flag / beam count (every note's duration)

For each `<note>`, look at its stem in the image:

  - 0 flags AND no beam → quarter (`<duration>4</duration>` /
    `<type>quarter</type>`), or whole/half if the notehead is open.
  - 1 flag (a single curly hook on the stem tip) OR the stem is
    connected to a neighbor by ONE beam → eighth
    (`<duration>2</duration>` / `<type>eighth</type>`).
  - 2 flags OR two beams → sixteenth
    (`<duration>1</duration>` / `<type>16th</type>`).
  - An augmentation DOT to the right of a notehead multiplies
    duration by 1.5: dotted-quarter = 6, dotted-eighth = 3,
    dotted-half = 12. Include `<dot/>` in the note.

The sum of all durations in the measure MUST equal the time
signature times divisions=4 (so 16 for 4/4, 12 for 3/4). If the
sum doesn't match, you have miscounted flags/beams or missed a
dot — recount and fix.

## Other things to leave alone

Do NOT change `<attributes>` (key/time/clef/divisions). Do NOT
change voice or `<chord>` structure. Do NOT add or remove notes
unless the candidate is missing a notehead that is clearly visible
or has a note that is clearly absent from the image.

## Output

Output ONLY the corrected `<measure>` element — no commentary,
no fences, no XML preamble. If the candidate is already correct,
output it UNCHANGED. Use the exact MusicXML 4.0 tag names.

CANDIDATE TO VERIFY:

{candidate_block}
"""

# Per-primitive sub-question prompts (iter-17 Stage-C).
#
# These prompts ask ONE focused vision question per call. The pitch
# prompt asks about a single notehead in a NARROW zoom (so the model
# can't accidentally answer about a neighbor). The rhythm prompt asks
# about ALL noteheads in a beam group, in a WIDE zoom that includes
# every beam/flag connecting them — iter-14's lesson is that beam
# count requires seeing the full beam span, not a single notehead.

# Iter-26: lean iter-17 pitch prompt + ONE active-key line at the top
# (text injection of the Stage-C key primitive's enumerated letters).
# The earlier banner-overlay / numbered-ruler / position-line variants
# (iters 18..25) are dropped — they didn't budge d-major-m2 and the
# extra prompt mass was not earning its keep.

PITCH_PRIMITIVE_PROMPT = """\
ACTIVE KEY: {key_caption}

You are looking at a NARROW zoom crop centered on ONE notehead in a
treble-clef staff. There may be ONE other notehead at the very edge
of the crop — IGNORE IT. Answer only about the CENTER notehead, the
one closest to the horizontal middle of the image.

The image has thin WHITE GUTTERS on each side with horizontal TICK
MARKS labeled with single-letter pitch names. Top to bottom:

  C   two ledger lines above       (C6)
  B   between 1st and 2nd ledgers  (B5)
  A   one ledger line above        (A5)
  G   space directly above staff   (G5)
  F   top staff line               (F5) — BOLD
  E   top space                    (E5)
  D   4th line                     (D5) — BOLD
  C   3rd space                    (C5)
  B   middle line                  (B4) — BOLD
  A   2nd space                    (A4)
  G   2nd line                     (G4) — BOLD
  F   1st space                    (F4)
  E   bottom staff line            (E4) — BOLD
  D   space directly below staff   (D4)
  C   one ledger line below        (C4 = middle C)
  B   between 1st and 2nd ledgers  (B3)
  A   two ledger lines below       (A3)

## Reasoning protocol — answer in FOUR labeled lines

You MUST reason through the notehead position step by step, then
emit the final answer. Output exactly these four lines, in order:

Line 1 — POSITION: name the staff line or space the center notehead
sits on. Examples: "3rd space" / "top line" / "1st ledger line below
staff" / "space directly above staff" / "2nd line".

Line 2 — LETTER: state the bare letter that the position maps to,
using the ruler key above. The letter MUST come from the position you
named on Line 1 — do NOT shift letters because of the active key.
Example: if POSITION is "3rd space", LETTER is C; if POSITION is "top
line", LETTER is F; if POSITION is "1st ledger line below staff",
LETTER is C.

Line 3 — KEY-CHECK: state whether this letter is altered by the
ACTIVE KEY at the top of the prompt. The key signature does NOT move
the notehead on the staff — it only tells the downstream assembler
which letters to sharp/flat. State: "Letter X is/is not in the active
key — letter on the staff is still X." Do NOT rename the letter.

Line 4 — ANSWER: emit the FINAL pitch as exactly ONE token: a letter
A-G followed by a single digit 0-8 (the octave from the ruler).
Examples: C4, G5, A3, B5. The letter on Line 4 MUST match the letter
on Line 2 — never substitute a different letter because of the key.
If the notehead is genuinely unreadable, answer "unreadable".

## Critical rule

The KEY signature does NOT change which letter you read off the
staff. A notehead in the 3rd space is C — it is C in C major, C in
D major (where it SOUNDS as C#), C in F# major (still C). The pitch
primitive emits the bare staff letter. The assembler applies
`<alter>` from the active key. If you let the active key change the
letter, the assembler will double-apply the sharp and the answer
will be wrong.

Output ONLY the four lines POSITION:, LETTER:, KEY-CHECK:, ANSWER:.
No commentary outside those lines, no XML, no markdown fences.
"""

RHYTHM_PRIMITIVE_PROMPT = """\
You are looking at a WIDE zoom crop containing exactly {n_notes}
notehead(s) in horizontal order, possibly connected by a beam at the
top of their stems. The crop is wide enough to show the full
extent of any beam connecting them. Time signature: {time_caption}.

EVERY notehead in this crop is REAL and must be answered. Index the
noteheads 1..{n_notes} from LEFT to RIGHT. You MUST emit a four-line
block for ALL {n_notes} indices — do NOT collapse, skip, or merge
any notehead. Even if a notehead seems hard to read, produce its
four-line block with your best estimate; fall back to `unreadable`
only as a last resort, and never on more than one note in a group.

## Glyph lookup — STEM-TIP shape → BASE duration

Use this table top-to-bottom. Match the SHAPE you see at THIS
specific note's stem tip (and the notehead fill).

  STEM-TIP SHAPE                              | NOTEHEAD | BASE
  --------------------------------------------|----------|---------
  no stem                                     | hollow   | whole
  plain stem, no flag, no beam                | hollow   | half
  plain stem, no flag, no beam                | filled   | quarter
  ONE flag (single curly hook on stem tip)    | filled   | eighth
  ONE beam shared with a neighbor             | filled   | eighth
  TWO flags (two stacked hooks)               | filled   | 16th
  TWO parallel beams shared with a neighbor   | filled   | 16th

Beam-attribution rule (CRITICAL): a beam is a thick horizontal bar
joining the stem tips of TWO OR MORE noteheads. If your stem ends
exactly on that bar, you are on the beam. Every notehead whose stem
touches the SAME beam shares the SAME base duration — count the
beam on EACH such note's own COUNT line, not just the leftmost.
Conversely, a notehead whose stem does NOT touch the bar (it ends
in mid-air with no flag) is NOT on the beam — that note is a
quarter, even if a neighbor next to it is beamed.

Augmentation DOT: a small filled dot positioned DIRECTLY TO THE
RIGHT of the notehead, at roughly the same vertical height as the
notehead's center. It multiplies the base duration by 1.5. A dot
ABOVE or BELOW the notehead is a staccato — DO NOT count it. A
dot to the right of a NEIGHBOR is not yours.

## Reasoning protocol — FOUR labeled lines per notehead

For EACH notehead in left-to-right order (indexed 1..{n_notes}), you
MUST reason through the duration step by step, then emit the final
answer. Output exactly these four lines per notehead, in order, with
the index prefix on every line so the four lines for note K are
grouped together before the lines for note K+1.

Line 1 — `K-COUNT:` describe what you see at THIS specific note's
stem tip. State (a) the notehead fill (filled or hollow), (b) the
number of FLAGS on the stem tip, and (c) the number of BEAMS this
note's stem touches. Examples:
"filled, 0 flags, 0 beams" /
"filled, 1 flag, 0 beams" /
"filled, 0 flags, 1 beam shared with right neighbor" /
"filled, 0 flags, 1 beam shared with left neighbor" /
"filled, 0 flags, 2 beams shared across group" /
"hollow, plain stem, 0 flags, 0 beams" /
"hollow, no stem".
If this note's stem touches a shared beam, EVERY notehead under
that beam reports the same beam on its own COUNT line.

Line 2 — `K-DOT-CHECK:` state whether there is an augmentation dot
directly to the RIGHT of THIS notehead (not a neighbor's), at the
same vertical height as the notehead. Examples:
"no dot" /
"dot present to the right of this notehead".

Line 3 — `K-BASE:` look up the COUNT from Line 1 in the glyph table
above and emit the BASE duration token. The base on Line 3 MUST be
derived ONLY from this note's own flags+beams+fill — NEVER from a
neighbor's. Examples:
"COUNT was filled+0 flags+0 beams → BASE quarter" /
"COUNT was filled+1 flag → BASE eighth" /
"COUNT was filled+1 beam → BASE eighth" /
"COUNT was filled+2 beams → BASE 16th" /
"COUNT was hollow+plain stem → BASE half" /
"COUNT was hollow+no stem → BASE whole".

Line 4 — `K-ANSWER:` emit the FINAL answer as one of:
`whole`, `half`, `quarter`, `eighth`, `16th`, optionally followed by
`, dotted` if Line 2 said a dot is present. The base token on Line 4
MUST match the BASE on Line 3 — never substitute a different base
because of total-bar arithmetic, neighbor symmetry, or the time
signature. Example answers:
`quarter` / `eighth` / `eighth, dotted` / `16th` / `half, dotted` /
`quarter, dotted`.

## Critical rules

  - The TIME SIGNATURE does NOT change which glyph you read. A
    quarter note in 3/4 is still a quarter — the signature only
    affects how many beats fit in the bar, not the glyph identity.
    Do NOT "fix" a note's duration to make the bar add up.
  - A beamed pair of two eighths is TWO separate eighth notes — emit
    one ANSWER line per notehead, never collapse them into one.
  - A note whose stem does NOT touch a beam is a QUARTER, even if
    its neighbor is a beamed eighth. Read each stem independently.
  - You MUST emit the full four-line block for ALL {n_notes} indices,
    in order 1, 2, ..., {n_notes}. Missing or merged blocks corrupt
    downstream alignment.
  - If and only if a single notehead is genuinely unreadable, answer
    `unreadable` on Line 4 of THAT note. The other notes still get
    their normal four-line block.

## Worked example (do NOT copy verbatim — yours must match the image)

For a beamed pair (eighth + eighth), then a lone quarter, then a
dotted quarter, you would emit:

  1-COUNT: filled, 0 flags, 1 beam shared with right neighbor
  1-DOT-CHECK: no dot
  1-BASE: COUNT was filled+1 beam → BASE eighth
  1-ANSWER: eighth
  2-COUNT: filled, 0 flags, 1 beam shared with left neighbor
  2-DOT-CHECK: no dot
  2-BASE: COUNT was filled+1 beam → BASE eighth
  2-ANSWER: eighth
  3-COUNT: filled, 0 flags, 0 beams
  3-DOT-CHECK: no dot
  3-BASE: COUNT was filled+0 flags+0 beams → BASE quarter
  3-ANSWER: quarter
  4-COUNT: filled, 0 flags, 0 beams
  4-DOT-CHECK: dot present to the right of this notehead
  4-BASE: COUNT was filled+0 flags+0 beams → BASE quarter
  4-ANSWER: quarter, dotted

## Final output format

The parser only reads lines whose label ends in `-ANSWER:`. Make
sure each notehead has exactly one `K-ANSWER: <token>[, dotted]`
line where K is the 1-based index from 1 to {n_notes}.

No commentary outside the labeled lines, no XML, no markdown fences.
"""

# Iter-41: measure-wide rhythm primitive. Replaces the per-beam-group
# rhythm primitive call. The crop spans the FULL measure (clef/key/time
# at the start of measure 1 included — the prompt tells the model to
# skip them). The model returns ONE four-line block per pitched note
# in the candidate, indexed against `n_notes` (the candidate's pitched-
# note count), in left-to-right order. The assembler applies the
# answers positionally to the candidate's pitched notes — no notehead-
# detector remap, so phantom detections that survived the prefix
# x-coord drop can no longer corrupt the alignment.
MEASURE_RHYTHM_PRIMITIVE_PROMPT = """\
You are looking at a WIDE zoom crop of ONE complete measure of treble-
clef sheet music. The crop may begin with non-musical glyphs at the
very LEFT — a treble clef, possibly a key signature (sharps or flats),
and possibly a time signature. SKIP all of those — they are NOT notes
and you must NOT count them. Then, working LEFT-to-RIGHT, locate every
NOTEHEAD in the measure (filled or hollow oval head sitting on a staff
line, in a staff space, or on a ledger line above/below the staff).

There are EXACTLY {n_notes} noteheads to read in this measure (the
upstream pitch detector and the staff transcription both agree on
this count). Index them 1..{n_notes} from LEFT to RIGHT in the order
they appear horizontally on the staff. Time signature: {time_caption}.

You MUST emit a four-line block for ALL {n_notes} indices — do NOT
collapse, skip, or merge any notehead. Even if a notehead seems hard
to read, produce its four-line block with your best estimate; fall
back to `unreadable` only as a last resort, and never on more than
one note in the measure.

## Glyph lookup — STEM-TIP shape → BASE duration

Use this table top-to-bottom. Match the SHAPE you see at THIS
specific note's stem tip (and the notehead fill).

  STEM-TIP SHAPE                              | NOTEHEAD | BASE
  --------------------------------------------|----------|---------
  no stem                                     | hollow   | whole
  plain stem, no flag, no beam                | hollow   | half
  plain stem, no flag, no beam                | filled   | quarter
  ONE flag (single curly hook on stem tip)    | filled   | eighth
  ONE beam shared with a neighbor             | filled   | eighth
  TWO flags (two stacked hooks)               | filled   | 16th
  TWO parallel beams shared with a neighbor   | filled   | 16th

Beam-attribution rule (CRITICAL): a beam is a thick horizontal bar
joining the stem tips of TWO OR MORE noteheads. If your stem ends
exactly on that bar, you are on the beam. Every notehead whose stem
touches the SAME beam shares the SAME base duration — count the
beam on EACH such note's own COUNT line, not just the leftmost.
Conversely, a notehead whose stem does NOT touch the bar (it ends
in mid-air with no flag) is NOT on the beam — that note is a
quarter, even if a neighbor next to it is beamed.

Augmentation DOT: a small filled dot positioned DIRECTLY TO THE
RIGHT of the notehead, at roughly the same vertical height as the
notehead's center. It multiplies the base duration by 1.5. A dot
ABOVE or BELOW the notehead is a staccato — DO NOT count it. A
dot to the right of a NEIGHBOR is not yours.

## Reasoning protocol — FOUR labeled lines per notehead

For EACH notehead in left-to-right order (indexed 1..{n_notes}), you
MUST reason through the duration step by step, then emit the final
answer. Output exactly these four lines per notehead, in order, with
the index prefix on every line so the four lines for note K are
grouped together before the lines for note K+1.

Line 1 — `K-COUNT:` describe what you see at THIS specific note's
stem tip. State (a) the notehead fill (filled or hollow), (b) the
number of FLAGS on the stem tip, and (c) the number of BEAMS this
note's stem touches. Examples:
"filled, 0 flags, 0 beams" /
"filled, 1 flag, 0 beams" /
"filled, 0 flags, 1 beam shared with right neighbor" /
"filled, 0 flags, 1 beam shared with left neighbor" /
"filled, 0 flags, 2 beams shared across group" /
"hollow, plain stem, 0 flags, 0 beams" /
"hollow, no stem".
If this note's stem touches a shared beam, EVERY notehead under
that beam reports the same beam on its own COUNT line.

Line 2 — `K-DOT-CHECK:` state whether there is an augmentation dot
directly to the RIGHT of THIS notehead (not a neighbor's), at the
same vertical height as the notehead. Examples:
"no dot" /
"dot present to the right of this notehead".

Line 3 — `K-BASE:` look up the COUNT from Line 1 in the glyph table
above and emit the BASE duration token. The base on Line 3 MUST be
derived ONLY from this note's own flags+beams+fill — NEVER from a
neighbor's. Examples:
"COUNT was filled+0 flags+0 beams → BASE quarter" /
"COUNT was filled+1 flag → BASE eighth" /
"COUNT was filled+1 beam → BASE eighth" /
"COUNT was filled+2 beams → BASE 16th" /
"COUNT was hollow+plain stem → BASE half" /
"COUNT was hollow+no stem → BASE whole".

Line 4 — `K-ANSWER:` emit the FINAL answer as one of:
`whole`, `half`, `quarter`, `eighth`, `16th`, optionally followed by
`, dotted` if Line 2 said a dot is present. The base token on Line 4
MUST match the BASE on Line 3 — never substitute a different base
because of total-bar arithmetic, neighbor symmetry, or the time
signature. Example answers:
`quarter` / `eighth` / `eighth, dotted` / `16th` / `half, dotted` /
`quarter, dotted`.

## Critical rules

  - SKIP non-note glyphs at the left of the crop. The treble clef,
    any key-signature accidentals (sharps or flats sitting on
    specific staff lines just after the clef), and the time
    signature (a stack of two numerals) are NOT noteheads. Start
    indexing at 1 from the FIRST real notehead.
  - The TIME SIGNATURE does NOT change which glyph you read. A
    quarter note in 3/4 is still a quarter — the signature only
    affects how many beats fit in the bar, not the glyph identity.
    Do NOT "fix" a note's duration to make the bar add up.
  - A beamed pair of two eighths is TWO separate eighth notes — emit
    one ANSWER line per notehead, never collapse them into one.
  - A note whose stem does NOT touch a beam is a QUARTER, even if
    its neighbor is a beamed eighth. Read each stem independently.
  - You MUST emit the full four-line block for ALL {n_notes} indices,
    in order 1, 2, ..., {n_notes}. Missing or merged blocks corrupt
    downstream alignment.
  - If and only if a single notehead is genuinely unreadable, answer
    `unreadable` on Line 4 of THAT note. The other notes still get
    their normal four-line block.

## Final output format

The parser only reads lines whose label ends in `-ANSWER:`. Make
sure each notehead has exactly one `K-ANSWER: <token>[, dotted]`
line where K is the 1-based index from 1 to {n_notes}.

No commentary outside the labeled lines, no XML, no markdown fences.
"""


# Iter-43: per-detection notehead verification primitive. The notehead
# detector in preproc.py over-counts on measure 1 because clef/key/time
# glyphs have notehead-shaped ink in similar y-bands; downstream geometry
# trims (iters 36/38/39) couldn't reliably distinguish phantoms from real
# noteheads when phantoms survived the prefix drop. Per the iter-43
# planner hypothesis, we replace the post-hoc geometry trim with an
# explicit VLM verification: for each detection's narrow pitch-zoom crop
# we ask Gemini "is this a real notehead?" and keep only the YES answers.
# The crop the model sees is identical to the pitch-primitive zoom (one
# notehead, side ruler, full staff vertical extent), so a consistent
# decision criterion is applied without re-cropping.
NOTEHEAD_VERIFY_PROMPT = """\
You are looking at a NARROW zoom crop centered on ONE candidate glyph
in a treble-clef staff. The image has thin WHITE GUTTERS on each side
with horizontal TICK MARKS labeled with single-letter pitch names —
ignore those for this question; they are reference marks, not notes.

Your ONLY job is to decide whether the glyph CLOSEST TO THE HORIZONTAL
CENTER of the crop is a REAL NOTEHEAD or a non-note glyph.

A REAL NOTEHEAD is one of:
  * a FILLED black oval (quarter, eighth, 16th, beamed) sitting on a
    staff line, in a staff space, or on a ledger line above/below;
  * a HOLLOW oval with a thin outline (whole, half) sitting on a line,
    space, or ledger line.
The notehead is roughly one staff-space tall and slightly wider than
tall. It must sit on a valid pitch position (line, space, or ledger).

A NON-NOTE GLYPH (answer NO) is anything else, including:
  * a TREBLE CLEF (the curly G-clef glyph);
  * a SHARP (#), FLAT (b), or NATURAL sign (key-signature accidentals);
  * a TIME SIGNATURE NUMERAL (a digit like 4, 3, 6, 8 stacked on the
    staff);
  * a BARLINE (vertical line crossing the full staff height);
  * a REST (the wavy/curly rest glyphs, NOT noteheads);
  * pure stem/beam ink with no notehead;
  * any text, artifact, or empty-staff region.

There may be a partial neighboring notehead at the edge of the crop —
IGNORE IT. Answer ONLY about the glyph at the horizontal center of
the crop.

## Output — exactly ONE line

`answer: yes` if the centered glyph is a real notehead.
`answer: no`  otherwise.

No commentary, no XML, no markdown fences.
"""


# Iter-26: dedicated key-signature primitive. The model is given a
# narrow horizontal strip just to the right of the clef and asked to
# enumerate accidentals left-to-right and decide the canonical key.
# The output is parsed into a (count, letters, fifths) triple; the
# primitive caller validates that the letters match the canonical
# sharp/flat order for the parsed fifths before letting the answer
# override the candidate's key signature.
KEY_SIGNATURE_PRIMITIVE_PROMPT = """\
You are looking at a NARROW horizontal strip of a treble-clef staff.
The strip starts JUST AFTER the clef and extends to the right far
enough to include any key-signature accidentals (sharps or flats).
The strip MAY ALSO include the TIME SIGNATURE (a stack of two
numerals like 4/4, 3/4, 6/8) at its right edge — IGNORE the time
signature. The strip MAY ALSO clip the FIRST NOTEHEAD at the very
right edge — IGNORE any notehead. Only count and identify SHARP and
FLAT signs. This is the region where a KEY SIGNATURE — a row of
sharps (#) or flats (b) — would appear.

## What you are looking for

  * Zero, one, or more SHARP signs (#) — vertical hash with two
    horizontal bars — placed in canonical order on specific staff
    lines/spaces. Canonical sharp order: F, C, G, D, A, E, B.
  * OR zero, one, or more FLAT signs (b) — round bowl with vertical
    stem on the LEFT — placed in canonical order. Canonical flat
    order: B, E, A, D, G, C, F.
  * If there are no accidentals between the clef and the next glyph,
    the key is C major / A minor (NO key signature).

## Side ruler

The image has WHITE GUTTERS on each side with single-letter pitch
ticks for every staff line and space. Use the ruler to identify the
letter each accidental sits on.

## How to answer

Step 1 — Count the accidentals. State the COUNT and TYPE.
Step 2 — Name each accidental's letter in left-to-right order.
Step 3 — Decide the key. Canonical mappings:

  0 sharps/flats → fifths=0
  1 sharp  (F)              → fifths=1
  2 sharps (F, C)           → fifths=2
  3 sharps (F, C, G)        → fifths=3
  4 sharps (F, C, G, D)     → fifths=4
  5 sharps (F, C, G, D, A)  → fifths=5
  6 sharps (.., E)          → fifths=6
  7 sharps (.., B)          → fifths=7
  1 flat   (B)              → fifths=-1
  2 flats  (B, E)           → fifths=-2
  3 flats  (B, E, A)        → fifths=-3
  4 flats  (B, E, A, D)     → fifths=-4
  5 flats  (.., G)          → fifths=-5
  6 flats  (.., C)          → fifths=-6
  7 flats  (.., F)          → fifths=-7

If the count or ordering doesn't match a canonical sequence above,
answer `fifths: unknown`.

## Output — exactly THREE lines

Line 1: `count: <N> sharps` OR `count: <N> flats` OR `count: 0`
Line 2: `letters: <comma-separated letters in left-to-right order>` (or `letters: none`)
Line 3: `fifths: <integer in -7..7>` OR `fifths: unknown`

Examples:

  count: 0
  letters: none
  fifths: 0

  count: 2 sharps
  letters: F, C
  fifths: 2

  count: 1 flat
  letters: B
  fifths: -1

No commentary, no XML, no markdown.
"""

# Fallback for whole-page transcription when measure detection fails.
# No overlay is drawn in the fallback path, so this prompt does not
# reference the ruler.
PAGE_PROMPT = """\
Transcribe this page of sheet music as MusicXML 4.0.

Output ONLY the contents of `<part-list>` and `<part>` for this page.
Do NOT include `<?xml ...?>`, `<!DOCTYPE>`, or `<score-partwise>` wrappers.
Use the exact tag names from the MusicXML 4.0 spec. Be conservative:
if you can't read something clearly, omit it rather than guess.

Emit `<attributes>` in measure 1 with `<divisions>4</divisions>`,
the key signature, the time signature, and the clef. With divisions=4:
quarter=4, eighth=2, half=8, dotted-quarter=6, dotted-half=12, whole=16.

The key signature applies to EVERY note of that letter in EVERY octave.
Treble clef: bottom line E4, top line F5. Middle C = C4.

Output ONLY the raw MusicXML fragment, no markdown fences, no commentary.
"""
