"""Scanned-filing reader tests: the registration maths, the frame and deskew
measurements, and the reads over the synthetic corpus (scope §2.1, §7;
contract §2.7, §5.1.2).

Two halves, and only the second needs the OCR engine:

- the geometry — a pure transform from the pinned template's frame onto a
  scan's pixels, the content bounds and the skew estimate that feed it, and
  the crop they place — runs everywhere (pypdfium2 and Pillow are Python
  dependencies);
- the reads need the system Tesseract binary (``deploy/backend.Dockerfile``
  installs it with the Traditional-Chinese data), so the reading tests skip
  cleanly where it is absent, like the opt-in MinIO round trip. See
  ``README.md``.

The input PDFs are generated at test time by ``tests.pdf_scan_fixtures``:
the pinned NAR1 template filled through its own widgets and rasterized into
image-only pages, then degraded — resolution, JPEG, rotation, noise, pixel
offset. No binary is committed and no real scan is used, so the accuracy
numbers these tests assert are synthetic-fixture numbers, not field data.
"""

import math
import shutil
from collections.abc import Sequence
from functools import cache

import pypdfium2 as pdfium
import pytesseract
import pytest
from PIL import Image, ImageDraw

from app.adapters.forms.registry import load_form_registry
from app.adapters.pdf import ocr
from app.adapters.pdf.extractor import ExtractionResult, extract
from app.adapters.pdf.ocr import Bounds
from app.domain.prior_filing import (
    AUTO_ACCEPT_THRESHOLD,
    ConfidenceBand,
    FormCode,
    InputShape,
    ReadOrigin,
    ReadSignal,
    routing_for,
    value_is_validated,
)
from tests.pdf_scan_fixtures import (
    CLEAN,
    CORPUS,
    EXPECTED,
    FILING,
    MADE_UP_DATE,
    Degradation,
    scan_pdf,
    shuffled_scan_pdf,
)

# The geometry tests need no engine; the reading tests do.
needs_tesseract = pytest.mark.skipif(
    shutil.which("tesseract") is None,
    reason="system tesseract binary is not installed (see README.md)",
)

# The lowest per-variant accuracy the corpus is allowed to fall to before
# this is a regression rather than a scan (measured: see the slice report).
DEGRADED_FLOOR = 0.7


def _rotate(point: tuple[float, float], centre: tuple[float, float], degrees: float) -> tuple:
    """Rotate a point counter-clockwise on screen, PIL's own convention.

    An independent implementation, so the transform under test is checked
    against the maths rather than against itself.
    """
    radians = math.radians(degrees)
    dx, dy = point[0] - centre[0], point[1] - centre[1]
    return (
        centre[0] + dx * math.cos(radians) + dy * math.sin(radians),
        centre[1] - dx * math.sin(radians) + dy * math.cos(radians),
    )


def _canvas(*, rules: Sequence[int], size: tuple[int, int] = (1000, 1400)) -> Image.Image:
    """A form-like page: long printed rules plus rows of small glyph blobs."""
    image = Image.new("L", size, 255)
    draw = ImageDraw.Draw(image)
    for y in rules:
        draw.line((100, y, 900, y), fill=0, width=3)
    for y in range(60, size[1], 37):
        for x in range(120, 900, 53):
            draw.rectangle((x, y, x + 30, y + 9), fill=0)
    return image


# --- The pure transform: template points -> scan pixels ---


def test_registration_recovers_a_scaled_translated_placement() -> None:
    template = Bounds(0, 0, 500, 800)
    straight = Bounds(100, 150, 1100, 1750)  # 2x scale, offset, no rotation
    registration = ocr.registration(
        template=template, scan=straight, deskew=0.0, image_size=(1200, 2000)
    )

    for u, v in ((0, 0), (1, 0), (1, 1), (0, 1), (0.5, 0.5)):
        mapped = registration.map_point(
            template.x0 + u * template.width, template.y0 + v * template.height
        )
        assert mapped == pytest.approx(
            (straight.x0 + u * straight.width, straight.y0 + v * straight.height)
        )


