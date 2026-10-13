"""Generated PDF fixtures for the prior-filing extraction tests.

No PDF binaries are committed for this slice. Every fixture is built from
the pinned NAR1 template, so the read side meets the template's real
printing and widget structure:

- the AcroForm shape is produced by writing values through the template's
  own widgets (pypdfium2);
- the text-layer shape is produced by *drawing* the values into the named
  boxes as page text, which is the shape a flattened or CR-printed filing
  has — the template's print stays where it is, and its widgets stay empty;
- a composed package is the template with extra sheets inserted, the way a
  filing arrives with its own Schedule 1 sheets before the template pages
  that follow them (``research/counter-parties/cr/sample-corpus/README.md``):
  every later template page shifts, so the registry's absolute page numbers
  no longer name the package's pages.

Both shapes keep ``backend/forms/`` read-only and the repository free of
blobs.
"""

import ctypes
import io
import json
from collections.abc import Mapping, Sequence
from pathlib import Path

import pdfplumber
import pypdfium2 as pdfium
import pypdfium2.raw as raw
from pdfminer.pdftypes import PDFObjRef

# The pinned template's page 1 (MediaBox, points), reused so the text-layer
# fixtures place glyphs in the same absolute PDF coordinates the registry
# geometry uses, and so an inserted sheet is page-shaped.
PAGE_WIDTH = 595.44
PAGE_HEIGHT = 841.68

_FORMS_ROOT = Path(__file__).resolve().parents[1] / "forms"
NAR1_TEMPLATE = _FORMS_ROOT / "nar1" / "form_nar1_annual-return.pdf"


def geometry() -> dict[str, dict]:
    """The NAR1 geometry entries, keyed by widget name (registry data read
    directly, so the fixtures do not depend on the loader under test)."""
    payload = json.loads((_FORMS_ROOT / "nar1" / "geometry.json").read_text(encoding="utf-8"))
    return {field["name"]: field for field in payload["fields"]}


def _wstr(text: str):
    data = text.encode("utf-16-le") + b"\x00\x00"
    buffer = ctypes.create_string_buffer(data, len(data))
    return ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ushort))


def _qualified_name(data: dict) -> str:
    """The widget's fully-qualified field name, resolved through /Parent.

    Deliberately a second, minimal implementation of the parent walk: the
    fixture must be able to name widgets without borrowing the reader's
    resolution logic.
    """
    names: list[str] = []
    node: dict | None = data
    while node is not None:
        name = node.get("T")
        if isinstance(name, bytes):
            name = name.decode("latin-1")
        if name is not None:
            names.append(str(name))
        parent = node.get("Parent")
        node = parent.resolve() if isinstance(parent, PDFObjRef) else parent
    return ".".join(reversed(names))


def widget_locations() -> dict[str, tuple[int, int]]:
    """Widget name → (1-based page, annotation index) on the pinned template."""
    locations: dict[str, tuple[int, int]] = {}
    with pdfplumber.open(NAR1_TEMPLATE) as pdf:
        for page_number, page in enumerate(pdf.pages, start=1):
            for index, annot in enumerate(page.annots or []):
                name = _qualified_name(annot.get("data") or {})
                if name:
                    locations[name] = (page_number, index)
    return locations


def _composed_document(
    inserted_pages: Sequence[int],
) -> tuple[pdfium.PdfDocument, dict[int, int]]:
    """The pinned template with a blank sheet before each named template page.

    Returns the document and where each template page sits in it: an
    inserted sheet is a page, so every template page from the insertion
    point on shifts. A package's inserted sheets are usually copies of the
    sheet page (the corpus's are, and the reader places them like any other
    page); these are blank, which is the other thing an inserted sheet can
    be — a page that is not the form and is read from nowhere.
    """
    document = pdfium.PdfDocument(str(NAR1_TEMPLATE))
    positions = {page: page for page in range(1, len(document) + 1)}
    for index, template_page in enumerate(sorted(inserted_pages)):
        document.new_page(PAGE_WIDTH, PAGE_HEIGHT, index=template_page - 1 + index)
        for page in positions:
            if page >= template_page:
                positions[page] += 1
    return document, positions


def acroform_pdf(values: Mapping[str, str], *, inserted_pages: Sequence[int] = ()) -> bytes:
    """The pinned template with ``values`` written into its widgets.

    One value per widget: a text box takes it verbatim (UTF-16, so Chinese
    values exercise the decoder), a checkbox takes it as its ``/V`` state.
    ``inserted_pages`` assembles a composed package (``_composed_document``):
    a blank sheet before each named template page, shifting the pages after
    it, so a widget's registry page is no longer its package page.
    """
    locations = widget_locations()
    unknown = sorted(set(values) - set(locations))
    if unknown:
        raise KeyError(f"widgets not in the pinned template: {unknown}")
    document, positions = _composed_document(inserted_pages)
    handles: dict[int, pdfium.PdfPage] = {}
    for name, value in values.items():
        page_number, index = locations[name]
        physical = positions[page_number]
        if physical not in handles:
            handles[physical] = document[physical - 1]
        annot = raw.FPDFPage_GetAnnot(handles[physical].raw, index)
        raw.FPDFAnnot_SetStringValue(annot, b"V", _wstr(value))
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def text_layer_pdf(
    words: Mapping[str, str], *, inserted_pages: Sequence[int] = (), font_size: int = 12
) -> bytes:
    """A text-layer filing: the pinned template with ``words`` drawn into it.

    The words are drawn as page text rather than written into the widgets,
    so the reader sees the text layer a flattened or CR-printed filing
    carries: the template's printing stays, its widget annotations stay
    empty. ``inserted_pages`` assembles a composed package, as
    ``acroform_pdf`` does. Values must be Latin-script — they are drawn
    with a standard font, which carries no CJK glyphs.
    """
    entries = geometry()
    unknown = sorted(set(words) - set(entries))
    if unknown:
        raise KeyError(f"widgets not in the NAR1 geometry: {unknown}")
    document, positions = _composed_document(inserted_pages)
    font = raw.FPDFText_LoadStandardFont(document.raw, b"Helvetica")
    for name, text in words.items():
        widget = entries[name]
        x0, y0, _, y1 = widget["rect"]
        baseline = y0 + (y1 - y0) / 3
        page = document[positions[widget["page"]] - 1]
        item = raw.FPDFPageObj_CreateTextObj(document.raw, font, float(font_size))
        raw.FPDFText_SetText(item, _wstr(text))
        raw.FPDFPageObj_Transform(item, 1, 0, 0, 1, x0 + 4, baseline)
        raw.FPDFPage_InsertObject(page.raw, item)
        raw.FPDFPage_GenerateContent(page.raw)
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def scan_shaped_pdf() -> bytes:
    """A one-page PDF with a drawn rectangle and no text: the scan shape.

    Stands in for an image-only filing — the shape the OCR slice (E2) will
    read; this slice must recognise it and refuse to guess.
    """
    return _single_page_pdf(["0.6 g 40 60 515 720 re f"])


def _single_page_pdf(drawings: list[str]) -> bytes:
    """A minimal single-page PDF around one content stream."""
    content = ("\n".join(drawings) + "\n").encode("latin-1")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {PAGE_WIDTH} {PAGE_HEIGHT}] "
            "/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>"
        ).encode(),
        b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content + b"endstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for index, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{index} 0 obj\n".encode() + body + b"\nendobj\n"
    xref_at = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref_at}\n%%EOF\n"
    ).encode()
    return bytes(out)
