"""Per-measure cropping at high DPI with structural side-ruler reference marks.

Iter-26 baseline: restored iter-17's primitive-decomposition layout
verbatim (single-letter side ruler in left+right gutters; per-notehead
narrow pitch zooms; per-beam-group wide rhythm zooms). The numbered
ruler / banner overlay / 4-ledger pitch zooms / position-line cross
check / multi-DPI tricks introduced after iter-17 (iters 18..25) all
failed to budge the score and several actively regressed; this iter
strips them back out.

ON TOP of the iter-17 base we keep ONE structural addition from iter-
20 — the dedicated key-signature zoom. The crop isolates the strip
between the clef and the first notehead so a Stage-C primitive can
enumerate accidentals left-to-right; the result is then injected as
TEXT into every per-notehead pitch-zoom prompt for the page so the
pitch primitive sees the active key while reading the notehead. The
zoom uses the same single-letter side ruler as the rest of the
pipeline (no numbered ruler — iter-7 lesson: dense in-image text
overloads the model).
"""

from __future__ import annotations

import io
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pymupdf
from PIL import Image, ImageDraw, ImageFont

Image.MAX_IMAGE_PIXELS = None  # Allow 600 DPI page renders without bomb-check trip.

DEFAULT_DPI = 600
DETECT_DPI = 200  # bar/staff detection runs on a downsampled copy for speed
DARK_THRESHOLD = 128
ROW_INK_MIN = 50  # min dark pixels in a row to count as music
STAFF_LINE_DARK_COUNT = 5  # >= 5 dark rows in a column = staff-line column
BARLINE_FILL_FRAC = 0.7  # column counts as a barline if >= 70% filled
PAGE_FOOTER_FRAC = 0.9  # ignore the bottom 10% (page numbers etc.)
PAD_PX_LOWRES = 24  # padding on the low-res scan; scaled when cropping

TREBLE_LINE_PITCHES = ("F", "D", "B", "G", "E")  # top -> bottom: F5 D5 B4 G4 E4
TREBLE_SPACE_PITCHES = ("E", "C", "A", "F")  # top -> bottom: E5 C5 A4 F4
TREBLE_ABOVE_ANCHORS = (
    (0.5, "G"),
    (1.0, "A"),
    (1.5, "B"),
    (2.0, "C"),
)
TREBLE_BELOW_ANCHORS = (
    (0.5, "D"),
    (1.0, "C"),
    (1.5, "B"),
    (2.0, "A"),
)
LEDGER_ANCHOR_OFFSETS = (1.0, 2.0)

GUTTER_PX_HI = 140
TICK_LEN_PX = 60

# Iter-32: rhythm-zoom-specific geometry. The rhythm primitive needs to
# see full flag/beam stem extent (stems-up flags rise 3-4 line-spacings
# above the top line) and survive Gemini's input downsampling for thin
# features (flag tips, augmentation-dot pixels). VERT_PAD_MULT=4.0
# extends above/below the staff to ~4 line-spacings (vs the parent
# measure crop's 2.5). UPSCALE=2.0 doubles linear resolution via LANCZOS
# before the model sees the image.
RHYTHM_VERT_PAD_MULT = 4.0
RHYTHM_UPSCALE = 2.0

TICK_LEN_LEDGER_PX = 90
LINE_TICK_COLOR = (0, 0, 0)
SPACE_TICK_COLOR = (120, 120, 120)
LEDGER_TICK_COLOR = (0, 0, 0)


@dataclass(frozen=True)
class PrimitiveZoom:
    """A zoom crop around a single notehead OR a beamed group of notes.

    `note_indices` is the list of 0-based notehead indices (left-to-right
    within the parent measure) covered by this zoom. For pitch zooms
    the list always has exactly one element. For rhythm zooms it
    contains every notehead in the beamed/flagged group (which can be
    a single note, a beamed pair, etc.).
    """

    note_indices: tuple[int, ...]
    png: bytes


