"""Prior-filing extraction adapter: read the digital shapes of a filing PDF
(scope §2.1, §7; contract §2.7).

Which shape a filing arrives in decides how it is read, and the reader
never guesses at what it cannot see:

- **AcroForm values** — the filing was filled digitally, so the template's
  own field values are the data, read exactly.
- **Text layer** — the page carries glyphs, so values are found by locating
  text inside the pinned template's widget rectangles: geometry-driven and
  capped below the auto-accept band.
- **Neither** — an image-only page is a scan, and is read by the OCR
  adapter (``app.adapters.pdf.ocr``): the page is registered against the
  pinned template's frame, the printed specification number is verified,
  and each box is read and scored. A scan this reader cannot place or
  confirm is refused, never guessed at.

Every page is placed on the pinned template by its own printed content
before anything is read off it, and the form's printed specification number
is verified with the scan reader's own region and token rule
(``app.adapters.pdf.ocr``). Placement is by content because a filing
arrives as a composed package — its own sheets sit between the template's
pages, shifting every page after them — while the registry's geometry
addresses the template's absolute pages. A page that is not the pinned
form's, or a filing whose specification is another one (or cannot be read
at all), is refused: nothing is read off it.

Values are mapped to master-data targets through the registry: an item's
target may join several contract paths with `` + ``, and the reader only
proposes items whose paths *all* map 1:1 to a widget (positionally, in
registry order), whose single path repeats across the item's widgets, or
that are the bilingual company-name box. An item the registry carries but
this slice does not write (officer rows, share-capital collections, filing
metadata) is skipped whole, never half-read.

Confidence comes from the domain ladder (``app.domain.prior_filing``): the
read's origin sets the base, and honest signals cap it — a bilingual name
box split by script, a repeated header field whose occurrences disagree, or
an OCR value no machine check can confirm. A value that does not normalise
is not proposed at all; the assessment records why, per target.

The adapter reads bytes: no database, no blob store, and registry files are
reached only through ``load_form_registry``.
"""

import io
import re
from collections import Counter
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date
from functools import cache
from typing import Any, cast

import pdfplumber
import pypdfium2 as pdfium
from pdfminer.pdftypes import PDFObjRef
from pdfminer.psparser import PSLiteral
from pdfplumber.pdf import PDF

from app.adapters.forms.registry import (
    FormRegistry,
    FormRegistryError,
    WidgetGeometry,
    load_form_registry,
    template_path,
)
from app.adapters.pdf import ocr
from app.adapters.pdf.options import vocabulary_options
from app.domain.prior_filing import (
    FormCode,
    InputShape,
    InvalidFieldValueError,
    ReadOrigin,
    ReadSignal,
    TargetWrite,
    cap_for,
    confidence_for,
    individual_paths,
    is_supported_target,
    normalize_value,
    ocr_confidence,
    parts_to_date,
    split_bilingual,
    target_spec,
    value_is_validated,
)

# The option semantics of a checkbox group are registry prose (mapping.json
# notes: "cb_1_P.1 = 私人公司 Private company; …"), not machine-readable data.
# Their one home is ``app.adapters.pdf.options`` — the same table the form
# auto-drafter's fill planner reads — and this is the reader's own view of it:
# the groups whose boxes each carry a vocabulary word, which is the value an
# ``ExtractedField`` can propose. A group whose boxes are a yes/no pair has no
# such value and is not read here. A test asserts every widget named in the
# table exists in one of the two pinned templates' geometry.
_READ_GROUPS: tuple[str, ...] = ("company.type",)

_CHECKBOX_OPTIONS: Mapping[str, Mapping[str, str]] = {
    target: vocabulary_options(target) for target in _READ_GROUPS
}

_MADE_UP_DATE_TARGET = "return.made_up_date"
_NAME_SUFFIXES = ("name_en", "name_zh")

# The §2.7 ``model`` value for the deterministic reader: the implementation
# and its rule revision. The scanned path names its own (the OCR pipeline
# plus the engine version); the LLM-backed paths will carry a pinned model
# id plus prompt version here.
EXTRACTOR_MODEL = "pdf-geometry/v1"

