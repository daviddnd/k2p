"""Scanned-filing reader: register the page, verify the form, read the boxes
(scope §2.1, §7; contract §2.7, §5.1.2).

A scanned filing is a photocopy of the pinned template, so reading one is
three steps that must all succeed before a value is proposed:

1. **Register the page.** A skew estimate over the long rules the pinned
   template prints gives the small rotation; the scan is then measured
   *straightened*, which gives the frame, and therefore translation and
   scale; ``registration`` turns the three into one pure transform, and
   field rectangles are converted through it. Measuring before
   straightening is not an option — the corners of a form are not inked,
   so the bounding box of a rotated page reads a percent or two large,
   which is most of a field's width by the far edge.
2. **Verify the form.** The region where the template prints its
   specification number is OCR'd and compared with the registry's pinned
   one. A different specification, or a region yielding no number at all,
   is refused: fields are never read off a page that is not this form.
3. **Read each box.** The field rectangle is cropped (and rotated
   straight), OCR'd with the local engine, and scored from the engine's
   per-word confidence capped by validation
   (``app.domain.prior_filing.value_is_validated``). A checkbox is read by
   ink presence rather than glyph recognition — an OCR'd tick is not a
   reliable glyph — and a mark is capped into staff review.

Refusal is a first-class outcome, carried as a ``ReadSignal``: no frame,
an unreadable or unsupported specification number, no engine, a box that
yields nothing. Nothing here guesses, and nothing here writes — the
adapter reads bytes and returns reads; mapping them onto master-data
targets is the extractor's job (``app.adapters.pdf.extractor``).
"""

import math
import os
import re
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from functools import cache
from itertools import pairwise
from typing import Any, cast

import pdfplumber
import pypdfium2 as pdfium
import pytesseract
from PIL import Image

from app.adapters.forms.registry import FormRegistry, WidgetGeometry, template_path
from app.domain.prior_filing import (
    CONFIDENCE_OCR_CHECKMARK,
    MIN_OCR_WORD_CONFIDENCE,
    FormCode,
    ReadOrigin,
    ReadSignal,
    individual_paths,
    is_supported_target,
)

# One render resolution for every page: high enough for the smallest boxes
# on the form, and the same for the pinned template and the scan so their
# frames are measured alike.
RENDER_DPI = 300

# The §2.7 ``model`` value: the engine, then the prompt-free pipeline that
# decides what is cropped and how a read is scored.
OCR_PIPELINE = "ocr-fields/v1"

# Traditional Chinese is half of every printed form entry, so the engine
# runs both scripts; ``eng`` alone would read the Chinese boxes as noise.
OCR_LANGUAGE = "eng+chi_tra"
# Boxes are read as a block of text first; a bare number in an otherwise
# empty box is what the block mode drops, so sparse mode is the fallback.
OCR_CONFIG = "--psm 6"
OCR_SPARSE_CONFIG = "--psm 11"
# The specification line is one short line of print.
OCR_SPEC_CONFIG = "--psm 7"

# A page is placed by its frame; more skew than this is not a scan of a
# form but a bad photograph, and is refused rather than followed.
MAX_DESKEW_DEGREES = 3.0
DESKEW_COARSE_STEP = 0.5
DESKEW_FINE_STEP = 0.1
# Below this the estimate is noise; rotating a crop by it would blur more
# than it straightens.
MIN_DESKEW_DEGREES = 0.05
# The slope search runs on tangents (rows of travel per column), so the
# degree steps above convert once, here.
_MAX_SLOPE = math.tan(math.radians(MAX_DESKEW_DEGREES))
_COARSE_SLOPE = math.tan(math.radians(DESKEW_COARSE_STEP))
_FINE_SLOPE = math.tan(math.radians(DESKEW_FINE_STEP))

# How far the scan's frame may differ from the template's, per dimension,
# before the page is judged not to be this form. Both are measured in
# points, so this is a physical size check: a scan that lost an edge, a
# photocopy produced at a copier's "fit to page" zoom, and a page of
# another form are all caught here rather than read through a wrong
# transform.
FRAME_TOLERANCE = 0.03
# A row or column counts as content when it carries this fraction of the
# page's pixels, which ignores scanner dust and punch holes alike.
CONTENT_INK_FRACTION = 0.01
INK_THRESHOLD = 140

# Slack around a field rectangle, in points: glyphs are drawn inside the
# box but scan bleed and pixel rounding are not, so the crop is a little
# larger than the registry's rectangle (larger still when the page is
# skewed, where the crop must also hold the rotated box).
CROP_PAD_POINTS = 1.5
# Ink is looked for inside this much of the field rectangle, so the printed
# box's own border is not mistaken for a value.
FIELD_INSET_PIXELS = 3
# The printed box's own border is cut away before OCR (Tesseract's layout
# analysis trips over it, and at low resolution it survives as a "|" word)
# and the interior is floated on a white margin. The inset is in points —
# a printed rule is a fraction of one — with a couple of pixels of slack.
BORDER_INSET_POINTS = 1.2
BORDER_INSET_SLACK_PIXELS = 2
OCR_MARGIN_PIXELS = 20
# The share of a checkbox that a mark must cover before the box counts as
# ticked: a tick covers about a fifth of the box, an empty one under 2%
# (its own border, where the template draws one at all).
CHECKBOX_MARK_RATIO = 0.08
# A gap narrower than this share of the line's average character width is
# letter spacing inside a word, not a space between two.
JOIN_GAP_RATIO = 0.45