@dataclass(frozen=True)
class MeasureCrop:
    page_index: int
    measure_index_on_page: int  # 0-based, in page order
    total_on_page: int
    png: bytes
    detection_failed: bool = False
    pitch_zooms: tuple[PrimitiveZoom, ...] = field(default_factory=tuple)
    rhythm_zooms: tuple[PrimitiveZoom, ...] = field(default_factory=tuple)
    notehead_count: int = 0  # number of detected noteheads in this measure
    # Iter-36: per-detection x-centers (full-image coords, hi-res) and
    # an estimated music-start x. The assembler uses these to drop
    # phantom detections in the clef/key/time prefix region instead of
    # the positional LEFT-trim, which was off-by-one on the rhythm
    # fixtures (it sometimes consumed the first real notehead).
    notehead_xs: tuple[int, ...] = field(default_factory=tuple)
    measure_x0: int = 0  # left edge of this measure in full-image coords
    measure_x1: int = 0  # right edge of this measure in full-image coords
    line_spacing_hi: float = 0.0
    is_first_on_page: bool = False
    # Iter-26: key-signature zoom — only set on the first measure of
    # each page where a key signature can appear. Spans the strip
    # immediately to the right of the clef and to the LEFT of the first
    # detected notehead / time signature. Used by Stage-C to enumerate
    # accidentals and inject the active key as TEXT into every per-
    # notehead pitch-zoom prompt for the page.
    key_zoom_png: bytes | None = None
    # Iter-41: ONE measure-wide rhythm crop spanning [music region of
    # the measure]. Replaces the per-beam-group rhythm zooms — the
    # primitive call asks for the FULL duration sequence in a single
    # pass, indexed against the candidate's N pitched notes. Rationale:
    # per-cluster rhythm zooms required remapping detected-notehead
    # indices to candidate-pitched-note indices, and that remap was
    # being corrupted whenever phantoms survived the prefix x-coord
    # drop (rhythm fixture 02 trace: 7 detections, 4 GT noteheads, the
    # surviving phantom at index 1 displaced every real notehead by
    # one in the rhythm pass while the assembler skipped the phantom
    # answer as overflow). The measure-wide crop sidesteps the
    # remap entirely — the model returns N tokens, one per candidate
    # note, applied positionally.
    measure_rhythm_zoom_png: bytes | None = None


def detect_measure_crops(pdf_path: Path, dpi: int = DEFAULT_DPI) -> list[MeasureCrop]:
    crops: list[MeasureCrop] = []
    with pymupdf.open(pdf_path) as doc:
        for page_index, page in enumerate(doc):
            page_crops = _split_page(page, page_index, dpi)
            crops.extend(page_crops)
    return crops