# A filing page is placed on the pinned template by the print it carries:
# the template page whose printed words the page's own words cover most.
# The registry's geometry addresses the template's absolute pages, but a
# package's own sheets sit between them (the sample corpus inserts its
# Schedule 1 sheets before template pages 10-15), so every read is placed
# through this map. Below this coverage nothing is placed — a page too
# damaged to tell, or no page of this form at all — and nothing is read
# off it.
PAGE_MATCH_MIN_COVERAGE = 0.75
# Words shorter than this, and bare numbers, are too common to place a
# page by.
_ANCHOR_MIN_LENGTH = 3
_ANCHOR_SPLIT = re.compile(r"\s+")


class UnreadablePdfError(ValueError):
    """The bytes are not a PDF this reader can open."""


@dataclass(frozen=True)
class ExtractedField:
    """One proposed value for one master-data target.

    ``pdf_field``/``page`` are provenance (the first widget the winning
    value came from, and the page of the package it was read on — which an
    inserted sheet shifts away from the registry's own page number);
    ``confidence``, ``origin`` and ``signals`` are the domain ladder's
    inputs, stored for the review queue.
    """

    target: str
    value: str
    confidence: float
    origin: ReadOrigin
    signals: tuple[ReadSignal, ...]
    pdf_field: str
    page: int


@dataclass(frozen=True)
class ExtractionResult:
    """What one run of the reader found.

    ``skipped`` holds the targets that produced no proposal, with the
    signals that explain why — a checkbox group with two ticks, a value
    that does not normalise, a filing date the pages disagree on. Nothing
    is dropped silently.
    """

    shape: InputShape
    fields: tuple[ExtractedField, ...]
    made_up_date: date | None
    skipped: dict[str, tuple[ReadSignal, ...]]
    document_signals: tuple[ReadSignal, ...] = ()
    model: str = EXTRACTOR_MODEL


@dataclass(frozen=True)
class _Read:
    """One widget occurrence as it was read, plus the evidence it carries.

    The digital shapes give each read a fixed ladder rung; an OCR read
    brings its own evidence instead — the engine's per-word confidences
    (scored against validation by ``_field``), or a fixed confidence for a
    checkbox mark, which no word score covers.
    """

    widget: str
    page: int
    value: str | None
    origin: ReadOrigin
    signals: tuple[ReadSignal, ...] = ()
    word_confidences: tuple[float, ...] | None = None
    confidence: float | None = None


def extract(data: bytes, *, form: FormCode) -> ExtractionResult:
    """Read one filing PDF into per-target values, never guessing.

    Raises ``UnreadablePdfError`` when the bytes cannot be parsed at all;
    every other outcome, including "nothing readable", is a result.
    """
    registry = load_form_registry(form)
    with _open(data) as pdf:
        placement = _placement(data, registry)
        acroform = _acroform_reads(pdf.pages, registry, placement.pages)
        if acroform:
            shape, reads = InputShape.ACROFORM, acroform
        elif any(page.extract_words() for page in pdf.pages):
            shape, reads = (
                InputShape.TEXT_LAYER,
                _text_layer_reads(pdf.pages, registry, placement.pages),
            )
        else:
            return _scan_result(data, registry)
    if placement.refusal is not None:
        return ExtractionResult(
            shape=shape,
            fields=(),
            made_up_date=None,
            skipped={},
            document_signals=(placement.refusal,),
        )
    return _map_to_targets(reads, registry, shape)


def _scan_result(data: bytes, registry: FormRegistry) -> ExtractionResult:
    """Read an image-only filing through the OCR adapter.

    A refused scan produces no reads at all — a batch with no proposals and
    the refusal in the assessment — while a read scan is mapped through
    exactly the same target machinery as the digital shapes, so the review
    queue and the confirm path need know nothing about how it was read.
    """
    try:
        reading = ocr.read_scanned(data, registry=registry)
    except ocr.ScanUnreadableError as exc:
        raise UnreadablePdfError(f"could not read the PDF: {exc}") from None
    if not reading.reads:
        return ExtractionResult(
            shape=InputShape.NEEDS_OCR,
            fields=(),
            made_up_date=None,
            skipped={},
            document_signals=reading.signals,
            model=reading.model,
        )
    reads = {
        read.widget: _Read(
            widget=read.widget,
            page=read.page,
            value=read.value,
            origin=read.origin,
            signals=read.signals,
            word_confidences=read.word_confidences,
            confidence=read.confidence,
        )
        for read in reading.reads
    }
    result = _map_to_targets(reads, registry, InputShape.NEEDS_OCR, model=reading.model)
    return replace(result, document_signals=reading.signals)