def test_registration_recovers_a_small_rotation() -> None:
    """The page straightens, and a field lands where the ink now is."""
    template = Bounds(0, 0, 500, 800)
    scan = Bounds(100, 150, 1100, 1750)
    image_size = (1300, 2100)
    centre = (image_size[0] / 2, image_size[1] / 2)
    deskew = -1.4  # the page arrived rotated by +1.4 degrees

    registration = ocr.registration(
        template=template, scan=scan, deskew=deskew, image_size=image_size
    )

    for u, v in ((0, 0), (1, 0), (1, 1), (0, 1), (0.25, 0.75)):
        mapped = registration.map_point(
            template.x0 + u * template.width, template.y0 + v * template.height
        )
        expected = _rotate(
            (scan.x0 + u * scan.width, scan.y0 + v * scan.height), centre, -deskew
        )
        assert mapped == pytest.approx(expected, abs=0.01)


def test_the_transform_lands_on_a_rotated_page_marker() -> None:
    """Checked against a real rotation, not against the maths alone.

    A canvas with a border and a printed mark is rotated the way a scanner
    would deliver it; the transform has to place the mark's template
    position where PIL actually moved it.
    """
    page = Image.new("L", (600, 800), 255)
    draw = ImageDraw.Draw(page)
    draw.rectangle((20, 30, 580, 770), outline=0, width=4)
    draw.ellipse((172, 232, 188, 248), fill=0)  # a mark centred on (180, 240)
    rotation = 1.4
    rotated = page.rotate(
        rotation, resample=Image.Resampling.BILINEAR, fillcolor=255, expand=True
    )
    # PIL turns the source canvas about its centre and centres the result in
    # the larger one, so the mark ends up here:
    shifted = _rotate((180, 240), (300.0, 400.0), rotation)
    expected = (
        shifted[0] + rotated.width / 2 - 300.0,
        shifted[1] + rotated.height / 2 - 400.0,
    )

    straight = ocr.straight_frame(rotated, -rotation)
    assert straight is not None
    registration = ocr.registration(
        template=Bounds(20, 30, 580, 770),
        scan=straight,
        deskew=-rotation,
        image_size=rotated.size,
    )

    assert registration.map_point(180, 240) == pytest.approx(expected, abs=2.5)


def test_a_rotated_page_is_measured_straightened() -> None:
    """A rotated page's box is inflated; its straightened frame is not."""
    page = Image.new("L", (600, 800), 255)
    ImageDraw.Draw(page).rectangle((40, 50, 560, 750), outline=0, width=3)
    frame = ocr.content_bounds(page)
    rotated = page.rotate(
        2.0, resample=Image.Resampling.BILINEAR, fillcolor=255, expand=True
    )
    raw = ocr.content_bounds(rotated)
    straightened = ocr.straight_frame(rotated, -2.0)
    assert frame is not None and raw is not None and straightened is not None

    assert raw.width > frame.width * 1.02
    assert raw.height > frame.height * 1.01
    assert straightened.width == pytest.approx(frame.width, abs=4)
    assert straightened.height == pytest.approx(frame.height, abs=4)


def test_map_rect_returns_the_four_mapped_corners() -> None:
    template = Bounds(0, 0, 500, 800)
    registration = ocr.registration(
        template=template, scan=Bounds(0, 0, 500, 800), deskew=0.0, image_size=(500, 800)
    )

    corners = registration.map_rect((10, 20, 30, 40))

    assert len(corners) == 4
    assert _corners(ocr.enclosing(corners)) == pytest.approx(_corners(Bounds(10, 20, 30, 40)))


def _corners(bounds: Bounds) -> tuple[float, float, float, float]:
    return (bounds.x0, bounds.y0, bounds.x1, bounds.y1)


# --- Measuring the page ---