def _split_page(page, page_index: int, dpi: int) -> list[MeasureCrop]:
    detect_pix = page.get_pixmap(dpi=DETECT_DPI)
    detect_arr = _pixmap_to_gray(detect_pix)
    bounds = _detect_bounds(detect_arr)

    hi_pix = page.get_pixmap(dpi=dpi)
    hi_img = Image.open(io.BytesIO(hi_pix.tobytes("png"))).convert("RGB")

    if bounds is None:
        return [
            MeasureCrop(
                page_index=page_index,
                measure_index_on_page=0,
                total_on_page=1,
                png=_pil_to_png(hi_img),
                detection_failed=True,
            )
        ]

    y0, y1, staff_left, _staff_right, barlines, staff_lines = bounds
    if not barlines or len(staff_lines) != 5:
        return [
            MeasureCrop(
                page_index=page_index,
                measure_index_on_page=0,
                total_on_page=1,
                png=_pil_to_png(hi_img),
                detection_failed=True,
            )
        ]

    scale = dpi / DETECT_DPI
    pad = int(PAD_PX_LOWRES * scale)
    hi_W, hi_H = hi_img.size

    staff_lines_hi = [int(round(s * scale)) for s in staff_lines]
    line_spacing_hi = (staff_lines_hi[-1] - staff_lines_hi[0]) / 4.0
    target_above = int(round(line_spacing_hi * 2.5))
    target_below = int(round(line_spacing_hi * 2.5))
    raw_y0_hi = int(y0 * scale)
    raw_y1_hi = int(y1 * scale)
    top_line_hi = staff_lines_hi[0]
    bot_line_hi = staff_lines_hi[-1]
    above_existing = top_line_hi - raw_y0_hi
    below_existing = raw_y1_hi - bot_line_hi
    extra_above = max(0, target_above - above_existing)
    extra_below = max(0, target_below - below_existing)
    y0_hi = max(0, raw_y0_hi - pad - extra_above)
    y1_hi = min(hi_H, raw_y1_hi + pad + extra_below)
    staff_left_hi = max(0, int(staff_left * scale) - pad)

    crops: list[MeasureCrop] = []
    n_measures = len(barlines)
    prev_bar_hi = staff_left_hi
    for i, bar in enumerate(barlines):
        bar_hi = min(hi_W, int(bar * scale) + pad)
        # Annotated full-context crop spans staff_left → this barline
        # (so the model can read clef + key + time on the LEFT and the
        # target measure ends at the rightmost barline). This matches
        # the iter-10 baseline crop.
        measure_img = hi_img.crop((staff_left_hi, y0_hi, bar_hi, y1_hi))
        local_lines = [s - y0_hi for s in staff_lines_hi]
        annotated = _annotate_with_side_ruler(measure_img, local_lines)

        # Detect noteheads in JUST this measure's x range (not the
        # cumulative crop). For measure 1 there's no prev bar — start
        # from staff_left_hi PLUS room to skip the clef/key/time, by
        # narrowing the detection x-range later via density alignment.
        if i == 0:
            meas_x0 = staff_left_hi
        else:
            meas_x0 = prev_bar_hi
        meas_x1 = bar_hi

        notehead_xs = _detect_notehead_xs(
            hi_img, meas_x0, meas_x1, y0_hi, y1_hi, staff_lines_hi
        )

        # Build pitch + rhythm zooms from the unannotated hi-res
        # source (we add a side ruler INSIDE the zoom so the model
        # sees the same coordinate frame in zooms as in the measure).
        pitch_zooms = _build_pitch_zooms(
            hi_img=hi_img,
            notehead_xs=notehead_xs,
            y0_hi=y0_hi,
            y1_hi=y1_hi,
            staff_lines_hi=staff_lines_hi,
            line_spacing_hi=line_spacing_hi,
        )
        rhythm_zooms = _build_rhythm_zooms(
            hi_img=hi_img,
            notehead_xs=notehead_xs,
            y0_hi=y0_hi,
            y1_hi=y1_hi,
            staff_lines_hi=staff_lines_hi,
            line_spacing_hi=line_spacing_hi,
            measure_x1=bar_hi,
        )

        # Iter-26: key-signature zoom on the first measure of the page.
        key_zoom_png: bytes | None = None
        if i == 0:
            key_zoom_png = _build_key_signature_zoom(
                hi_img=hi_img,
                staff_left_hi=staff_left_hi,
                y0_hi=y0_hi,
                y1_hi=y1_hi,
                staff_lines_hi=staff_lines_hi,
                line_spacing_hi=line_spacing_hi,
            )

        # Iter-41: ONE measure-wide rhythm crop. For measure 1 it spans
        # from the staff left edge (so the clef/key/time prefix is
        # visible — the prompt explicitly tells the model to skip it),
        # for later measures from the previous barline. Same y-padding
        # and LANCZOS upscale as per-group rhythm zooms so flag/beam/dot
        # pixels survive Gemini's input downsampling.
        measure_rhythm_zoom_png = _build_measure_rhythm_zoom(
            hi_img=hi_img,
            crop_x0=staff_left_hi if i == 0 else prev_bar_hi,
            crop_x1=bar_hi,
            staff_lines_hi=staff_lines_hi,
            line_spacing_hi=line_spacing_hi,
        )

        crops.append(
            MeasureCrop(
                page_index=page_index,
                measure_index_on_page=i,
                total_on_page=n_measures,
                png=_pil_to_png(annotated),
                pitch_zooms=tuple(pitch_zooms),
                rhythm_zooms=tuple(rhythm_zooms),
                notehead_count=len(notehead_xs),
                notehead_xs=tuple(notehead_xs),
                measure_x0=meas_x0,
                measure_x1=meas_x1,
                line_spacing_hi=float(line_spacing_hi),
                is_first_on_page=(i == 0),
                key_zoom_png=key_zoom_png,
                measure_rhythm_zoom_png=measure_rhythm_zoom_png,
            )
        )
        prev_bar_hi = bar_hi
    return crops