def _open(data: bytes) -> PDF:
    try:
        return pdfplumber.open(io.BytesIO(data))
    except Exception as exc:  # pdfminer raises a family of parse errors
        raise UnreadablePdfError(f"could not read the PDF: {exc}") from None


# --- Placing the package's pages on the pinned form ---


@dataclass(frozen=True)
class _Placement:
    """The filing's pages on the pinned template, and the form's verdict.

    ``pages`` maps each physical page of the filing to the template page its
    printing is, and every read is placed through it. ``refusal`` is set
    when the filing is not the pinned form — its specification cannot be
    read, or is another one — and the run then proposes nothing.
    """

    pages: dict[int, int]
    refusal: ReadSignal | None


def _placement(data: bytes, registry: FormRegistry) -> _Placement:
    """Where the filing's pages sit on the pinned form, and whether it is one.

    One pass over the filing's own text layer (pdfium, the rasterizer the
    scan path already uses): each page is placed on the template page whose
    printed words it covers, and then the placed specification page is
    checked against the registry's pinned number. Pages are placed by what
    they print rather than by their position because a package brings its
    own sheets — the sample corpus's Schedule 1 sheets shift template pages
    10-15 — while the registry's geometry names the template's absolute
    pages. A page of another form (or too damaged to tell) is placed
    nowhere, and nothing is read off it.
    """
    anchors = _template_anchors(registry.form)
    try:
        document = pdfium.PdfDocument(data)
    except Exception:  # pdfium reports its own error family
        return _Placement(pages={}, refusal=ReadSignal.SPEC_REGION_UNREADABLE)
    try:
        pages: dict[int, int] = {}
        for offset in range(len(document)):
            placed = _best_template_page(_page_words(_page_text(document[offset])), anchors)
            if placed is not None:
                pages[offset + 1] = placed
        return _Placement(pages=pages, refusal=_form_refusal(document, registry, pages))
    finally:
        document.close()


@cache
def _template_anchors(form: FormCode) -> tuple[frozenset[str], ...]:
    """Every pinned template page's printed words, read once per process.

    The templates are pinned and read-only, and every filing is placed
    against them, so the measurement is taken once per form.
    """
    document = pdfium.PdfDocument(str(template_path(form)))
    try:
        return tuple(_page_words(_page_text(document[offset])) for offset in range(len(document)))
    finally:
        document.close()


def _page_text(page: pdfium.PdfPage) -> str:
    """One page's text layer, as pdfium reads it.

    Deliberately not pdfminer: a filing may carry an injected face that
    pdfminer refuses to parse (the sample corpus's flattened sheets do),
    and the page's own print is still readable there.
    """
    return cast(str, page.get_textpage().get_text_range())


def _page_words(text: str) -> frozenset[str]:
    """A page's printed words as placement anchors (lowercased).

    Bare numbers are left out: the boxes carry values that are numbers and
    short codes, and a page is placed by what it prints, not by what
    somebody wrote on it.
    """
    return frozenset(
        word.lower()
        for word in _ANCHOR_SPLIT.split(text)
        if len(word) >= _ANCHOR_MIN_LENGTH and not word.isdigit()
    )


def _best_template_page(
    words: frozenset[str], anchors: Sequence[frozenset[str]]
) -> int | None:
    """The template page a page's printing covers most, or ``None``.

    The measure is coverage of the template page's own print — how much of
    it the page carries — so a page with values and notes of its own still
    places on the page it is a copy of. A tie goes to the earlier template
    page, and below the floor nothing is placed.
    """
    best_page: int | None = None
    best_coverage = 0.0
    for number, printed in enumerate(anchors, start=1):
        if not printed:
            continue
        coverage = len(words & printed) / len(printed)
        if coverage > best_coverage:
            best_page, best_coverage = number, coverage
    return best_page if best_coverage >= PAGE_MATCH_MIN_COVERAGE else None


