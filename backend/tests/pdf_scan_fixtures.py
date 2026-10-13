"""Generated scanned-filing fixtures for the OCR tests (E2).

No scan binary is committed and no real scan is available here, so a scan
is synthesized from the pinned template: the NAR1 template is filled
through its own widgets (pdfium draws the values, and draws a tick from a
checkbox's appearance state), rasterized at the requested resolution,
degraded the way a scan degrades — rotation, Gaussian noise, JPEG
compression, a pixel offset — and assembled into an image-only PDF.

Two honest limits. The degradations stand in for what a flatbed or a phone
produces rather than reproducing a particular scanner, so accuracy measured
over this corpus is a property of these fixtures, not field performance.
And the template's own field appearance carries no CJK font, so the values
written here are Latin-script: Traditional-Chinese *values* are not covered,
however much of the form's printed text (and the OCR language list) is.
"""

import ctypes
import io
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from functools import cache

import pypdfium2 as pdfium
import pypdfium2.raw as raw
from PIL import Image, ImageChops, ImageDraw, ImageFont

from tests.pdf_fixtures import NAR1_TEMPLATE, geometry, widget_locations

# The pages of the pinned template the registry maps widgets onto (the
# template runs to 27 pages; nothing this product reads lives past 15). A
# scan of the whole form would carry them too, so the fixture does.
PAGES = 15

# Where the pinned template prints its specification line, in points from
# the page's top-left: "指明編號 2/2025 ... Specification No. 2/2025" along
# the foot of every page (measured from the template's own text).
SPEC_LINE = (50.0, 810.0, 245.0, 824.0)

# One synthetic filing, as the boxes would be filled in by hand or by CR's
# own tooling: the header repeats the BRN and the made-up date on every
# page, which is what the repeated-field consensus rules exist for.
BRN_BOXES = [f"fill_1_P.{page}" for page in range(1, 9)] + [
    f"fill_4_P.{page}" for page in range(9, PAGES + 1)
]
MADE_UP_DATE = date(2025, 3, 31)
DATE_BOXES = ("fill_6_P.1", "fill_7_P.1", "fill_8_P.1")
HEADER_DATE_BOXES = {
    f"fill_{offset}_P.{page}": part
    for page in range(9, PAGES + 1)
    for offset, part in ((1, "31"), (2, "03"), (3, "2025"))
}

FILING: dict[str, str] = {
    "fill_2_P.1": "SYNTHETIC HARBOUR LIMITED",
    "fill_3_P.1": "SYNTHETIC TRADING",
    "cb_1_P.1": "On",
    "fill_4_P.1": "7010",
    "fill_5_P.1": "TRADING",
    "fill_15_P.1": "FLAT 12",
    "fill_16_P.1": "HARBOUR BUILDING",
    "fill_17_P.1": "1 QUEEN'S ROAD",
    "fill_18_P.1": "CENTRAL",
    "fill_2_P.2": "staff@tcsp.example",
    "fill_3_P.2": "28001234",
    "fill_4_P.2": "1,234.50",
    "fill_5_P.2": "1024",
    **dict(zip(DATE_BOXES, ("31", "03", "2025"), strict=True)),
    **HEADER_DATE_BOXES,
    **dict.fromkeys(BRN_BOXES, "12345678"),
}

# What the filing states, per contract target and in canonical form: the
# values an extraction run has to reproduce from the scan.
EXPECTED: dict[str, str] = {
    "company.br_number": "12345678",
    "company.type": "private",
    "company.name_en": "SYNTHETIC HARBOUR LIMITED",
    "company.business_name": "SYNTHETIC TRADING",
    "company.business_nature_code": "7010",
    "company.business_nature_description": "TRADING",
    "company.registered_office.flat": "FLAT 12",
    "company.registered_office.building": "HARBOUR BUILDING",
    "company.registered_office.street": "1 QUEEN'S ROAD",
    "company.registered_office.district": "CENTRAL",
    "company.email": "staff@tcsp.example",
    "company.contact_phone_hk": "28001234",
    "company.mortgages_and_charges_total": "1234.50",
    "company.member_count": "1024",
}


@dataclass(frozen=True)
class Degradation:
    """How a scan of a page was made worse than the page itself."""

    dpi: int = 300
    jpeg_quality: int | None = None
    rotation: float = 0.0
    noise: float = 0.0
    offset: tuple[int, int] = (0, 0)

    def describe(self) -> str:
        parts = [f"{self.dpi}dpi"]
        if self.jpeg_quality is not None:
            parts.append(f"jpeg{self.jpeg_quality}")
        if self.rotation:
            parts.append(f"rot{self.rotation:+.2f}")
        if self.noise:
            parts.append(f"noise{self.noise:.0f}")
        if self.offset != (0, 0):
            parts.append(f"offset{self.offset[0]}x{self.offset[1]}")
        return " ".join(parts)


CLEAN = Degradation()
# The degraded set: a lower-resolution fax-quality pass, a photocopy with
# compression, two pages fed slightly askew, and a grainy offset scan.
CORPUS: tuple[tuple[str, Degradation], ...] = (
    ("clean-300", CLEAN),
    ("fax-150", Degradation(dpi=150)),
    ("jpeg-200", Degradation(dpi=200, jpeg_quality=45)),
    ("rotated-cw", Degradation(dpi=200, rotation=0.8)),
    ("rotated-ccw", Degradation(dpi=300, rotation=-1.6)),
    ("noisy-offset", Degradation(dpi=200, noise=10.0, offset=(7, 5))),
)