_SPEC_TOKEN = re.compile(r"(\d{1,2})\s*/\s*(\d{4})")


class ScanUnreadableError(ValueError):
    """The bytes are not a PDF the rasterizer can open."""


@dataclass(frozen=True)
class Bounds:
    """An axis-aligned rectangle in one space's own pixels (origin top-left)."""

    x0: float
    y0: float
    x1: float
    y1: float

    @property
    def width(self) -> float:
        return self.x1 - self.x0

    @property
    def height(self) -> float:
        return self.y1 - self.y0

    @property
    def centre(self) -> tuple[float, float]:
        return ((self.x0 + self.x1) / 2, (self.y0 + self.y1) / 2)

    def grow(self, margin: float) -> "Bounds":
        return Bounds(self.x0 - margin, self.y0 - margin, self.x1 + margin, self.y1 + margin)

    def clamp(self, size: tuple[int, int]) -> "Bounds":
        """The same rectangle pulled inside an image of ``size``."""
        return Bounds(
            max(0.0, min(self.x0, size[0])),
            max(0.0, min(self.y0, size[1])),
            max(0.0, min(self.x1, size[0])),
            max(0.0, min(self.y1, size[1])),
        )


def enclosing(points: Iterable[tuple[float, float]]) -> Bounds:
    """The smallest axis-aligned rectangle containing every point."""
    xs, ys = zip(*points, strict=True)
    return Bounds(min(xs), min(ys), max(xs), max(ys))


def _rotate(
    point: tuple[float, float], centre: tuple[float, float], degrees: float
) -> tuple[float, float]:
    """Rotate a point counter-clockwise on screen — PIL's own convention."""
    radians = math.radians(degrees)
    dx, dy = point[0] - centre[0], point[1] - centre[1]
    return (
        centre[0] + dx * math.cos(radians) + dy * math.sin(radians),
        centre[1] - dx * math.sin(radians) + dy * math.cos(radians),
    )


@dataclass(frozen=True)
class Registration:
    """The transform from pinned-template points onto a scan's pixels.

    ``template`` is the form's frame in points (y down); ``scan`` is the
    same frame measured on the page *straightened* by ``deskew``, in the
    scan's pixels. The straightened page shares its canvas with the raw
    one, so a point moves between the two by rotating about the canvas
    centre — ``deskew`` in PIL's convention, positive counter-clockwise.

    Field rectangles are placed in the straightened space (where a skewed
    box is square again) and only then rotated onto the raw image, which is
    what the crops are cut from.
    """

    template: Bounds
    scan: Bounds
    deskew: float
    image_size: tuple[int, int]

    @property
    def scale(self) -> float:
        """Scan pixels per template point (the two axes averaged)."""
        return (
            self.scan.width / self.template.width + self.scan.height / self.template.height
        ) / 2

    def straight_rect(self, rect: tuple[float, float, float, float]) -> Bounds:
        """A template rectangle as an axis-aligned rectangle of the page.

        The axes keep their own scale, so a scan that stretched one way
        still lands its boxes on their printings.
        """
        x0, y0, x1, y1 = rect
        left = self.scan.x0 + (x0 - self.template.x0) / self.template.width * self.scan.width
        top = self.scan.y0 + (y0 - self.template.y0) / self.template.height * self.scan.height
        right = self.scan.x0 + (x1 - self.template.x0) / self.template.width * self.scan.width
        bottom = self.scan.y0 + (y1 - self.template.y0) / self.template.height * self.scan.height
        return Bounds(left, top, right, bottom)

    def to_raw(self, point: tuple[float, float]) -> tuple[float, float]:
        """A point of the straightened page, as a pixel of the raw scan."""
        centre = (self.image_size[0] / 2, self.image_size[1] / 2)
        return _rotate(point, centre, -self.deskew)

    def map_point(self, x: float, y: float) -> tuple[float, float]:
        """One template point as a pixel in the raw scan image."""
        return self.to_raw(self.straight_rect((x, y, x, y)).centre)

    def map_rect(
        self, rect: tuple[float, float, float, float]
    ) -> tuple[tuple[float, float], ...]:
        """A template rectangle's four corners, in the raw scan image."""
        straight = self.straight_rect(rect)
        return tuple(
            self.to_raw(corner)
            for corner in (
                (straight.x0, straight.y0),
                (straight.x1, straight.y0),
                (straight.x1, straight.y1),
                (straight.x0, straight.y1),
            )
        )