def _form_refusal(
    document: pdfium.PdfDocument, registry: FormRegistry, pages: Mapping[int, int]
) -> ReadSignal | None:
    """Whether the placed pages print the registry's specification number.

    The region and the token rule are the scan reader's
    (``ocr.spec_region`` locates the region in the pinned template's own
    text, so a registry refresh moves it without touching either reader);
    what differs is where the text comes from — the page's own text layer
    rather than words the engine recognised in a raster. A page that could
    not be placed, or a region no number comes out of, is refused rather
    than assumed: geometry without the right specification is a map onto a
    form this product does not know.
    """
    spec = ocr.spec_region(registry.form)
    if spec is None:
        return None
    template_page, region = spec
    physical = next(
        (page for page in sorted(pages) if pages[page] == template_page), None
    )
    if physical is None:
        return ReadSignal.SPEC_REGION_UNREADABLE
    tokens = ocr.spec_tokens(_text_in_region(document[physical - 1], region))
    if not tokens:
        return ReadSignal.SPEC_REGION_UNREADABLE
    expected = ocr.expected_spec_token(registry.specification_no)
    return None if expected is not None and expected in tokens else ReadSignal.UNSUPPORTED_SPEC


def _text_in_region(page: pdfium.PdfPage, region: ocr.Bounds) -> str:
    """One page's own text inside a region, in the region's own coordinates.

    ``ocr.spec_region`` measures from the page's top-left (pdfplumber's
    space, y down) and pdfium measures from the bottom-left (y up), so the
    region is flipped before each character's centre is compared with it.
    """
    height = page.get_size()[1]
    left, right = region.x0, region.x1
    bottom, top = height - region.y1, height - region.y0
    textpage = page.get_textpage()
    characters = []
    for index in range(textpage.count_chars()):
        x0, y0, x1, y1 = textpage.get_charbox(index)
        centre = ((x0 + x1) / 2, (y0 + y1) / 2)
        if left <= centre[0] <= right and bottom <= centre[1] <= top:
            characters.append(textpage.get_text_range(index, 1))
    return "".join(characters)


# --- Reading the two digital shapes ---


def _acroform_reads(
    pages: Sequence[Any], registry: FormRegistry, page_map: Mapping[int, int]
) -> dict[str, _Read]:
    """Every filled widget, by its fully-qualified field name.

    A filled text box yields its value; a ticked checkbox yields its tick.
    Everything else is simply absent: an empty box says nothing, and an
    unticked box is not a read either. A widget is read only where the page
    it sits on has been placed as its template page, so a box of the same
    name on some other page is never taken for it.
    """
    reads: dict[str, _Read] = {}
    for page_number, page in enumerate(pages, start=1):
        template_page = page_map.get(page_number)
        if template_page is None:
            continue
        for annot in page.annots or []:
            data = annot.get("data") or {}
            widget = registry.geometry.get(_qualified_name(data))
            if widget is None or widget.page != template_page:
                continue
            if _widget_type(data) == "/Btn":
                if _is_selected(data):
                    reads[widget.name] = _Read(
                        widget.name, page_number, "on", ReadOrigin.EXACT_FIELD_VALUE
                    )
                continue
            value = (_text_value(data) or "").strip()
            if value:
                reads[widget.name] = _Read(
                    widget.name, page_number, value, ReadOrigin.EXACT_FIELD_VALUE
                )
    return reads


def _text_layer_reads(
    pages: Sequence[Any], registry: FormRegistry, page_map: Mapping[int, int]
) -> dict[str, _Read]:
    """Every widget whose rectangle contains text, by widget name.

    The words inside a rectangle are joined in reading order (top to
    bottom, left to right); a checkbox rectangle holding any glyph reads as
    ticked, which is how a flattened filing carries its marks. As in the
    AcroForm shape, a box is read only where its page has been placed as the
    box's template page — a page of the template no box lives on (the notes
    pages) is not even read for words.
    """
    by_template_page: dict[int, list[WidgetGeometry]] = {}
    for widget in registry.geometry.values():
        by_template_page.setdefault(widget.page, []).append(widget)
    reads: dict[str, _Read] = {}
    for page_number, page in enumerate(pages, start=1):
        template_page = page_map.get(page_number)
        if template_page is None:
            continue
        widgets = by_template_page.get(template_page, ())
        if not widgets:
            continue
        words = page.extract_words()
        if not words:
            continue
        for widget in widgets:
            inside = [word for word in words if _word_inside(widget, word, page.height)]
            if not inside:
                continue
            inside.sort(key=lambda word: (round(word["top"], 1), word["x0"]))
            reads[widget.name] = _Read(
                widget.name,
                page_number,
                " ".join(word["text"] for word in inside),
                ReadOrigin.TEXT_IN_FIELD_RECT,
            )
    return reads