def test_content_bounds_ignore_isolated_specks() -> None:
    image = Image.new("L", (400, 600), 255)
    draw = ImageDraw.Draw(image)
    draw.rectangle((60, 80, 340, 500), fill=0)
    draw.point((2, 2), fill=0)
    draw.rectangle((397, 598, 399, 599), fill=0)

    bounds = ocr.content_bounds(image)

    assert bounds is not None
    assert _corners(bounds) == pytest.approx((60, 80, 341, 501))


def test_content_bounds_of_a_blank_page_are_none() -> None:
    assert ocr.content_bounds(Image.new("L", (400, 600), 255)) is None


def test_frame_matches_only_a_page_sized_form() -> None:
    template = Bounds(0, 0, 2000, 2800)

    # The same frame, measured a little off: a scan, not another page.
    assert ocr.frame_matches(template, Bounds(10, 15, 2010, 2820))
    # A page of another shape, a page that lost an edge, and a photocopy
    # produced at a copier's 90% "fit to page" zoom are all refused.
    assert not ocr.frame_matches(template, Bounds(0, 0, 1400, 2800))
    assert not ocr.frame_matches(template, Bounds(0, 0, 2000, 1200))
    assert not ocr.frame_matches(template, Bounds(0, 0, 1800, 2520))


def _bands(image: Image.Image) -> tuple[Bounds, ...]:
    """The rule bands the reader would follow, for a page at its own scale."""
    frame = ocr.content_bounds(image)
    assert frame is not None
    return ocr.rule_bands(
        rules=ocr.template_rules(image), template=frame, scan=frame
    )


def test_deskew_angle_is_zero_for_a_straight_page() -> None:
    image = _canvas(rules=(200, 500, 900))

    assert ocr.deskew_angle(image, bands=_bands(image)) == pytest.approx(0.0, abs=0.15)


@pytest.mark.parametrize("rotation", [0.5, -0.5, 1.0, -1.5, 2.0])
def test_deskew_angle_undoes_the_rotation_a_page_arrived_with(rotation: float) -> None:
    straight = _canvas(rules=(200, 500, 900))
    bands = _bands(straight)
    image = straight.rotate(rotation, resample=Image.BILINEAR, fillcolor=255)

    estimate = ocr.deskew_angle(image, bands=bands)

    assert estimate == pytest.approx(-rotation, abs=0.25)


def _word(text: str, x0: float, x1: float, y0: float = 0.0, y1: float = 20.0) -> ocr._Word:
    return ocr._Word(text=text, confidence=0.9, bounds=Bounds(x0, y0, x1, y1))


def test_words_are_joined_with_the_spacing_the_page_has() -> None:
    """Tesseract returns words: an email arrives as three of them."""
    email = (_word("staff@", 0, 200), _word("tcsp.", 203, 400), _word("example", 403, 700))
    assert ocr._join(email) == "staff@tcsp.example"

    name = (
        _word("SYNTHETIC", 0, 260),
        _word("HARBOUR", 280, 500),
        _word("LIMITED", 520, 720),
    )
    assert ocr._join(name) == "SYNTHETIC HARBOUR LIMITED"


def test_words_on_separate_lines_are_joined_with_a_space() -> None:
    """A bilingual box holds two scripts on two lines; they are not one run."""
    stacked = (_word("合成", 0, 100, 0, 20), _word("ABC", 0, 100, 30, 50))

    assert ocr._join(stacked) == "合成 ABC"


def test_deskew_angle_without_a_rule_to_follow_is_zero() -> None:
    """No printed rule in the band: no estimate, and no invented correction."""
    image = _canvas(rules=())

    assert ocr.deskew_angle(image, bands=(Bounds(0, 500, 1000, 700),)) == 0.0


# --- Placing the boxes on the scans (no engine needed) ---


@cache
def _scan_bytes(name: str) -> bytes:
    """One corpus variant as a PDF, built once and shared."""
    return scan_pdf(FILING, dict(CORPUS)[name])