def _detect_notehead_xs(
    hi_img: Image.Image,
    x0: int,
    x1: int,
    y0: int,
    y1: int,
    staff_lines_hi: list[int],
) -> list[int]:
    """Detect approximate x-centers of noteheads in [x0, x1] x [y0, y1].

    Strategy: count "notehead-shaped" ink in horizontal bands centered
    at every valid notehead Y (lines, spaces, ledgers). A real notehead
    is a ~spacing-tall by ~1.2x-spacing-wide blob sitting on one of
    those Y positions. The clef/time/key glyphs don't satisfy this
    column shape and produce diffuse density rather than a sharp peak.
    """
    if x1 <= x0 + 8 or y1 <= y0 + 8 or len(staff_lines_hi) != 5:
        return []
    region = hi_img.crop((x0, y0, x1, y1)).convert("L")
    arr = np.array(region)
    binary = arr < DARK_THRESHOLD
    H, W = binary.shape
    if H < 20 or W < 20:
        return []

    spacing = (staff_lines_hi[-1] - staff_lines_hi[0]) / 4.0
    if spacing <= 0:
        return []
    notehead_h = max(6, int(round(spacing * 0.85)))
    notehead_w = max(8, int(round(spacing * 1.15)))

    line_ys = sorted(s - y0 for s in staff_lines_hi)
    centers_y: list[int] = list(line_ys)
    for i in range(4):
        centers_y.append((line_ys[i] + line_ys[i + 1]) // 2)
    top = line_ys[0]
    for k in (1, 2, 3, 4):
        y = int(round(top - k * spacing / 2))
        if 0 <= y < H:
            centers_y.append(y)
    bot = line_ys[-1]
    for k in (1, 2, 3, 4):
        y = int(round(bot + k * spacing / 2))
        if 0 <= y < H:
            centers_y.append(y)
    centers_y = sorted(set(c for c in centers_y if 0 <= c < H))

    band = np.zeros(H, dtype=bool)
    half_h = max(2, notehead_h // 2)
    for cy in centers_y:
        band[max(0, cy - half_h) : min(H, cy + half_h + 1)] = True
    line_mask = np.ones(H, dtype=bool)
    for ly in line_ys:
        thick = max(1, int(round(spacing * 0.07)))
        line_mask[max(0, ly - thick) : min(H, ly + thick + 1)] = False
    inkmask = band & line_mask
    masked = binary & inkmask[:, None]

    col_density = masked.sum(axis=0).astype(np.float32)
    if notehead_w > 1:
        kernel = np.ones(notehead_w, dtype=np.float32) / notehead_w
        smooth = np.convolve(col_density, kernel, mode="same")
    else:
        smooth = col_density

    min_density = 0.55 * notehead_h
    above = smooth >= min_density
    if not above.any():
        return []

    half_w = max(2, notehead_w // 2)
    peaks: list[int] = []
    last_peak_x = -10 * notehead_w
    for x in range(W):
        if not above[x]:
            continue
        lo = max(0, x - half_w)
        hi = min(W, x + half_w + 1)
        if smooth[x] >= smooth[lo:hi].max() - 1e-6:
            if x - last_peak_x >= max(notehead_w // 2, 4):
                peaks.append(x + x0)  # back to full-image coords
                last_peak_x = x
    return peaks


def _build_pitch_zooms(
    *,
    hi_img: Image.Image,
    notehead_xs: list[int],
    y0_hi: int,
    y1_hi: int,
    staff_lines_hi: list[int],
    line_spacing_hi: float,
) -> list[PrimitiveZoom]:
    """One narrow zoom per notehead — used for step+octave questions.

    Width = ~3x notehead width (just enough to see the notehead
    clearly with a hint of the stem). The side ruler is drawn so the
    model can read the pitch off ticks. We do NOT include neighboring
    noteheads — the zoom must isolate ONE notehead so the model
    answers about THIS notehead, not its left/right neighbor.
    """
    if not notehead_xs:
        return []
    zoom_half_w = max(int(round(line_spacing_hi * 1.5)), 30)
    zooms: list[PrimitiveZoom] = []
    W, H = hi_img.size
    for idx, cx in enumerate(notehead_xs):
        x0 = max(0, cx - zoom_half_w)
        x1 = min(W, cx + zoom_half_w)
        # Vertical: full extended staff height (so the side ruler covers
        # ledgers above and below). Use the same y-range as the parent
        # measure crop.
        crop = hi_img.crop((x0, y0_hi, x1, y1_hi))
        local_lines = [s - y0_hi for s in staff_lines_hi]
        annotated = _annotate_with_side_ruler(crop, local_lines)
        zooms.append(
            PrimitiveZoom(
                note_indices=(idx,),
                png=_pil_to_png(annotated),
            )
        )
    return zooms


def _build_key_signature_zoom(
    *,
    hi_img: Image.Image,
    staff_left_hi: int,
    y0_hi: int,
    y1_hi: int,
    staff_lines_hi: list[int],
    line_spacing_hi: float,
) -> bytes | None:
    """Crop the strip immediately to the right of the clef glyph.

    Iter-26: this zoom isolates the region where any key-signature
    accidentals (sharps/flats) appear. The treble clef glyph is
    roughly 4 line-spacings wide on Verovio-rendered output; we skip
    past it. The zoom extends 8 line-spacings further right to
    comfortably capture up to 7 accidentals. The crop may also
    include the time signature and the leading edge of the first
    notehead — the prompt instructs the model to ignore those.

    The crop includes the FULL staff vertical extent + the same
    above/below ledger margins as the holistic measure crop so the
    model can use the side ruler to identify which line/space each
    accidental sits on (the canonical sharp/flat ordering uses
    specific line/space positions).

    Returns None if the geometry is degenerate.
    """
    if len(staff_lines_hi) != 5 or line_spacing_hi <= 0:
        return None
    W, _ = hi_img.size
    clef_right = staff_left_hi + int(round(line_spacing_hi * 4.0))
    zone_width = int(round(line_spacing_hi * 8))
    zoom_end = min(W, clef_right + zone_width)
    if zoom_end <= clef_right + 16:
        return None
    pad_left = int(round(line_spacing_hi * 0.2))
    pad_right = int(round(line_spacing_hi * 0.2))
    crop_x0 = max(0, clef_right - pad_left)
    crop_x1 = min(W, zoom_end + pad_right)
    if crop_x1 <= crop_x0 + 16:
        return None
    crop = hi_img.crop((crop_x0, y0_hi, crop_x1, y1_hi))
    local_lines = [s - y0_hi for s in staff_lines_hi]
    annotated = _annotate_with_side_ruler(crop, local_lines)
    return _pil_to_png(annotated)


def _build_rhythm_zooms(
    *,
    hi_img: Image.Image,
    notehead_xs: list[int],
    y0_hi: int,
    y1_hi: int,
    staff_lines_hi: list[int],
    line_spacing_hi: float,
    measure_x1: int,
) -> list[PrimitiveZoom]:
    """Group consecutive noteheads into "beam groups" by inter-notehead
    gap, then emit ONE wide zoom per group spanning the full group with
    generous padding so the beams above/below the noteheads are visible.

    Heuristic: a "group" boundary is wherever the gap between two
    consecutive noteheads is > 2.5x the median inter-notehead gap. This
    catches the gap between a beamed pair and the next quarter, while
    keeping the beamed pair together. For a measure with no beaming
    (all stand-alone notes), we still emit one zoom per note (group of
    size 1) — wider than the pitch zoom so flags are visible.

    Critical iter-14 lesson: a NARROW zoom strips beam context, so
    rhythm zooms MUST extend high enough above the noteheads to show
    flags/beams (which sit above the stem above the notehead).

    Iter-32: the two rhythm fixtures are stuck at 0.0 even with the
    iter-31 CoT prompt. Per planner hypothesis, the bottleneck is
    pixel/zoom: flag pixels and augmentation-dot pixels are too thin
    to survive Gemini's downsampling, AND the previous y-range only
    extended ~2.5 line-spacings above the staff which is too tight for
    stems-up flags/beams that can rise 4+ line-spacings above the top
    line. So we (1) compute a rhythm-zoom-specific y-range that
    extends RHYTHM_VERT_PAD_MULT line-spacings above and below the
    staff (capturing full flag/beam vertical extent), and (2) upscale
    the rhythm crop RHYTHM_UPSCALE x with LANCZOS before annotation
    so thin-pixel features (flag tips, beam edges, dot pixels)
    survive the downsampling Gemini does on large images.
    """
    if not notehead_xs:
        return []
    n = len(notehead_xs)
    # Compute inter-notehead gaps to detect beam groups.
    if n == 1:
        groups: list[list[int]] = [[0]]
    else:
        gaps = [notehead_xs[i + 1] - notehead_xs[i] for i in range(n - 1)]
        sorted_gaps = sorted(gaps)
        median_gap = sorted_gaps[len(sorted_gaps) // 2]
        small_gap_threshold = max(median_gap * 0.6, line_spacing_hi * 1.5)
        groups = []
        cur = [0]
        for i, gap in enumerate(gaps):
            if gap <= small_gap_threshold:
                cur.append(i + 1)
            else:
                groups.append(cur)
                cur = [i + 1]
        groups.append(cur)

    pad_x = max(int(round(line_spacing_hi * 1.2)), 20)
    W, H = hi_img.size

    # Iter-32: rhythm-specific y-range — extend further above/below the
    # staff than the parent measure crop to capture full flag/beam stem
    # height (stems-up flags can reach 4+ line-spacings above top line).
    top_line_hi = staff_lines_hi[0]
    bot_line_hi = staff_lines_hi[-1]
    rhythm_pad_above = int(round(line_spacing_hi * RHYTHM_VERT_PAD_MULT))
    rhythm_pad_below = int(round(line_spacing_hi * RHYTHM_VERT_PAD_MULT))
    rh_y0 = max(0, top_line_hi - rhythm_pad_above)
    rh_y1 = min(H, bot_line_hi + rhythm_pad_below)

    zooms: list[PrimitiveZoom] = []
    for grp in groups:
        first_x = notehead_xs[grp[0]]
        last_x = notehead_xs[grp[-1]]
        x0 = max(0, first_x - pad_x)
        x1 = min(W, max(last_x + pad_x, last_x + 1))
        x1 = min(x1, measure_x1)
        crop = hi_img.crop((x0, rh_y0, x1, rh_y1))
        # Iter-32: LANCZOS upscaling so thin flag/dot pixels survive
        # Gemini's input downsampling.
        if RHYTHM_UPSCALE != 1.0:
            new_w = max(1, int(round(crop.width * RHYTHM_UPSCALE)))
            new_h = max(1, int(round(crop.height * RHYTHM_UPSCALE)))
            crop = crop.resize((new_w, new_h), Image.Resampling.LANCZOS)
            local_lines = [
                int(round((s - rh_y0) * RHYTHM_UPSCALE)) for s in staff_lines_hi
            ]
        else:
            local_lines = [s - rh_y0 for s in staff_lines_hi]
        annotated = _annotate_with_side_ruler(crop, local_lines)
        zooms.append(
            PrimitiveZoom(
                note_indices=tuple(grp),
                png=_pil_to_png(annotated),
            )
        )
    return zooms


def _build_measure_rhythm_zoom(
    *,
    hi_img: Image.Image,
    crop_x0: int,
    crop_x1: int,
    staff_lines_hi: list[int],
    line_spacing_hi: float,
) -> bytes | None:
    """Iter-41: one wide rhythm crop covering the entire measure.

    The crop spans [crop_x0, crop_x1] in x and the rhythm-friendly
    y-extent (RHYTHM_VERT_PAD_MULT line-spacings above + below the
    staff) so all flag/beam pixels are visible. The result is upscaled
    by RHYTHM_UPSCALE with LANCZOS so thin features survive Gemini's
    input downsampling. The side-ruler is annotated for vertical
    reference (the rhythm primitive doesn't actually need it for
    durations, but the model can use it as a sanity check).

    The prompt that consumes this image asks for N indexed answers
    (one per candidate-pitched-note in left-to-right order), telling
    the model to ignore the clef/key/time prefix at the start of
    measure 1. Phantom detections in the assembler are no longer
    forwarded to the rhythm primitive — the primitive operates over
    the candidate's NOTE COUNT, not the detector's.
    """
    if len(staff_lines_hi) != 5 or line_spacing_hi <= 0:
        return None
    W, H = hi_img.size
    if crop_x1 <= crop_x0 + 16:
        return None
    top_line_hi = staff_lines_hi[0]
    bot_line_hi = staff_lines_hi[-1]
    rhythm_pad_above = int(round(line_spacing_hi * RHYTHM_VERT_PAD_MULT))
    rhythm_pad_below = int(round(line_spacing_hi * RHYTHM_VERT_PAD_MULT))
    rh_y0 = max(0, top_line_hi - rhythm_pad_above)
    rh_y1 = min(H, bot_line_hi + rhythm_pad_below)
    crop_x0 = max(0, crop_x0)
    crop_x1 = min(W, crop_x1)
    crop = hi_img.crop((crop_x0, rh_y0, crop_x1, rh_y1))
    if RHYTHM_UPSCALE != 1.0:
        new_w = max(1, int(round(crop.width * RHYTHM_UPSCALE)))
        new_h = max(1, int(round(crop.height * RHYTHM_UPSCALE)))
        crop = crop.resize((new_w, new_h), Image.Resampling.LANCZOS)
        local_lines = [
            int(round((s - rh_y0) * RHYTHM_UPSCALE)) for s in staff_lines_hi
        ]
    else:
        local_lines = [s - rh_y0 for s in staff_lines_hi]
    annotated = _annotate_with_side_ruler(crop, local_lines)
    return _pil_to_png(annotated)


def _detect_bounds(
    arr: np.ndarray,
) -> tuple[int, int, int, int, list[int], list[int]] | None:
    H, W = arr.shape
    binary = arr < DARK_THRESHOLD
    dark_per_row = binary.sum(axis=1)
    page_top_cutoff = int(H * PAGE_FOOTER_FRAC)
    music_rows = np.where(dark_per_row[:page_top_cutoff] > ROW_INK_MIN)[0]
    if len(music_rows) == 0:
        return None
    y0, y1 = int(music_rows[0]), int(music_rows[-1])
    if y1 <= y0:
        return None

    staff_strip = binary[y0 : y1 + 1, :]
    col_density = staff_strip.sum(axis=0)
    h = y1 - y0

    has_staff = col_density >= STAFF_LINE_DARK_COUNT
    staff_cols = np.where(has_staff)[0]
    if len(staff_cols) == 0:
        return None
    staff_right = int(staff_cols[-1])
    staff_left = _first_staff_column(staff_strip, h)
    if staff_left is None:
        return None

    barline_threshold = h * BARLINE_FILL_FRAC
    is_barline = col_density >= barline_threshold

    barlines: list[int] = []
    in_bar = False
    start = 0
    for x in range(W):
        if is_barline[x] and not in_bar:
            in_bar = True
            start = x
        elif not is_barline[x] and in_bar:
            in_bar = False
            barlines.append((start + x - 1) // 2)
    if in_bar:
        barlines.append((start + W - 1) // 2)

    barlines = [b for b in barlines if staff_left + 20 <= b <= staff_right + 20]

    staff_lines = _detect_staff_lines(binary, y0, y1, staff_left, staff_right)
    return y0, y1, staff_left, staff_right, barlines, staff_lines


def _first_staff_column(staff_strip: np.ndarray, h: int) -> int | None:
    H, W = staff_strip.shape
    min_extent = int(h * 0.6)
    for x in range(W):
        col = staff_strip[:, x]
        ys = np.where(col)[0]
        if len(ys) < STAFF_LINE_DARK_COUNT:
            continue
        if int(ys[-1] - ys[0]) >= min_extent:
            return int(x)
    return None


def _detect_staff_lines(
    binary: np.ndarray, y0: int, y1: int, staff_left: int, staff_right: int
) -> list[int]:
    if staff_right <= staff_left:
        return []
    strip = binary[y0 : y1 + 1, staff_left : staff_right + 1]
    width = strip.shape[1]
    row_density = strip.sum(axis=1)
    threshold = width * 0.5
    is_line_row = row_density >= threshold

    runs: list[tuple[int, int, int]] = []
    in_run = False
    start = 0
    for i, v in enumerate(is_line_row):
        if v and not in_run:
            in_run = True
            start = i
        elif not v and in_run:
            in_run = False
            ink = int(row_density[start:i].sum())
            runs.append((start, i - 1, ink))
    if in_run:
        ink = int(row_density[start : len(is_line_row)].sum())
        runs.append((start, len(is_line_row) - 1, ink))

    if len(runs) < 5:
        return []
    strongest = sorted(runs, key=lambda r: r[2], reverse=True)[:5]
    centers = sorted((r[0] + r[1]) // 2 + y0 for r in strongest)
    gaps = [centers[i + 1] - centers[i] for i in range(4)]
    if not gaps or max(gaps) > 3 * min(gaps):
        return []
    return centers


def _annotate_with_side_ruler(
    crop: Image.Image, line_ys: list[int]
) -> Image.Image:
    if len(line_ys) != 5:
        return crop

    crop_w, crop_h = crop.size
    gutter = GUTTER_PX_HI
    new_w = crop_w + 2 * gutter
    new_h = crop_h
    canvas = Image.new("RGB", (new_w, new_h), (255, 255, 255))
    canvas.paste(crop, (gutter, 0))

    draw = ImageDraw.Draw(canvas, "RGBA")
    font = _load_font(size=36)

    line_ys_sorted = sorted(line_ys)
    spacing = (line_ys_sorted[-1] - line_ys_sorted[0]) / 4.0
    space_ys = [
        int(round((line_ys_sorted[i] + line_ys_sorted[i + 1]) / 2)) for i in range(4)
    ]

    above_targets: list[tuple[int, str, bool, bool]] = []
    for offset, label in TREBLE_ABOVE_ANCHORS:
        y = int(round(line_ys_sorted[0] - offset * spacing))
        if 0 <= y < crop_h:
            is_ledger = offset in LEDGER_ANCHOR_OFFSETS
            above_targets.append((y, label, is_ledger, not is_ledger))
    below_targets: list[tuple[int, str, bool, bool]] = []
    for offset, label in TREBLE_BELOW_ANCHORS:
        y = int(round(line_ys_sorted[-1] + offset * spacing))
        if 0 <= y < crop_h:
            is_ledger = offset in LEDGER_ANCHOR_OFFSETS
            below_targets.append((y, label, is_ledger, not is_ledger))

    music_x0 = gutter
    music_x1 = gutter + crop_w - 1

    def draw_anchor(
        y: int,
        label: str,
        tick_color: tuple[int, int, int],
        tick_len: int,
        tick_width: int,
        bold: bool,
    ) -> None:
        left_tick_x0 = music_x0 - tick_len
        left_tick_x1 = music_x0 - 1
        draw.line(
            [(left_tick_x0, y), (left_tick_x1, y)],
            fill=tick_color + (255,),
            width=tick_width,
        )
        right_tick_x0 = music_x1 + 1
        right_tick_x1 = music_x1 + tick_len
        draw.line(
            [(right_tick_x0, y), (right_tick_x1, y)],
            fill=tick_color + (255,),
            width=tick_width,
        )
        bbox = draw.textbbox((0, 0), label, font=font)
        tw = bbox[2] - bbox[0]
        th = bbox[3] - bbox[1]
        label_color = (0, 0, 0) if bold else (90, 90, 90)
        lx = left_tick_x0 - tw - 8
        if lx >= 4:
            draw.text((lx, y - th // 2), label, fill=label_color + (255,), font=font)
        rx = right_tick_x1 + 8
        if rx + tw <= new_w - 4:
            draw.text((rx, y - th // 2), label, fill=label_color + (255,), font=font)

    for ly, label in zip(line_ys_sorted, TREBLE_LINE_PITCHES):
        draw_anchor(
            ly,
            label,
            tick_color=LINE_TICK_COLOR,
            tick_len=TICK_LEN_PX,
            tick_width=4,
            bold=True,
        )
    for sy, label in zip(space_ys, TREBLE_SPACE_PITCHES):
        draw_anchor(
            sy,
            label,
            tick_color=SPACE_TICK_COLOR,
            tick_len=int(TICK_LEN_PX * 0.7),
            tick_width=2,
            bold=False,
        )
    for y, label, is_ledger, _is_space in above_targets + below_targets:
        if is_ledger:
            draw_anchor(
                y,
                label,
                tick_color=LEDGER_TICK_COLOR,
                tick_len=TICK_LEN_LEDGER_PX,
                tick_width=4,
                bold=True,
            )
        else:
            draw_anchor(
                y,
                label,
                tick_color=SPACE_TICK_COLOR,
                tick_len=int(TICK_LEN_PX * 0.7),
                tick_width=2,
                bold=False,
            )

    return canvas


def _load_font(size: int = 36):
    candidates = [
        "/System/Library/Fonts/Helvetica.ttc",
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _pixmap_to_gray(pix) -> np.ndarray:
    img = Image.open(io.BytesIO(pix.tobytes("png"))).convert("L")
    return np.array(img)


def _pil_to_png(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=False)
    return buf.getvalue()