def _word_inside(widget: WidgetGeometry, word: Mapping[str, float], page_height: float) -> bool:
    """Whether the word's centre falls inside the widget rectangle.

    The registry stores PDF-space rectangles (points, origin bottom-left);
    pdfplumber measures from the page top, so the rectangle is flipped
    before comparing.
    """
    x0, y0, x1, y1 = widget.rect
    top, bottom = page_height - y1, page_height - y0
    centre_x = (word["x0"] + word["x1"]) / 2
    centre_y = (word["top"] + word["bottom"]) / 2
    return x0 <= centre_x <= x1 and top <= centre_y <= bottom


def _qualified_name(data: Mapping[str, Any]) -> str:
    """The widget's fully-qualified field name through its parent chain.

    CR templates nest every positional widget under a parent field, so the
    name the registry knows (``fill_1_P.1``) is the chain's /T values
    joined, not the widget's own.
    """
    names = [name for node in _chain(data) if (name := _as_text(node.get("T")))]
    return ".".join(reversed(names))


def _widget_type(data: Mapping[str, Any]) -> str | None:
    for node in _chain(data):
        node_type = node.get("FT")
        if node_type is not None:
            return str(node_type)
    return None


def _text_value(data: Mapping[str, Any]) -> str | None:
    for node in _chain(data):
        if "V" in node:
            return _as_text(node["V"])
    return None


def _is_selected(data: Mapping[str, Any]) -> bool:
    """Whether a checkbox widget is ticked: its value is a name but not /Off.

    Filled forms carry the tick on the field (``/V``); a freshly-saved one
    may only carry the appearance state (``/AS``), so both are consulted.
    """
    for key in ("V", "AS"):
        value = next((node[key] for node in _chain(data) if key in node), None)
        if value is not None:
            name = _as_text(value)
            return bool(name) and name != "Off"
    return False


def _chain(data: Mapping[str, Any]) -> Iterator[Mapping[str, Any]]:
    """The annotation dictionary and its parent fields, widget first."""
    node: Mapping[str, Any] | None = data
    while node is not None:
        yield node
        parent = node.get("Parent")
        node = parent.resolve() if isinstance(parent, PDFObjRef) else parent


def _as_text(value: Any) -> str | None:
    """A PDF string or name as text (``None`` for anything else).

    Strings are PDFDocEncoding (read as Latin-1, exact for the ASCII values
    the forms carry) or UTF-16 with a byte-order mark — what Acrobat and
    pdfium write for the Chinese entries.
    """
    if isinstance(value, PSLiteral):
        name = value.name
        return name if isinstance(name, str) else name.decode("latin-1")
    if isinstance(value, bytes):
        if value[:2] == b"\xfe\xff":
            return value[2:].decode("utf-16-be", errors="replace")
        if value[:2] == b"\xff\xfe":
            return value[2:].decode("utf-16-le", errors="replace")
        try:
            return value.decode("utf-8")
        except UnicodeDecodeError:
            return value.decode("latin-1")
    if isinstance(value, str):
        return value
    return None


# --- Mapping widgets to master-data targets ---


def _map_to_targets(
    reads: Mapping[str, _Read],
    registry: FormRegistry,
    shape: InputShape,
    *,
    model: str = EXTRACTOR_MODEL,
) -> ExtractionResult:
    occurrences: dict[str, list[_Read]] = {}
    skipped: dict[str, tuple[ReadSignal, ...]] = {}
    for item in registry.items:
        _distribute(
            item.item,
            item.item_type,
            item.target,
            item.pdf_fields,
            reads,
            registry,
            occurrences,
            skipped,
        )

    fields: list[ExtractedField] = []
    for target, target_reads in occurrences.items():
        if target_spec(target).write is TargetWrite.CONTEXT:
            continue
        field = (
            _option_field(target, target_reads, skipped)
            if target in _CHECKBOX_OPTIONS
            else _value_field(target, target_reads, skipped)
        )
        if field is not None:
            fields.append(field)

    made_up_date, date_signals = _made_up_date(occurrences.get(_MADE_UP_DATE_TARGET, ()))
    if date_signals:
        skipped[_MADE_UP_DATE_TARGET] = date_signals
    return ExtractionResult(
        shape=shape,
        fields=tuple(sorted(fields, key=lambda field: field.target)),
        made_up_date=made_up_date,
        skipped=skipped,
        model=model,
    )