def registration(
    *,
    template: Bounds,
    scan: Bounds,
    deskew: float,
    image_size: tuple[int, int],
) -> Registration:
    """The transform for a page whose frame measured as ``scan``.

    ``scan`` must be measured on the page as it sits once ``deskew`` has
    straightened it (``straight_frame``), never as the raw bounding box of
    a rotated page: the corners of a form are not inked, so a bounding box
    solves to a frame a percent or two small, which is a field's width by
    the far edge of the page.
    """
    return Registration(template=template, scan=scan, deskew=deskew, image_size=image_size)


# --- Measuring a page ---


def _mask(image: Image.Image) -> Image.Image:
    """The page's ink as 0/255 (a form is printed and scanned dark on light)."""
    return image.convert("L").point(lambda level: 255 if level < INK_THRESHOLD else 0)


PROFILE_WIDTH = 700


def _reduced(image: Image.Image) -> Image.Image:
    """A page's ink as a mask small enough to measure a frame on.

    Counting over the full raster costs more than the rest of a read put
    together, and a frame only has to be found to within a point — so a
    block counts as ink when it is at least half ink, which keeps dust out
    at either resolution.
    """
    mask = _mask(image)
    factor = min(1.0, PROFILE_WIDTH / max(mask.size))
    if factor >= 1.0:
        return mask
    return mask.resize(
        (max(1, round(mask.size[0] * factor)), max(1, round(mask.size[1] * factor))),
        Image.Resampling.BOX,
    ).point(lambda level: 255 if level > 127 else 0)


def _profiles(mask: Image.Image) -> tuple[list[int], list[int]]:
    """Ink counts per row and column of a mask (ink is 255)."""
    width, height = mask.size
    pixels = mask.tobytes()
    rows = [pixels[y * width : (y + 1) * width].count(255) for y in range(height)]
    columns = [pixels[x::width].count(255) for x in range(width)]
    return rows, columns


def _ink_bounds(mask: Image.Image) -> Bounds | None:
    """The bounding box of a mask's ink, or ``None`` for a blank one.

    A row or column counts only when it carries a fraction of the mask's
    pixels, so a speck of dust or a punch hole at the paper's edge does not
    drag a frame outwards.
    """
    rows, columns = _profiles(mask)
    inked_rows = [
        y for y, count in enumerate(rows) if count >= max(2, CONTENT_INK_FRACTION * len(columns))
    ]
    inked_columns = [
        x for x, count in enumerate(columns) if count >= max(2, CONTENT_INK_FRACTION * len(rows))
    ]
    if not inked_rows or not inked_columns:
        return None
    return Bounds(
        inked_columns[0], inked_rows[0], inked_columns[-1] + 1, inked_rows[-1] + 1
    )


def content_bounds(image: Image.Image) -> Bounds | None:
    """The bounding box of a page's ink, or ``None`` for a blank page.

    A page whose ink is too thin to give a frame has no frame to register
    against at all.
    """
    mask = _reduced(image)
    bounds = _ink_bounds(mask)
    if bounds is None:
        return None
    scale = mask.size[0] / image.size[0]
    return Bounds(
        bounds.x0 / scale, bounds.y0 / scale, bounds.x1 / scale, bounds.y1 / scale
    )


def straight_frame(image: Image.Image, deskew: float) -> Bounds | None:
    """A page's content frame, measured with its skew undone.

    The page is straightened on a reduced copy — the frame only has to be
    found to within a point, and rotating the full raster would cost more
    than everything else in a read — and the frame is scaled back into the
    scan's own pixels, where it shares a canvas with the raw image.
    """
    small = _reduced(image)
    if abs(deskew) >= MIN_DESKEW_DEGREES:
        # The corners a rotation leaves behind are paper, and paper is 0 in
        # a mask whose ink is 255.
        small = small.rotate(deskew, resample=Image.Resampling.NEAREST, fillcolor=0)
    bounds = _ink_bounds(small)
    if bounds is None:
        return None
    factor = image.size[0] / small.size[0]
    return Bounds(
        bounds.x0 * factor, bounds.y0 * factor, bounds.x1 * factor, bounds.y1 * factor
    )


def frame_matches(template: Bounds, scan: Bounds) -> bool:
    """Whether a scanned frame is the template's, within scan tolerance.

    Both rectangles must be in the same units (points), and both
    dimensions are compared, not just the aspect ratio.
    """
    return all(
        abs(measured / expected - 1) <= FRAME_TOLERANCE
        for measured, expected in (
            (scan.width, template.width),
            (scan.height, template.height),
        )
    )