@cache
def _page(name: str) -> tuple[Image.Image, ocr.Registration, ocr.TemplatePage]:
    """A variant's first page, registered: the crop tests need no engine."""
    document = pdfium.PdfDocument(_scan_bytes(name))
    image = ocr._render(document[0])
    assert image is not None
    template = ocr._template_page(FormCode.NAR1, 1)
    assert template is not None
    frame = ocr.content_bounds(image)
    assert frame is not None
    angle = ocr.deskew_angle(
        image, bands=ocr.rule_bands(rules=template.rules, template=template.frame, scan=frame)
    )
    straight = ocr.straight_frame(image, angle)
    assert straight is not None
    return (
        image,
        ocr.registration(
            template=template.frame, scan=straight, deskew=angle, image_size=image.size
        ),
        template,
    )


def _box(name: str, template: ocr.TemplatePage) -> Bounds:
    registry = load_form_registry(FormCode.NAR1)
    return Bounds(*ocr._to_image_space(registry.geometry[name].rect, template.height))


@pytest.mark.parametrize(("name", "degradation"), CORPUS, ids=[name for name, _ in CORPUS])
def test_a_field_crop_holds_its_box_however_the_page_arrived(
    name: str, degradation: Degradation
) -> None:
    """The crop of a filled box holds that box, in every variant.

    Registration that is a few points out still crops a box — of the wrong
    rows. What is asserted here is that the box's own rectangle falls
    inside the crop that was cut for it, skewed page or not.
    """
    image, transform, template = _page(name)

    for widget_name in ("fill_2_P.1", "fill_3_P.1", "fill_17_P.1"):
        crop, field = ocr._crop(image, transform, _box(widget_name, template))
        assert field.x0 >= 0 and field.x1 <= crop.width, widget_name
        assert field.y0 >= 0 and field.y1 <= crop.height, widget_name
        assert ocr._has_ink(crop, field), widget_name


@pytest.mark.parametrize(("name", "degradation"), CORPUS, ids=[name for name, _ in CORPUS])
def test_the_ticked_box_is_marked_and_its_neighbours_are_not(
    name: str, degradation: Degradation
) -> None:
    """A tick and an empty box are told apart by ink, on every variant.

    The three boxes of the company-type group sit in a row with the same
    printing around them: only the ticked one carries a mark.
    """
    image, transform, template = _page(name)

    assert ocr._checkbox_marked(image, transform, _box("cb_1_P.1", template)) is True
    assert ocr._checkbox_marked(image, transform, _box("cb_2_P.1", template)) is False
    assert ocr._checkbox_marked(image, transform, _box("cb_3_P.1", template)) is False


# --- Reading a scanned filing (needs the engine) ---


@cache
def _scan(name: str) -> ExtractionResult:
    """One extraction run per corpus variant, shared by the tests below."""
    return extract(_scan_bytes(name), form=FormCode.NAR1)


@needs_tesseract
def test_the_clean_rasterization_reads_the_filing() -> None:
    result = _scan("clean-300")

    assert result.shape is InputShape.NEEDS_OCR
    assert {field.target: field.value for field in result.fields} == EXPECTED
    # The made-up date is the one target these fixtures get wrong: the
    # values in them are drawn through pdfium's substituted field face,
    # whose figures are decorative — the month "03" reads as "08" — so day
    # and year come out and the month does not. What must hold is that a
    # wrong date never arrives unflagged (the header triplets disagree, and
    # the disagreement rides with the target). See the slice report.
    if result.made_up_date is not None and result.made_up_date != MADE_UP_DATE:
        assert ReadSignal.REPEATED_FIELD_DISAGREEMENT in result.skipped["return.made_up_date"]