def scan_pdf(
    values: Mapping[str, str] = FILING,
    degradation: Degradation = CLEAN,
    *,
    specification: str | None = None,
) -> bytes:
    """One image-only PDF of the pinned template as a scan of it.

    ``values`` are written into the template's own widgets before
    rasterizing, so the pages are the real form with the real field
    rectangles — the reader under test meets the form itself, not a
    synthetic lookalike. ``specification`` overprints the printed
    specification number, which is how a scan of a *different* CR
    specification is synthesized.
    """
    rendered = _rendered(tuple(sorted(values.items())), degradation.dpi)
    pages = [_degrade(page, degradation) for page in rendered]
    if specification is not None:
        pages = [
            _overprint_specification(page, specification, degradation.dpi) for page in pages
        ]
    return pages_pdf(pages, dpi=degradation.dpi)


def shuffled_scan_pdf() -> bytes:
    """A scan whose first two page images were fed the wrong way round.

    The frame and the printed specification still match, so the reader has
    no way to tell: everything it reads off the pages is read from the
    wrong one. Nothing may come out of such a run claiming to be certain.
    """
    pages = list(_rendered(tuple(sorted(FILING.items())), CLEAN.dpi))
    pages[0], pages[1] = pages[1], pages[0]
    return pages_pdf(pages, dpi=CLEAN.dpi)


def pages_pdf(pages: Sequence[Image.Image], *, dpi: int) -> bytes:
    """Image-only PDF pages, one raster per page."""
    buffer = io.BytesIO()
    pages[0].save(
        buffer, format="PDF", resolution=dpi, save_all=True, append_images=list(pages[1:])
    )
    return buffer.getvalue()


def _overprint_specification(page: Image.Image, token: str, dpi: int) -> Image.Image:
    """Whiten the printed specification line and print another one there."""
    scale = dpi / 72
    x0, y0, x1, y1 = (value * scale for value in SPEC_LINE)
    page = page.copy()
    draw = ImageDraw.Draw(page)
    draw.rectangle((x0, y0, x1, y1), fill=255)
    draw.text(
        (x0 + 4, y0 + 2),
        f"Specification No. {token}",
        font=ImageFont.load_default(size=max(8, round(9 * scale))),
        fill=0,
    )
    return page


@cache
def _rendered(items: tuple[tuple[str, str], ...], dpi: int) -> tuple[Image.Image, ...]:
    """The filled template rasterized at ``dpi``, one image per page.

    Cached: rendering the pages is the expensive half of building a
    corpus, and every variant of one filing starts from the same pages.
    """
    document = pdfium.PdfDocument(_filled(items))
    document.init_forms()
    try:
        return tuple(
            document[index].render(scale=dpi / 72).to_pil().convert("L").copy()
            for index in range(min(PAGES, len(document)))
        )
    finally:
        document.close()


def _filled(items: tuple[tuple[str, str], ...]) -> bytes:
    """The pinned template with the values written into its own widgets.

    A checkbox needs both its value and its appearance state set: pdfium
    draws a tick from the state, and a scan has to carry the drawn mark
    for the reader to find.
    """
    locations = widget_locations()
    types = {name: entry["type"] for name, entry in geometry().items()}
    document = pdfium.PdfDocument(str(NAR1_TEMPLATE))
    try:
        pages: dict[int, pdfium.PdfPage] = {}
        for name, value in items:
            page_number, index = locations[name]
            if page_number not in pages:
                pages[page_number] = document.get_page(page_number - 1)
            page = pages[page_number]
            annotation = raw.FPDFPage_GetAnnot(page.raw, index)
            raw.FPDFAnnot_SetStringValue(annotation, b"V", _utf16(value))
            if types[name] == "/Btn":
                raw.FPDFAnnot_SetStringValue(annotation, b"AS", _utf16(value))
        buffer = io.BytesIO()
        document.save(buffer)
    finally:
        document.close()
    return buffer.getvalue()


def _utf16(text: str):
    data = text.encode("utf-16-le") + b"\x00\x00"
    buffer = ctypes.create_string_buffer(data, len(data))
    return ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ushort))


def _degrade(page: Image.Image, degradation: Degradation) -> Image.Image:
    """One rasterized page as the scanner would have left it."""
    if degradation.rotation:
        # expand: a scanner turns the whole sheet, it does not clip the
        # corners; without this the fixture would lose the printed
        # specification line the reader is asked to check.
        page = page.rotate(
            degradation.rotation,
            resample=Image.Resampling.BILINEAR,
            fillcolor=255,
            expand=True,
        )
    if degradation.noise:
        # effect_noise is centred on mid-grey, so adding it with an offset
        # of -128 is a zero-mean addition (clipped, as a sensor would be).
        page = ImageChops.add(
            page, Image.effect_noise(page.size, degradation.noise), 1.0, -128
        )
    if degradation.offset != (0, 0):
        shifted = Image.new(
            "L", (page.width + degradation.offset[0], page.height + degradation.offset[1]), 255
        )
        shifted.paste(page, degradation.offset)
        page = shifted
    if degradation.jpeg_quality is not None:
        buffer = io.BytesIO()
        page.save(buffer, format="JPEG", quality=degradation.jpeg_quality)
        page = Image.open(buffer).convert("L")
    return page