def template_rules(image: Image.Image) -> tuple[Bounds, ...]:
    """The long printed rules a page of the template carries.

    These are what a skew estimate can follow: a rule spans most of the
    page and is unique in its rows, where text lines repeat and cannot be
    told from their neighbours once the page has been rotated.
    """
    mask = _reduced(image)
    rows, _ = _profiles(mask)
    scale = mask.size[0] / image.size[0]
    width = mask.size[0]
    strongest = max(rows, default=0)
    if strongest < 0.4 * width:
        return ()
    pixels = mask.tobytes()
    bounds = []
    for group in _runs([y for y, count in enumerate(rows) if count >= 0.6 * strongest]):
        middle = group[len(group) // 2]
        inked = [x for x in range(width) if pixels[middle * width + x]]
        if inked:
            bounds.append(
                Bounds(
                    inked[0] / scale,
                    group[0] / scale,
                    (inked[-1] + 1) / scale,
                    (group[-1] + 1) / scale,
                )
            )
    return tuple(bounds)


def _runs(rows: Sequence[int]) -> list[list[int]]:
    """Group consecutive row numbers (a printed rule is a few rows thick)."""
    groups: list[list[int]] = []
    for row in rows:
        if groups and row - groups[-1][-1] <= 2:
            groups[-1].append(row)
        else:
            groups.append([row])
    return groups


def rule_bands(*, rules: Sequence[Bounds], template: Bounds, scan: Bounds) -> tuple[Bounds, ...]:
    """The template's rules as search bands in the scan's own pixels.

    The bands come from the frame's plain translation and scale (the skew
    is still unknown here), widened enough to hold the rule however the
    page arrived rotated and to catch a neighbouring rule — which tilts
    the same way, so one serves as well as the other.
    """
    scale_x = scan.width / template.width
    scale_y = scan.height / template.height
    slack = 0.05 * scan.height
    return tuple(
        Bounds(
            scan.x0 + (rule.x0 - template.x0) * scale_x,
            scan.y0 + (rule.y0 - template.y0) * scale_y - slack,
            scan.x0 + (rule.x1 - template.x0) * scale_x,
            scan.y0 + (rule.y0 - template.y0) * scale_y + slack,
        )
        for rule in rules
    )


def deskew_angle(image: Image.Image, *, bands: Sequence[Bounds]) -> float:
    """The rotation that straightens the page, in degrees (PIL convention).

    A Hough-style vote: a printed rule is a straight line, so the ink of
    its band lines up along ``row = intercept + slope · column`` for
    exactly one slope. Each band's columns vote, per slope, for the
    intercept they imply; the slope with the sharpest line wins, signed so
    a page rotated one way is corrected the other. With no rule to follow
    the answer is no correction, never a guess.
    """
    mask = _mask(image)
    width, height = mask.size
    pixels = mask.tobytes()
    for band in bands:
        columns = _band_columns(pixels, width, height, band)
        if len(columns) < 20:
            continue
        coarse = _best_slope(columns, low=-_MAX_SLOPE, high=_MAX_SLOPE, step=_COARSE_SLOPE)
        if coarse is None:
            continue
        slope = _best_slope(
            columns, low=coarse - _COARSE_SLOPE, high=coarse + _COARSE_SLOPE, step=_FINE_SLOPE
        )
        if slope is None:
            continue
        return math.degrees(math.atan(slope))
    return 0.0


def _band_columns(
    pixels: bytes, width: int, height: int, band: Bounds
) -> list[tuple[int, list[int]]]:
    """Each sampled column of a band, with the centres of its ink runs."""
    x_start, x_end = max(0, int(band.x0)), min(width, int(band.x1))
    y_start, y_end = max(0, int(band.y0)), min(height, int(band.y1))
    if x_end - x_start < 40 or y_end - y_start < 2:
        return []
    step = max(1, (x_end - x_start) // 160)
    columns = []
    for x in range(x_start, x_end, step):
        rows = [y for y in range(y_start, y_end) if pixels[y * width + x]]
        if rows:
            columns.append((x, [run[len(run) // 2] for run in _runs(rows)]))
    return columns


def _best_slope(
    columns: Sequence[tuple[int, list[int]]], *, low: float, high: float, step: float
) -> float | None:
    """The slope the band's ink columns agree on most (ties go to the smaller)."""
    steps = max(1, round((high - low) / step))
    best_key: tuple[int, float] | None = None
    best_slope: float | None = None
    for index in range(steps + 1):
        slope = low + index * (high - low) / steps
        votes: Counter[int] = Counter()
        for x, rows in columns:
            offset = round(x * slope)
            votes.update(row - offset for row in rows)
        if not votes:
            continue
        key = (max(votes.values()), -abs(slope))
        if best_key is None or key > best_key:
            best_key, best_slope = key, slope
    return best_slope


# --- The pinned template's own measurements ---


@dataclass(frozen=True)
class TemplatePage:
    """One page of the pinned template, as the reader measures it.

    Rectangles are in template points with y down, the same space a field
    rectangle is converted into, so no DPI travels with them.
    """

    page: int
    frame: Bounds
    rules: tuple[Bounds, ...]
    height: float


@cache
def _template_page(form: FormCode, page_number: int) -> TemplatePage | None:
    """One page of the pinned template, measured once per process.

    Rendered on demand: a scan is refused on the first page whose frame
    does not match, and there is no reason to measure pages the reader
    never reaches.
    """
    document = pdfium.PdfDocument(str(template_path(form)))
    try:
        if page_number > len(document):
            return None
        page = document[page_number - 1]
        width_points, height_points = page.get_size()
        image = _render(page)
    finally:
        document.close()
    if image is None:
        return None
    frame = content_bounds(image)
    if frame is None:
        return None
    scale = image.size[0] / width_points
    return TemplatePage(
        page=page_number,
        frame=Bounds(frame.x0 / scale, frame.y0 / scale, frame.x1 / scale, frame.y1 / scale),
        rules=tuple(
            Bounds(rule.x0 / scale, rule.y0 / scale, rule.x1 / scale, rule.y1 / scale)
            for rule in template_rules(image)
        ),
        height=height_points,
    )


@cache
def spec_region(form: FormCode) -> tuple[int, Bounds] | None:
    """Where the pinned template prints its specification number.

    Located in the template's own text, so a registry refresh moves it
    without touching this module; ``None`` means the pinned template does
    not print one, and nothing is read from the scan at all. The digital
    shapes share the region (``app.adapters.pdf.extractor`` reads it from
    the page's text layer instead of from a raster).
    """
    with pdfplumber.open(template_path(form)) as pdf:
        for number, page in enumerate(pdf.pages, start=1):
            words = [word for word in page.extract_words() if _SPEC_TOKEN.fullmatch(word["text"])]
            if words:
                region = Bounds(
                    min(word["x0"] for word in words),
                    min(word["top"] for word in words),
                    max(word["x1"] for word in words),
                    max(word["bottom"] for word in words),
                )
                return number, region.grow(CROP_PAD_POINTS * 2)
    return None


def spec_tokens(text: str) -> set[str]:
    """The specification numbers a stretch of text prints (``2/2025``).

    The one token rule both readers share: the scan reader over the words
    it recognises in the region, the digital reader over the page's own
    text layer.
    """
    return {f"{day}/{year}" for day, year in _SPEC_TOKEN.findall(text)}


def expected_spec_token(specification_no: str) -> str | None:
    """The registry's specification number as the form prints it (``2/2025``)."""
    match = _SPEC_TOKEN.search(specification_no)
    return None if match is None else f"{match.group(1)}/{match.group(2)}"


# --- The reading itself ---


@dataclass(frozen=True)
class FieldRead:
    """One widget as the scan reader saw it, or did not see it.

    ``value`` is ``None`` when the box yielded nothing readable — a blank
    box, an empty crop, a low-confidence blob — and the read's ``signals``
    then say so. ``word_confidences`` are the engine's per-word scores
    (0-1) for a text read, which the extractor turns into the field's
    score together with whether the value validates.
    """

    widget: str
    page: int
    value: str | None
    origin: ReadOrigin
    signals: tuple[ReadSignal, ...] = ()
    word_confidences: tuple[float, ...] | None = None
    confidence: float | None = None


@dataclass(frozen=True)
class ScanReading:
    """One pass over a scanned filing.

    ``reads`` is empty exactly when the scan was refused, and ``signals``
    then carries the refusal (it always names the scan shape, which is
    what the batch records).
    """

    reads: tuple[FieldRead, ...]
    signals: tuple[ReadSignal, ...]
    model: str


@dataclass(frozen=True)
class _Word:
    """One recognised word inside a crop, with where it sits in the crop."""

    text: str
    confidence: float
    bounds: Bounds


@cache
def _engine_version() -> str | None:
    """The installed Tesseract version, or ``None`` when there is none."""
    try:
        return str(pytesseract.get_tesseract_version())
    except pytesseract.TesseractNotFoundError:
        return None


def model_id() -> str:
    """The §2.7 ``model`` for this path: engine version plus pipeline."""
    return f"tesseract/{_engine_version() or 'missing'}+{OCR_PIPELINE}"


def read_scanned(data: bytes, *, registry: FormRegistry) -> ScanReading:
    """Read one scanned filing into per-widget reads, refusing rather than guessing.

    Refusals: bytes the rasterizer cannot open (``ScanUnreadableError``),
    a page whose printed frame does not match the registry's template, a
    specification number that is not the pinned one (or cannot be read at
    all), and a deployment without the OCR engine. A refusal reads
    nothing — a page the reader cannot place is not a page it will read
    fields off.
    """
    wanted = _readable_widgets(registry)
    spec = spec_region(registry.form)
    spec_page = None if spec is None else spec[0]
    try:
        document = pdfium.PdfDocument(data)
    except Exception as exc:  # pdfium reports failures as its own error family
        raise ScanUnreadableError(f"could not rasterize the PDF: {exc}") from None

    reads: list[FieldRead] = []
    try:
        for page_number in sorted(set(wanted) | ({spec_page} if spec_page else set())):
            if page_number > len(document):
                continue
            page_template = _template_page(registry.form, page_number)
            if page_template is None:
                continue
            source = document[page_number - 1]
            image = _render(source)
            if image is None:
                return _refusal(ReadSignal.FORM_FRAME_NOT_FOUND)
            frame = content_bounds(image)
            if frame is None:
                return _refusal(ReadSignal.FORM_FRAME_NOT_FOUND)
            angle = deskew_angle(
                image,
                bands=rule_bands(
                    rules=page_template.rules, template=page_template.frame, scan=frame
                ),
            )
            straight = straight_frame(image, angle)
            if straight is None:
                return _refusal(ReadSignal.FORM_FRAME_NOT_FOUND)
            transform = registration(
                template=page_template.frame,
                scan=straight,
                deskew=angle,
                image_size=image.size,
            )
            # The registered frame, in points: what the page physically is,
            # once its own skew has been taken out of the measurement.
            measured = _in_points(straight, image.size[0] / source.get_size()[0])
            if not frame_matches(page_template.frame, measured):
                return _refusal(ReadSignal.FORM_FRAME_NOT_FOUND)
            if _engine_version() is None:
                return _refusal(ReadSignal.OCR_ENGINE_UNAVAILABLE)
            if spec is not None and page_number == spec[0]:
                refusal = _verify_spec(image, transform, spec[1], registry.specification_no)
                if refusal is not None:
                    return _refusal(refusal)
            for widget in wanted.get(page_number, ()):
                reads.append(_read_widget(image, transform, page_template, widget))
    finally:
        document.close()
    return ScanReading(reads=tuple(reads), signals=(ReadSignal.NEEDS_OCR,), model=model_id())


def _in_points(bounds: Bounds, pixels_per_point: float) -> Bounds:
    """A rectangle measured in a scan's pixels, as the same rectangle in points."""
    return Bounds(
        bounds.x0 / pixels_per_point,
        bounds.y0 / pixels_per_point,
        bounds.x1 / pixels_per_point,
        bounds.y1 / pixels_per_point,
    )


def _refusal(signal: ReadSignal) -> ScanReading:
    """A refused scan: no reads, the shape, and the reason."""
    return ScanReading(
        reads=(), signals=(ReadSignal.NEEDS_OCR, signal), model=model_id()
    )


def _render(page: pdfium.PdfPage) -> Image.Image | None:
    """One page at the reader's resolution, as a greyscale image."""
    try:
        bitmap = page.render(scale=RENDER_DPI / 72)
    except pdfium.PdfiumError:
        return None
    image: Image.Image = bitmap.to_pil().convert("L")
    bitmap.close()
    return image


def _readable_widgets(registry: FormRegistry) -> dict[int, tuple[WidgetGeometry, ...]]:
    """The widgets of the items this product proposes, grouped by page.

    Items whose targets are composite, collection-valued, or outside the
    extraction vocabulary are not read at all: their reads could never
    become a record, so reading them would only cost a scan's time.
    """
    names = {
        name
        for item in registry.items
        if (paths := individual_paths(item.target)) and all(map(is_supported_target, paths))
        for name in item.pdf_fields
    }
    pages: dict[int, list[WidgetGeometry]] = {}
    for name in sorted(names):
        widget = registry.geometry[name]
        pages.setdefault(widget.page, []).append(widget)
    return {page: tuple(widgets) for page, widgets in pages.items()}


def _verify_spec(
    image: Image.Image, transform: Registration, region: Bounds, specification_no: str
) -> ReadSignal | None:
    """Whether the scan prints the registry's specification number.

    Anything else — another specification, or a region the engine reads no
    number out of — is a refusal, because geometry without the right
    specification is a transform onto a form this product does not know.
    """
    crop, _ = _crop(image, transform, region)
    words = _ocr_words(crop, config=OCR_SPEC_CONFIG)
    tokens = spec_tokens(_join(words))
    if not tokens:
        return ReadSignal.SPEC_REGION_UNREADABLE
    expected = expected_spec_token(specification_no)
    return None if expected is not None and expected in tokens else ReadSignal.UNSUPPORTED_SPEC


def _read_widget(
    image: Image.Image, transform: Registration, page: TemplatePage, widget: WidgetGeometry
) -> FieldRead:
    """One field box: a tick, or its text, or an explicit empty read."""
    rect = Bounds(*_to_image_space(widget.rect, page.height))
    if widget.widget_type == "/Btn":
        if _checkbox_marked(image, transform, rect):
            return FieldRead(
                widget=widget.name,
                page=widget.page,
                value="on",
                origin=ReadOrigin.OCR_CHECKBOX_MARK,
                confidence=CONFIDENCE_OCR_CHECKMARK,
            )
        return FieldRead(
            widget=widget.name,
            page=widget.page,
            value=None,
            origin=ReadOrigin.OCR_CHECKBOX_MARK,
            signals=(ReadSignal.OCR_FIELD_UNREADABLE,),
        )
    crop, field = _crop(image, transform, rect)
    if not _has_ink(crop, field):
        return FieldRead(
            widget=widget.name,
            page=widget.page,
            value=None,
            origin=ReadOrigin.OCR_TEXT_IN_FIELD_RECT,
            signals=(ReadSignal.OCR_FIELD_UNREADABLE,),
        )
    words = _read_words(crop, field, scale=transform.scale)
    if not words:
        return FieldRead(
            widget=widget.name,
            page=widget.page,
            value=None,
            origin=ReadOrigin.OCR_TEXT_IN_FIELD_RECT,
            signals=(ReadSignal.OCR_FIELD_UNREADABLE,),
        )
    return FieldRead(
        widget=widget.name,
        page=widget.page,
        value=_join(words),
        origin=ReadOrigin.OCR_TEXT_IN_FIELD_RECT,
        word_confidences=tuple(word.confidence for word in words),
    )


def _read_words(crop: Image.Image, field: Bounds, *, scale: float) -> list[_Word]:
    """The words inside one box, reading it twice at most.

    The engine sees the box's interior without its printed border, on a
    white margin; the block mode reads what is written there, and sparse
    mode — paid for only when the block mode found nothing confident — is
    what catches a bare number in an otherwise empty box.
    """
    prepared, box = _interior(crop, field, scale=scale)
    words = _words_inside(_ocr_words(prepared, config=OCR_CONFIG), box)
    if words:
        return words
    return _words_inside(_ocr_words(prepared, config=OCR_SPARSE_CONFIG), box)


def _interior(
    crop: Image.Image, field: Bounds, *, scale: float
) -> tuple[Image.Image, Bounds]:
    """The inside of a box, floated on a white margin, with its position.

    The returned rectangle is where the field's own box sits in the new
    image, so words can still be told apart from anything the margin holds.
    """
    inset = BORDER_INSET_POINTS * scale + BORDER_INSET_SLACK_PIXELS
    box = Bounds(
        field.x0 + inset, field.y0 + inset, field.x1 - inset, field.y1 - inset
    ).clamp(crop.size)
    left, top = int(box.x0), int(box.y0)
    inner = crop.crop((left, top, math.ceil(box.x1), math.ceil(box.y1)))
    canvas = Image.new(
        "L",
        (inner.width + 2 * OCR_MARGIN_PIXELS, inner.height + 2 * OCR_MARGIN_PIXELS),
        255,
    )
    canvas.paste(inner, (OCR_MARGIN_PIXELS, OCR_MARGIN_PIXELS))
    shift = (OCR_MARGIN_PIXELS - left, OCR_MARGIN_PIXELS - top)
    return canvas, Bounds(
        field.x0 + shift[0], field.y0 + shift[1], field.x1 + shift[0], field.y1 + shift[1]
    )


def _to_image_space(
    rect: tuple[float, float, float, float], page_height: float
) -> tuple[float, float, float, float]:
    """A registry rectangle (PDF points, y up) in points measured from the top."""
    x0, y0, x1, y1 = rect
    return (x0, page_height - y1, x1, page_height - y0)


def _crop(image: Image.Image, transform: Registration, rect: Bounds) -> tuple[Image.Image, Bounds]:
    """The field's box cut out of the scan, rotated straight.

    Returns the crop and where the box sits in it, so words can be told
    from whatever the padding and the neighbouring printings hold. The
    window is placed on the straightened page (where the box is square) and
    carried back to the raw image, so what is cut is the box's own pixels
    however the page arrived skewed; the crop is then turned about itself
    and the box's place in it moves by exactly the difference between
    turning about the crop and turning about the page.
    """
    field = transform.straight_rect((rect.x0, rect.y0, rect.x1, rect.y1))
    window = field.grow(CROP_PAD_POINTS * transform.scale).clamp(image.size)
    corners = [
        transform.to_raw(corner)
        for corner in (
            (window.x0, window.y0),
            (window.x1, window.y0),
            (window.x1, window.y1),
            (window.x0, window.y1),
        )
    ]
    raw = enclosing(corners).grow(1).clamp(image.size)
    crop = image.crop((int(raw.x0), int(raw.y0), math.ceil(raw.x1), math.ceil(raw.y1)))
    if abs(transform.deskew) < MIN_DESKEW_DEGREES or not crop.width or not crop.height:
        return crop, Bounds(
            field.x0 - raw.x0, field.y0 - raw.y0, field.x1 - raw.x0, field.y1 - raw.y0
        )
    crop = crop.rotate(transform.deskew, resample=Image.Resampling.BILINEAR, fillcolor=255)
    page = (image.size[0] / 2, image.size[1] / 2)
    turned = _rotate(page, (raw.x0 + crop.width / 2, raw.y0 + crop.height / 2), transform.deskew)
    shift = (turned[0] - raw.x0 - page[0], turned[1] - raw.y0 - page[1])
    return crop, Bounds(
        field.x0 + shift[0],
        field.y0 + shift[1],
        field.x1 + shift[0],
        field.y1 + shift[1],
    )


def _has_ink(image: Image.Image, rect: Bounds) -> bool:
    """Whether the box itself holds any mark (its printed border does not count)."""
    box = rect.grow(-FIELD_INSET_PIXELS).clamp(image.size)
    if box.width < 1 or box.height < 1:
        return False
    mask = _mask(image.crop((int(box.x0), int(box.y0), int(box.x1), int(box.y1))))
    return mask.getbbox() is not None


def _checkbox_marked(image: Image.Image, transform: Registration, rect: Bounds) -> bool:
    """Whether a checkbox carries a mark, by how much of it is inked.

    A tick is drawn by hand (or by the template's own appearance stream)
    and covers a fifth of the box; an empty box covers a couple of
    percent at most, and usually nothing.
    """
    crop, field = _crop(image, transform, rect)
    box = field.grow(-FIELD_INSET_PIXELS).clamp(crop.size)
    if box.width < 1 or box.height < 1:
        return False
    mask = _mask(crop.crop((int(box.x0), int(box.y0), int(box.x1), int(box.y1))))
    inked = mask.histogram()[255]
    return inked / (mask.size[0] * mask.size[1]) >= CHECKBOX_MARK_RATIO


def _ocr_words(image: Image.Image, *, config: str) -> list[_Word]:
    """Every word the engine recognises in a crop, with its confidence.

    Words the engine is less sure of than ``MIN_OCR_WORD_CONFIDENCE`` are
    dropped as noise; a crop that fails to OCR at all yields nothing,
    which the caller records as an unreadable region rather than a value.
    """
    # The engine's own OpenMP parallelism makes one small crop dramatically
    # slower on a busy host (measured: 50 s for one box with threads, under a
    # second without), so it runs single-threaded. Set here rather than at
    # import: a process-wide cap belongs to the call that needs it, not to
    # every module that happens to import this one.
    os.environ.setdefault("OMP_THREAD_LIMIT", "1")
    try:
        data = cast(
            dict[str, list[Any]],
            pytesseract.image_to_data(
                image,
                lang=OCR_LANGUAGE,
                config=config,
                output_type=pytesseract.Output.DICT,
            ),
        )
    except pytesseract.TesseractError:
        return []
    words = []
    for index, text in enumerate(data.get("text", [])):
        word = str(text).strip()
        confidence = _confidence(data, index)
        if not word or confidence < MIN_OCR_WORD_CONFIDENCE:
            continue
        words.append(
            _Word(
                text=word,
                confidence=confidence,
                bounds=Bounds(
                    float(data["left"][index]),
                    float(data["top"][index]),
                    float(data["left"][index] + data["width"][index]),
                    float(data["top"][index] + data["height"][index]),
                ),
            )
        )
    return words


def _confidence(data: dict[str, list[Any]], index: int) -> float:
    """One word's confidence as 0-1 (the engine reports a percentage)."""
    try:
        return float(data["conf"][index]) / 100
    except (KeyError, IndexError, TypeError, ValueError):
        return 0.0


def _words_inside(words: Sequence[_Word], field: Bounds) -> list[_Word]:
    """The words whose centre falls in the field's own rectangle.

    A hairline the height of the box, or a rule the width of it, is a piece
    of the box's own printing that survived the crop — the engine reads
    those as ``|`` and ``—`` — and never a value, so they are dropped here.

    Reading order is top to bottom, left to right, in bands a third of the
    box tall — enough for the two-line boxes (a bilingual company name)
    without letting a slightly taller glyph reorder a single line.
    """
    inside = [
        word
        for word in words
        if _centre_inside(word, field) and not _is_printing_fragment(word, field)
    ]
    band = max(1.0, field.height / 3)
    inside.sort(key=lambda word: (round((word.bounds.centre[1] - field.y0) / band), word.bounds.x0))
    return inside


def _centre_inside(word: _Word, field: Bounds) -> bool:
    centre = word.bounds.centre
    return field.x0 <= centre[0] <= field.x1 and field.y0 <= centre[1] <= field.y1


def _is_printing_fragment(word: _Word, field: Bounds) -> bool:
    """Whether a recognised word is a piece of the box rather than a value."""
    width = word.bounds.width / field.width if field.width else 0
    height = word.bounds.height / field.height if field.height else 0
    return (width < 0.02 and height > 0.5) or (height < 0.2 and width > 0.5)


def _join(words: Sequence[_Word]) -> str:
    """The recognised words as one value, with the page's own spacing.

    Tesseract returns words, not lines, and a value like
    ``staff@tcsp.example`` arrives as three of them: what tells a space
    from letter spacing is the gap between the boxes, measured against the
    average character width of the line. A gap narrower than that is a
    join; anything wider is a space (or a line break, which a form box
    only holds between a bilingual name's two scripts).
    """
    if not words:
        return ""
    characters = sum(len(word.text) for word in words)
    width = sum(word.bounds.width for word in words)
    if not characters or width <= 0:
        return " ".join(word.text for word in words)
    average_character = width / characters
    value = words[0].text
    for previous, word in pairwise(words):
        gap = word.bounds.x0 - previous.bounds.x1
        if _same_line(previous, word) and gap < JOIN_GAP_RATIO * average_character:
            value += word.text
        else:
            value = f"{value} {word.text}"
    return value


def _same_line(first: _Word, second: _Word) -> bool:
    """Whether two words sit on one line of the box (their boxes overlap)."""
    overlap = min(first.bounds.y1, second.bounds.y1) - max(first.bounds.y0, second.bounds.y0)
    return overlap > 0.5 * min(first.bounds.height, second.bounds.height)