@needs_tesseract
def test_reads_name_the_engine_and_their_own_origin() -> None:
    result = _scan("clean-300")

    assert result.model == f"tesseract/{pytesseract.get_tesseract_version()}+ocr-fields/v1"
    origins = {field.origin for field in result.fields}
    assert ReadOrigin.OCR_CHECKBOX_MARK in origins
    assert ReadOrigin.OCR_TEXT_IN_FIELD_RECT in origins


@needs_tesseract
def test_a_different_specification_number_is_refused() -> None:
    """A scan of another specification is not this form, however it looks."""
    result = extract(
        scan_pdf(FILING, CLEAN, specification="1/2023"), form=FormCode.NAR1
    )

    assert result.shape is InputShape.NEEDS_OCR
    assert result.fields == ()
    assert ReadSignal.UNSUPPORTED_SPEC in result.document_signals


@needs_tesseract
def test_boxes_that_yield_nothing_are_reported_rather_than_guessed() -> None:
    """A blank scan of the right form reads nothing and says so per box."""
    result = extract(scan_pdf({}), form=FormCode.NAR1)

    assert result.fields == ()
    assert result.skipped["company.email"] == (ReadSignal.OCR_FIELD_UNREADABLE,)
    assert result.skipped["company.member_count"] == (ReadSignal.OCR_FIELD_UNREADABLE,)


@needs_tesseract
def test_a_page_read_from_the_wrong_image_never_auto_accepts() -> None:
    """A misfed scan registers and passes the specification check.

    Nothing about the page looks wrong, so the run has to answer for what
    it read elsewhere: no value may claim the certainty of an auto-accept.
    """
    result = extract(shuffled_scan_pdf(), form=FormCode.NAR1)
    scores = {field.target: field.confidence for field in result.fields}

    for field in result.fields:
        if field.confidence >= AUTO_ACCEPT_THRESHOLD:
            assert value_is_validated(field.target, field.value)
    assert routing_for(scores) is not ConfidenceBand.AUTO_ACCEPT


@needs_tesseract
def test_no_ocr_value_reaches_auto_accept_without_validating() -> None:
    """The acceptance bar, over every variant of the synthetic corpus."""
    for name, _ in CORPUS:
        for field in _scan(name).fields:
            if field.confidence < AUTO_ACCEPT_THRESHOLD:
                continue
            assert value_is_validated(field.target, field.value), (name, field.target)


@pytest.mark.parametrize(
    ("name", "degradation"), CORPUS, ids=[name for name, _ in CORPUS]
)
@needs_tesseract
def test_degraded_scans_still_read_the_filing(
    name: str, degradation: Degradation, capsys: pytest.CaptureFixture[str]
) -> None:
    """Per-field accuracy over the degraded corpus, reported as it is.

    Measured with tesseract 5.5.0: 14/14 on the clean rasterization, and
    11-13/14 on the degraded variants — the failures being the made-up
    date's month (the fixtures draw values through pdfium's substituted
    field face, whose figures are decorative), the amount, the phone's
    last digit, and a name or email whose words come back out of order or
    apart under heavy JPEG. The floor is a regression guard on synthetic
    fixtures, not a claim about field accuracy.
    """
    result = _scan(name)
    read = {field.target: field.value for field in result.fields}
    exact = [target for target, value in EXPECTED.items() if read.get(target) == value]
    with capsys.disabled():
        print(
            f"\n{name}: {degradation.describe()} — "
            f"{len(exact)}/{len(EXPECTED)} fields exactly right"
        )
        for target, value in EXPECTED.items():
            if read.get(target) != value:
                reasons = [signal.value for signal in result.skipped.get(target, ())]
                print(f"    {target}: wanted {value!r}, read {read.get(target)!r} {reasons}")
        if result.made_up_date != MADE_UP_DATE:
            print(f"    made-up date: wanted {MADE_UP_DATE}, read {result.made_up_date}")

    assert len(exact) >= DEGRADED_FLOOR * len(EXPECTED)
    assert (
        routing_for({field.target: field.confidence for field in result.fields})
        is not ConfidenceBand.AUTO_ACCEPT
    )