def _distribute(
    item: str,
    item_type: str,
    registry_target: str,
    pdf_fields: Sequence[str],
    reads: Mapping[str, _Read],
    registry: FormRegistry,
    occurrences: dict[str, list[_Read]],
    skipped: dict[str, tuple[ReadSignal, ...]],
) -> None:
    """Attach one registry item's widget reads to its individual targets.

    Only the mappings the reader can make exactly are read; anything else
    would have to guess which box carries which path, so it is left out
    (the guard test asserts the pinned registry has no such item).
    """
    paths = individual_paths(registry_target)
    if not paths or any(not is_supported_target(path) for path in paths):
        return
    if item_type == "date_parts":
        _distribute_date_parts(paths[0], pdf_fields, reads, registry, occurrences, skipped)
        return
    if len(paths) == len(pdf_fields):
        for path, widget in zip(paths, pdf_fields, strict=True):
            if widget in reads:
                occurrences.setdefault(path, []).append(reads[widget])
        return
    if len(paths) == 1:
        occurrences.setdefault(paths[0], []).extend(
            reads[widget] for widget in pdf_fields if widget in reads
        )
        return
    if len(pdf_fields) == 1 and {path.rsplit(".", 1)[-1] for path in paths} == set(_NAME_SUFFIXES):
        _distribute_bilingual_name(paths, reads.get(pdf_fields[0]), occurrences, skipped)
        return
    raise FormRegistryError(
        f"{item}: target {registry_target!r} does not map to its widgets "
        f"({len(paths)} paths, {len(pdf_fields)} widgets)"
    )


def _distribute_bilingual_name(
    paths: Sequence[str],
    read: _Read | None,
    occurrences: dict[str, list[_Read]],
    skipped: dict[str, tuple[ReadSignal, ...]],
) -> None:
    """Split a company-name box carrying both scripts (mapping notes).

    One script only means the box is exactly that name and no split is
    needed; both scripts make it a heuristic, so the reads carry the cap
    signal that says so.
    """
    if read is None:
        return
    if not read.value:
        for path in paths:
            _record_absence(path, (read,), skipped)
        return
    latin, cjk = split_bilingual(read.value)
    if not latin or not cjk:
        suffix = "name_zh" if cjk else "name_en"
        target = next(path for path in paths if path.endswith(f".{suffix}"))
        occurrences.setdefault(target, []).append(read)
        return
    for suffix, value in (("name_en", latin), ("name_zh", cjk)):
        target = next(path for path in paths if path.endswith(f".{suffix}"))
        occurrences.setdefault(target, []).append(
            _Read(
                read.widget,
                read.page,
                value,
                read.origin,
                (ReadSignal.BILINGUAL_BOX_SPLIT,),
                word_confidences=read.word_confidences,
                confidence=read.confidence,
            )
        )


def _distribute_date_parts(
    target: str,
    pdf_fields: Sequence[str],
    reads: Mapping[str, _Read],
    registry: FormRegistry,
    occurrences: dict[str, list[_Read]],
    skipped: dict[str, tuple[ReadSignal, ...]],
) -> None:
    """Assemble each page's DD/MM/YYYY triplet into one read per page.

    A page whose triplet is incomplete yields nothing (the reviewer
    supplies the date at confirmation); a partial or impossible one is
    recorded as unparsable rather than silently dropped.
    """
    by_page: dict[int, list[str]] = {}
    for widget in pdf_fields:
        by_page.setdefault(registry.geometry[widget].page, []).append(widget)
    for page_number, widgets in by_page.items():
        parts = tuple(
            (reads[widget].value or "") if widget in reads else "" for widget in widgets
        )
        assembled = parts_to_date(parts)
        if assembled is None:
            if any(parts):
                skipped[target] = (ReadSignal.VALUE_UNPARSABLE,)
            else:
                _record_absence(target, tuple(reads[w] for w in widgets if w in reads), skipped)
            continue
        origin = (
            reads[widgets[0]].origin if widgets[0] in reads else ReadOrigin.EXACT_FIELD_VALUE
        )
        # The assembled date is only as sure as its weakest part, so the
        # parts' word confidences travel together with it.
        confidences = tuple(
            confidence
            for widget in widgets
            for confidence in (reads[widget].word_confidences or ())
        )
        occurrences.setdefault(target, []).append(
            _Read(
                widgets[0],
                page_number,
                assembled.isoformat(),
                origin,
                word_confidences=confidences or None,
            )
        )


def _value_field(
    target: str, reads: Sequence[_Read], skipped: dict[str, tuple[ReadSignal, ...]]
) -> ExtractedField | None:
    """The consensus value for one target, normalised, or ``None``.

    Repeated occurrences (the page-header BRN, the made-up date on every
    schedule page) must agree: the most common value is proposed, and a
    disagreement is recorded rather than averaged away. The winning read's
    own signals travel with it — a split bilingual box stays capped
    whatever the consensus says.
    """
    value, disagreement, source = _consensus(reads)
    if value is None or source is None:
        _record_absence(target, reads, skipped)
        return None
    try:
        normalised = normalize_value(target, value)
    except InvalidFieldValueError:
        skipped[target] = (ReadSignal.VALUE_UNPARSABLE,)
        return None
    return _field(
        target,
        normalised,
        source,
        source.signals
        + ((ReadSignal.REPEATED_FIELD_DISAGREEMENT,) if disagreement else ()),
    )


def _option_field(
    target: str, reads: Sequence[_Read], skipped: dict[str, tuple[ReadSignal, ...]]
) -> ExtractedField | None:
    """The selected option of a checkbox group, or nothing.

    Exactly one ticked box is a reading; two are a contradiction the box
    cannot resolve, so the target is skipped with the signal that says so.
    A blank group says nothing at all.
    """
    selected = [read for read in reads if read.value]
    if not selected:
        _record_absence(target, reads, skipped)
        return None
    options = _CHECKBOX_OPTIONS[target]
    if len(selected) != 1 or selected[0].widget not in options:
        skipped[target] = (ReadSignal.CHECKBOX_GROUP_NOT_SINGLE,)
        return None
    return _field(target, options[selected[0].widget], selected[0], selected[0].signals)


def _field(
    target: str, value: str, source: _Read, signals: tuple[ReadSignal, ...]
) -> ExtractedField:
    """One proposed field, scored from the read's own evidence.

    A digital read scores on the ladder's rung for how it was obtained; an
    OCR read scores on the engine's confidence for the value that is
    actually proposed, capped by whether that value validates — an
    unvalidated one is marked so and cannot reach the auto-accept band.
    Whatever the base, the signals' caps still apply last (a repeated
    header field whose occurrences disagree caps even a certain read).
    """
    if source.word_confidences is not None:
        validated = value_is_validated(target, value)
        score = ocr_confidence(word_confidences=source.word_confidences, value_validated=validated)
        if not validated:
            signals = (*signals, ReadSignal.OCR_VALUE_UNVALIDATED)
    elif source.confidence is not None:
        score = source.confidence
    else:
        score = confidence_for(origin=source.origin, signals=signals)
    cap = cap_for(signals)
    return ExtractedField(
        target=target,
        value=value,
        confidence=score if cap is None else min(score, cap),
        origin=source.origin,
        signals=signals,
        pdf_field=source.widget,
        page=source.page,
    )


def _record_absence(
    target: str, reads: Sequence[_Read], skipped: dict[str, tuple[ReadSignal, ...]]
) -> None:
    """Note why a target produced no value, when its reads say why.

    The digital shapes leave an empty box absent — nothing was read there
    and nothing claims otherwise. A scan answers explicitly (an empty
    region is an outcome), so its signal is recorded against the target
    and the review queue can say why the field is missing.
    """
    signals = tuple(dict.fromkeys(signal for read in reads for signal in read.signals))
    if signals:
        skipped.setdefault(target, signals)


def _consensus(reads: Sequence[_Read]) -> tuple[str | None, bool, _Read | None]:
    """The most common value across a target's occurrences.

    Occurrences are ordered by page (then widget) first, so a tie is won by
    the earliest reading on the form — deterministic, and the same
    disagreement the second element reports.
    """
    ordered = sorted((read for read in reads if read.value), key=lambda r: (r.page, r.widget))
    if not ordered:
        return None, False, None
    counts = Counter(read.value for read in ordered)
    winner = max(counts, key=lambda value: counts[value])
    return winner, len(counts) > 1, next(read for read in ordered if read.value == winner)


def _made_up_date(reads: Sequence[_Read]) -> tuple[date | None, tuple[ReadSignal, ...]]:
    """The filing's made-up date (context, not a master-data record)."""
    value, disagreement, _ = _consensus(reads)
    if value is None:
        return None, ()
    return date.fromisoformat(value), (
        (ReadSignal.REPEATED_FIELD_DISAGREEMENT,) if disagreement else ()
    )
