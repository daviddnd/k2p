"""The CR-draft filler: a pinned template + one ``FillPlan`` → the official
filled PDF (ADR-0004 as amended 2026-08-19 — "fields carry data, overlay
carries rendering"; scope §2.9's downloadable draft; contract §2.8).

``fill_template`` is the whole of this module: the template's path plus one
``app.domain.draft_fill.FillPlan`` in, the filled PDF's bytes out. The writer
is pure I/O — it reads the template and the embedded font and writes no row,
no audit entry and no document: the application stores what it returns
through the records registry's own write path
(``app.application.documents.register_upload``).

**The mechanism is the Phase 0 fill spike's, proven on pypdf 6.14.2**
(``research/counter-parties/cr/field-mapping/fill-spike/``), rewritten under
the house architecture:

- **Fields carry data.** Text and date values are written through pypdf's
  ``update_page_form_field_values``, which also regenerates each widget's
  ``/AP`` appearance stream.
- **Ticks resolve the widget's own on-state.** ``True`` writes the state the
  widget's ``/AP /N`` actually names — never a literal ``/On``, which a form
  that spells its states differently would silently downgrade to ``/Off`` —
  and ``False`` writes ``/Off``. A widget with no appearance states is left
  exactly as it stands when the plan asks for a tick.
- **Strikes are drawn, not clicked.** A ``STRIKE`` value becomes the field's
  non-blank ``/Opt`` entry (the form's own long-dash option), with the widget
  flagged hidden and its ``/AP`` dropped, and a 0.8pt vector line drawn across
  every widget rectangle through a merged overlay page: form renderers draw
  unreliable combo chrome, and ND2A's delete-whichever lines carry two widget
  rectangles (Chinese and English) per term. **Every other choice widget is
  hidden too** (no line drawn): the fill asks viewers to regenerate
  appearances, and a dropdown whose group the plan writes nothing into — an
  unanswered consent-signatory line — would otherwise print a ▼ on the
  artifact the client signs.
- **CJK rides an embedded overlay.** pypdf's appearance regeneration writes
  literal ``???`` for non-Latin values, and the templates' declared face
  (PMingLiU) is not embedded, so Noto Sans HK is embedded into the AcroForm's
  ``/DR`` as Type0/Identity-H — fontTools reads the cmap, the per-glyph widths
  and the descriptor values — the CJK widgets' appearances are rewritten to
  it, and their values are drawn onto the pages through the same overlay
  merge; the widgets stay in the file flagged hidden with ``/V`` preserved, so
  the data remains in the form while the rendering is print-ready. **The
  embedded face is a subset** (fontTools' subsetter, built once per fill from
  every character the package writes): the whole 12 MB face in every document
  is what put a continuation-sheet package over ``MAX_UPLOAD_BYTES``, and a
  released package must store through the records registry
  (``app.domain.documents``). The subset's own glyph order and widths are what
  the ``/W`` array and the ``CIDToGIDMap`` are built from, so the map and the
  embedded bytes cannot disagree. A character the font does not carry
  (Simplified Chinese — Noto Sans HK is Traditional/HK-only) raises
  ``GlyphUnavailableError`` by name rather than printing tofu — checked
  against the *whole* face's cmap before anything is subset — and a
  deployment without a readable font raises ``DraftFontUnavailableError``
  rather than rendering a statutory form with its Chinese silently missing.
- **The package is the template entire.** Stage 6's specimen convention keeps
  every sheet and the guidance notes in the package; the one page rule this
  slice applies is the unused sheets': **a sheet page whose only writes are
  header writes prints none of them** — its BRN and made-up-date boxes stay
  empty, exactly as the official specimens leave an unused sheet's, while the
  main form's header prints on every page. ``header_fields`` names the boxes
  the form repeats as its page header (the registry's ``HDR-*`` / ``0`` items)
  and ``sheet_pages`` its schedule/continuation pages (everything after the
  page a declarant signs); the caller reads both from
  ``app.adapters.forms.registry``.

The private-pypdf dependency is real and pinned: the module needs
``PdfWriter(clone_from=...)``, the per-page field update, and pypdf's raw
dictionary/stream objects (``writer._add_object``, ``DecodedStreamObject``) to
build the embedded font and the overlay. ``pypdf==6.14.2`` is the version the
spike proved; ``backend/pyproject.toml`` carries the requirement with the
founder's sign-off.

The font asset (``backend/assets/NotoSansHK.ttf``, OFL — see
``backend/assets/README.md``) is read once per process per path, bytes and
metrics both cached (``_font_at``): a fill never re-reads the 12 MB file. The
**subset** an individual document embeds is built per fill
(``_embedding_font``), once for the whole package.
"""

from collections.abc import Collection, Iterator, Mapping, Sequence
from dataclasses import dataclass
from functools import lru_cache
from io import BytesIO
from pathlib import Path
from typing import Any, cast

from fontTools import subset
from fontTools.ttLib import TTFont
from pypdf import PdfReader, PdfWriter
from pypdf.generic import (
    ArrayObject,
    DecodedStreamObject,
    DictionaryObject,
    NameObject,
    NumberObject,
    TextStringObject,
)

from app.core.config import settings
from app.domain.draft_fill import STRIKE, FillPlan, SheetPlan

# The overlay font's resource name inside the AcroForm's /DR and in the
# appearance/content streams that draw with it (the spike's own reference).
CJK_FONT_REF = "/FCJK"

# The strike line's width in points (the spike's proven weight).
STRIKE_LINE_WIDTH = 0.8

# The annotation flag that hides a widget without removing it: the value stays
# in ``/V`` (data preserved), the viewer stops drawing the widget.
_HIDDEN_FLAG = 2

# The font size the overlay falls back to when a widget carries no /DA, and the
# floor the auto-fit never shrinks below (the spike's values).
_DEFAULT_FONT_SIZE = 12.0
_MIN_FONT_SIZE = 6.0

# The templates' declared field font, used only as a size source when a widget
# carries no /DA of its own.
_DEFAULT_DA = "/PMingLiU 12 Tf 0 g"


class DraftFontUnavailableError(RuntimeError):
    """No readable font at the resolved path (``AICS_DRAFT_FONT_PATH`` else the
    packaged asset).

    A deployment fault, refused loudly: a fill without the font would put a
    statutory form's Chinese values on the page as text nobody can see.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        super().__init__(f"the draft filler's font is not readable at {path}")


class GlyphUnavailableError(ValueError):
    """The embedded font cannot render a character the fill would draw.

    Noto Sans HK is Traditional/HK-only; a Simplified character has no glyph
    (``font_glyph_unavailable: <chars>``, contract §5.1.3). The refusal names
    the characters so the caller can convert them — script conversion, never
    translation — and confirm.
    """

    def __init__(self, characters: str) -> None:
        self.characters = characters
        super().__init__(
            "characters not covered by the embedded CJK font (Noto Sans HK is "
            f"Traditional/HK-only): {characters!r} — convert to Traditional before filing"
        )


def packaged_font_path() -> Path:
    """The font shipped with the service: ``backend/assets`` in the repo,
    ``/app/assets`` in the image (both are the package's grandparent, the
    ``app.adapters.forms.registry.forms_root`` precedent)."""
    return Path(__file__).resolve().parents[3] / "assets" / "NotoSansHK.ttf"


def draft_font_path() -> Path:
    """The configured font path, or the packaged asset when none is set."""
    configured = settings.draft_font_path.strip()
    return Path(configured) if configured else packaged_font_path()


def _read_font(path: Path | None = None) -> tuple[bytes, "_FontMetrics"]:
    """The font's bytes and parsed metrics, or ``DraftFontUnavailableError``."""
    resolved = draft_font_path() if path is None else Path(path)
    if not resolved.is_file():
        raise DraftFontUnavailableError(resolved)
    return _font_at(str(resolved))


@lru_cache(maxsize=4)
def _font_at(path: str) -> tuple[bytes, "_FontMetrics"]:
    """One font's bytes and metrics, read once per path (a 12 MB TTF each fill)."""
    return Path(path).read_bytes(), _font_metrics(path)


def fill_template(
    template: Path,
    plan: FillPlan,
    *,
    header_fields: Collection[str] = (),
    sheet_pages: Collection[int] = (),
    font_path: Path | None = None,
) -> bytes:
    """Fill one pinned template from one plan and return the PDF's bytes.

    ``header_fields`` are the boxes the form repeats as its page header and
    ``sheet_pages`` its schedule/continuation pages — both as the registry
    states them, with 1-based page numbers; a sheet page whose only writes are
    headers prints none of them (the unused-sheet convention). ``font_path``
    overrides the resolved font for a caller that holds one (tests, an embedded
    deployment); the default is ``draft_font_path()``.

    The plan's **sheet copies** are assembled here (``FillPlan.sheets``): the
    first copy of a sheet is the template's own page, filled in place with the
    rest of the form; each later copy is filled in its own pass, hard-flattened
    and inserted immediately after the page it copies, because duplicated sheet
    pages share field names and would collide in the AcroForm. A plan with no
    sheet copies is the template entire, page for page.

    Every document of the package embeds the same **subset** of the font
    (``_embedding_font``): the glyph-coverage refusals run against the whole
    face's cmap first, each document draws with the subset's metrics, and the
    embedded bytes, the ``CIDToGIDMap`` and the ``/W`` widths all come from
    that one subset.
    """
    font_bytes, metrics = _read_font(font_path)
    writer = PdfWriter(clone_from=str(template))
    widgets = _widget_index(writer)
    writes, copies = _sheet_writes(plan, widgets, header_fields, sheet_pages)
    writes = _printed_writes(writes, widgets, header_fields, sheet_pages)
    for copy in copies:
        _require_glyphs(_text_values(copy.writes), metrics)
    drawn = {
        name: value
        for name, value in _text_values(writes).items()
        if _has_cjk(value)
    }
    _require_glyphs(drawn, metrics)
    embedding = _embedding_font(font_bytes, metrics, _package_characters(writes, copies))

    _, strike_lines = _write_values(writer, widgets, writes)
    if drawn:
        _embed_cjk_font(writer, _used_characters(writes), embedding)
        _rewrite_cjk_appearances(writer, widgets, drawn, embedding.metrics)
    if drawn or strike_lines:
        _flatten_overlay(writer, widgets, drawn, strike_lines, embedding.metrics)
    if not copies:
        return _to_bytes(writer)

    inserts: dict[int, list[bytes]] = {}
    for copy in copies:
        inserts.setdefault(copy.page, []).append(
            _flattened_sheet(template, copy, embedding=embedding)
        )
    return _assemble(_to_bytes(writer), inserts)


# --- The plan's sheet copies -------------------------------------------------


@dataclass(frozen=True)
class _SheetCopy:
    """One duplicate sheet to materialise: its plan, its page, its printed writes.

    ``page`` is the 0-based template page the copy prints at, and ``writes`` is
    the plan's complete value set as the package prints it (``_printed_writes``
    applied) — read once here, so the glyph check, the embedding subset and the
    flattening all work from the same set.
    """

    plan: SheetPlan
    page: int
    writes: Mapping[str, object]


def _sheet_writes(
    plan: FillPlan,
    widgets: Mapping[str, Sequence[tuple[int, Any]]],
    header_fields: Collection[str],
    sheet_pages: Collection[int],
) -> tuple[dict[str, object], list[_SheetCopy]]:
    """The plan's writes, with each used sheet's first copy merged in.

    The first ``SheetPlan`` of a sheet is the template's own page — its fields
    are printed in place with the rest of the form, so they join the main
    write set — and each later one is a duplicate to materialise: this returns
    those, in the plan's (package) order. A field the plan writes both on the
    main form and on a sheet is refused: one box has one value, and only the
    plan knows which it meant.
    """
    writes = dict(plan.fields)
    in_place: set[str] = set()
    copies: list[_SheetCopy] = []
    for sheet_plan in plan.sheets:
        if sheet_plan.sheet in in_place:
            copies.append(
                _SheetCopy(
                    plan=sheet_plan,
                    page=_sheet_page(sheet_plan, widgets),
                    writes=_printed_writes(
                        dict(sheet_plan.fields), widgets, header_fields, sheet_pages
                    ),
                )
            )
            continue
        in_place.add(sheet_plan.sheet)
        merged = set(sheet_plan.fields) & set(writes)
        if merged:
            raise ValueError(
                f"sheet {sheet_plan.sheet!r}: the plan writes "
                f"{', '.join(sorted(merged))} both on the main form and on the sheet"
            )
        writes.update(sheet_plan.fields)
    return writes, copies


def _sheet_page(sheet_plan: SheetPlan, widgets: Mapping[str, Sequence[tuple[int, Any]]]) -> int:
    """The template page one sheet plan copies, read from the fields it prints."""
    pages = {page for field in sheet_plan.fields for page, _obj in widgets.get(field, ())}
    if len(pages) != 1:
        raise ValueError(
            f"sheet {sheet_plan.sheet!r}: its fields sit on {len(pages)} template pages, not one"
        )
    return pages.pop()


def _flattened_sheet(
    template: Path,
    copy: _SheetCopy,
    *,
    embedding: "_EmbeddedFont",
) -> bytes:
    """One duplicate sheet copy, filled, hard-flattened and widget-free.

    The copy is filled in its own pass over a fresh clone of the template with
    the sheet plan's complete value set — the same write steps as the main
    fill, so a copy renders exactly as the template page would — and then made
    flat: **every** valued text widget is drawn through the overlay (not only
    the CJK ones; the widgets are about to be stripped and the overlay is what
    the page will print), each ticked checkbox's own appearance stream is drawn
    into its rectangle, a struck term keeps its drawn line, and the page's
    widgets are removed. A stripped copy can be inserted beside the template's
    own page without the two colliding in the AcroForm: duplicated pages share
    field names, and same-named fields share one ``/V``.

    The glyph check has already run against the whole face's cmap
    (``fill_template``); the document draws with the fill's subset, and embeds
    it only when it has text to draw.
    """
    writer = PdfWriter(clone_from=str(template))
    widgets = _widget_index(writer)
    text_values, strike_lines = _write_values(writer, widgets, copy.writes)
    appearances = _tick_appearances(widgets, copy.writes, copy.page)
    if text_values:
        _embed_cjk_font(writer, _used_characters(copy.writes), embedding)
    _flatten_overlay(writer, widgets, text_values, strike_lines, embedding.metrics, appearances)
    _strip_page_widgets(writer, copy.page)
    return _to_bytes(writer)


def _text_values(writes: Mapping[str, object]) -> dict[str, str]:
    """A write set's text and date values (ticks and strikes render otherwise)."""
    return {
        name: str(value)
        for name, value in writes.items()
        if not isinstance(value, bool) and value != STRIKE
    }


def _write_values(
    writer: PdfWriter,
    widgets: Mapping[str, Sequence[tuple[int, Any]]],
    writes: Mapping[str, object],
) -> tuple[dict[str, str], dict[int, list[tuple[float, float, float]]]]:
    """Carry one write set's data into a document and return its text and lines.

    Text and dates through pypdf's per-page field update (appearances
    regenerated), ticks through each widget's own on-state, strikes as the
    field's ``/Opt`` value with a line per widget rectangle, and every choice
    widget the write set does not name hidden — the whole of what the document
    needs before the overlay draws what a form renderer cannot.
    """
    text_values = _text_values(writes)
    ticks = {name: bool(value) for name, value in writes.items() if isinstance(value, bool)}
    strikes = [name for name, value in writes.items() if value == STRIKE]
    _write_text(writer, widgets, text_values)
    _apply_ticks(widgets, ticks)
    strike_lines = _strike_choices(widgets, strikes)
    _hide_untouched_choices(widgets, written=set(writes))
    return text_values, strike_lines


def _tick_appearances(
    widgets: Mapping[str, Sequence[tuple[int, Any]]],
    writes: Mapping[str, object],
    page_index: int,
) -> dict[int, list[tuple[tuple[float, float, float, float], Any]]]:
    """The on-state appearance streams of a page's ticked checkboxes, by page.

    A flattened copy has no widgets, so a tick is not rendered by the widget any
    more: its own ``/AP /N`` on-state stream is returned for the overlay to draw
    (as an XObject at the widget's rectangle). A widget with no appearance
    states has nothing to draw — the main fill leaves it as it stands, and so
    does the copy.
    """
    placements: dict[int, list[tuple[tuple[float, float, float, float], Any]]] = {}
    for name, value in writes.items():
        if value is not True:
            continue
        for page, obj in widgets.get(name, ()):
            if page != page_index:
                continue
            state = _on_state(obj)
            appearance = obj.get("/AP")
            if state is None or appearance is None:
                continue
            normal = appearance.get_object().get("/N")
            if not isinstance(normal, DictionaryObject) or state not in normal:
                continue
            placements.setdefault(page, []).append((_rect(obj), normal[state]))
    return placements


def _strip_page_widgets(writer: PdfWriter, page_index: int) -> None:
    """Remove a page's widget annotations (the hard flatten before assembly).

    The field dictionaries stay in the AcroForm with no widget left to show
    them; the page prints the overlay's drawing alone, and no field name of the
    copy can collide with the template page's own fields.
    """
    page = writer.pages[page_index]
    kept = ArrayObject(
        [
            annot
            for annot in _array(page.get("/Annots"))
            if annot.get_object().get("/Subtype") != "/Widget"
        ]
    )
    page[NameObject("/Annots")] = kept


def _assemble(main: bytes, inserts: Mapping[int, Sequence[bytes]]) -> bytes:
    """The package: the filled template, with each duplicate after its page.

    ``inserts`` maps a 0-based template page index to the flattened copies that
    follow it, in order. Pages are appended in ranges so the source reader is
    cloned once per range — the field objects keep their identity in the
    output's AcroForm, which is what makes the assembled package's values read
    back — and the guidance notes (the template's last pages) close the package
    by construction, since no copy follows the last sheet.
    """
    output = PdfWriter()
    source = PdfReader(BytesIO(main))
    total = len(source.pages)
    start = 0
    for page_index in sorted(inserts):
        output.append(source, pages=list(range(start, page_index + 1)))
        for copy in inserts[page_index]:
            output.append(PdfReader(BytesIO(copy)), pages=[page_index])
        start = page_index + 1
    if start < total:
        output.append(source, pages=list(range(start, total)))
    return _to_bytes(output)


# --- The plan, as the page prints it ----------------------------------------


def _printed_writes(
    writes: Mapping[str, object],
    widgets: Mapping[str, Sequence[tuple[int, Any]]],
    header_fields: Collection[str],
    sheet_pages: Collection[int],
) -> dict[str, object]:
    """The writes, minus the header boxes of the sheets they print nothing else on.

    Stage 6's specimen convention: an unused sheet stays in the package blank,
    with its date and BRN headers left empty, while the main form prints its
    header on every page — used or not — and a used sheet prints both. The
    header items write every occurrence (the form repeats one box on every
    page), so the assembly is where that convention is applied: a **sheet**
    page whose writes are all header writes prints none of them. A used sheet's
    own copy carries its headers in its sheet plan, so the rule never touches
    one.

    ``sheet_pages`` arrives in the registry's 1-based numbering; the widget index
    is keyed by zero-based page index, so the two are related once, here.
    """
    printed = dict(writes)
    headers = set(header_fields) & set(printed)
    if not headers or not sheet_pages:
        return printed
    for page in sorted({number - 1 for number in sheet_pages}):
        on_page = [
            name
            for name in printed
            if any(index == page for index, _obj in widgets.get(name, ()))
        ]
        if on_page and all(name in headers for name in on_page):
            for name in on_page:
                del printed[name]
    return printed


# --- Writing the values ------------------------------------------------------


def _write_text(
    writer: PdfWriter,
    widgets: Mapping[str, Sequence[tuple[int, Any]]],
    values: Mapping[str, str],
) -> None:
    """Text and date values, per page, through pypdf's field update.

    Only pages that carry a written widget are handed to pypdf: a page without
    annotations (the guidance-note pages) would otherwise log its "no fields to
    update" warning on every fill. The call regenerates each written widget's
    ``/AP`` appearance stream, which is what makes the value the form prints.
    """
    for page_index in sorted({page for name in values for page, _obj in widgets.get(name, ())}):
        page = writer.pages[page_index]
        page_values = {
            name: value
            for name, value in values.items()
            if any(index == page_index for index, _obj in widgets.get(name, ()))
        }
        if page_values:
            writer.update_page_form_field_values(page, page_values)


def _apply_ticks(
    widgets: Mapping[str, Sequence[tuple[int, Any]]], ticks: Mapping[str, bool]
) -> None:
    """Tick and untick checkboxes by the widget's own on-state.

    The on-state is read from the widget's ``/AP /N`` keys — the state the
    template actually draws ticks with — and written to the widget's ``/AS``
    and to its field's ``/V``, the pair every viewer reads. A widget with no
    appearance states has nothing to tick: a planned tick leaves it exactly as
    it stands rather than writing ``/Off`` (or a state nobody can render), and
    an explicit untick still writes ``/Off``, which every widget can show.
    """
    for name, on in ticks.items():
        for _page, obj in widgets.get(name, ()):
            state = _on_state(obj)
            if on and state is None:
                continue
            target = NameObject(state) if on and state is not None else NameObject("/Off")
            obj[NameObject("/AS")] = target
            _set_value(obj, target)


def _strike_choices(
    widgets: Mapping[str, Sequence[tuple[int, Any]]], names: Sequence[str]
) -> dict[int, list[tuple[float, float, float]]]:
    """Strike out the delete-whichever lines and return the lines to draw.

    Each named field takes its non-blank ``/Opt`` entry as its value (the
    form's own long-dash option — the semantic value stays queryable), the
    widget is hidden so a viewer does not draw combo chrome over the term, and
    one line per widget rectangle is collected: ND2A's dual-rect dropdowns draw
    the Chinese and English lines of one term as two widgets under one name.
    """
    lines: dict[int, list[tuple[float, float, float]]] = {}
    for name in names:
        option = None
        for page_index, obj in widgets.get(name, ()):
            if option is None:
                option = _strike_option(obj)
            _set_value(obj, TextStringObject(option))
            obj[NameObject("/F")] = NumberObject(_HIDDEN_FLAG)
            obj.pop(NameObject("/AP"), None)
            left, bottom, right, top = _rect(obj)
            y = (bottom + top) / 2
            lines.setdefault(page_index, []).append((left + 1, y, right - 1))
    return lines


def _hide_untouched_choices(
    widgets: Mapping[str, Sequence[tuple[int, Any]]], written: Collection[str]
) -> None:
    """Hide every choice widget the plan does not write, so no combo chrome prints.

    A form renderer draws a ▼ over *any* dropdown once the file asks for
    appearances to be regenerated — including a delete-whichever group the plan
    writes nothing at all into, whose chrome would print on the artifact the
    client signs. The spike hides every choice widget its values name; the ones
    the plan leaves alone are hidden here for the same reason. No line is drawn
    (that is a struck field's) and ``/V`` keeps whatever the template states.
    """
    for name, entries in widgets.items():
        if name in written:
            continue
        for _page, obj in entries:
            if not _is_choice(obj):
                continue
            obj[NameObject("/F")] = NumberObject(_HIDDEN_FLAG)
            obj.pop(NameObject("/AP"), None)


def _set_value(obj: DictionaryObject, value: Any) -> None:
    """Write a field's value where pypdf's own field update puts it.

    On these templates every page's box is a kid of a shared group field
    (``cb_2_P`` holds pages 1-7's checkboxes; ``Dropdown_2_P`` holds one
    delete-whichever term per page), and each kid carries its own ``/FT`` and
    ``/T`` — so the kid is the field pypdf writes and the group must not take
    the value: one page's tick would otherwise read as every page's.
    """
    holder = obj if obj.get("/FT") is not None and obj.get("/T") is not None else _holder(obj)
    holder[NameObject("/V")] = value


def _to_bytes(writer: PdfWriter) -> bytes:
    """The written PDF, off a buffer (the writer's own file target is a path)."""
    buffer = BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


# --- The widget index --------------------------------------------------------


def _widget_index(writer: PdfWriter) -> dict[str, list[tuple[int, DictionaryObject]]]:
    """Every widget annotation, keyed by its fully-qualified field name.

    The ``/Parent`` chain is walked for the name, so a kid widget of a parent
    group resolves to its full dotted name; several widgets share one name on
    these templates (the repeated page headers, the dual-rect dropdowns).
    """
    index: dict[str, list[tuple[int, DictionaryObject]]] = {}
    for page_index, page in enumerate(writer.pages):
        for annot in _array(page.get("/Annots")):
            obj = annot.get_object()
            if obj.get("/Subtype") != "/Widget":
                continue
            index.setdefault(_qualified_name(obj), []).append((page_index, obj))
    return index


def _qualified_name(obj: DictionaryObject) -> str:
    parts: list[str] = []
    node = obj
    while True:
        name = node.get("/T")
        if name:
            parts.append(str(name))
        parent = node.get("/Parent")
        if parent is None:
            break
        node = parent.get_object()
    return ".".join(reversed(parts))


def _holder(obj: DictionaryObject) -> DictionaryObject:
    """The field dictionary a value belongs on: the widget's parent, else itself."""
    parent = obj.get("/Parent")
    return parent.get_object() if parent is not None else obj


def _rect(obj: DictionaryObject) -> tuple[float, float, float, float]:
    """The widget's rectangle, as four plain floats."""
    left, bottom, right, top = (float(value) for value in cast(ArrayObject, obj["/Rect"]))
    return left, bottom, right, top


def _on_state(obj: DictionaryObject) -> str | None:
    """The widget's ticked state name, from its ``/AP /N`` keys.

    The template decides what a tick is called (``/On``, ``/Yes``, a named
    state); the writer never assumes. ``None`` means the widget carries no
    appearance states at all.
    """
    appearance = obj.get("/AP")
    if appearance is None:
        return None
    normal = appearance.get_object().get("/N")
    if not isinstance(normal, DictionaryObject):
        return None
    for key in normal:
        if str(key) != "/Off":
            return str(key)
    return None


def _strike_option(obj: DictionaryObject) -> str:
    """The field's non-blank ``/Opt`` entry — the form's strike-line option."""
    node: DictionaryObject | None = obj
    while node is not None:
        options = node.get("/Opt")
        if options:
            strike = next((str(entry) for entry in options if str(entry).strip()), None)
            if strike is None:
                break
            return strike
        parent = node.get("/Parent")
        node = parent.get_object() if parent is not None else None
    raise ValueError(
        f"{_qualified_name(obj)!r} has no non-blank /Opt strike option; the registry's "
        "delete-whichever group is not the choice field it was planned as"
    )


def _is_choice(obj: DictionaryObject) -> bool:
    """Whether the widget belongs to a choice (dropdown) field."""
    node: DictionaryObject | None = obj
    while node is not None:
        if node.get("/Opt"):
            return True
        parent = node.get("/Parent")
        node = parent.get_object() if parent is not None else None
    return False


def _array(value: Any) -> Iterator[Any]:
    """The entries of a PDF array, resolving an indirect reference."""
    if value is None:
        return
    resolved = value.get_object() if hasattr(value, "get_object") else value
    yield from resolved


# --- The embedded font -------------------------------------------------------


@dataclass(frozen=True)
class _FontMetrics:
    """One font's parsed metrics (cached per path: a 12 MB TTF is read once).

    Built from the whole face for the glyph-coverage refusals and from the
    **subset** for drawing and embedding (``_EmbeddedFont``); the widths and
    the glyph order are the subset's own, which is what keeps the
    ``CIDToGIDMap`` honest.
    """

    glyph_order: tuple[str, ...]
    cmap: Mapping[int, str]
    advances: Mapping[int, int]
    units_per_em: int
    bbox: tuple[int, int, int, int]
    ascent: int
    descent: int
    cap_height: int
    italic_angle: int

    def em_width(self, character: str) -> float:
        """The character's advance in em units (0.5 for one the font lacks)."""
        advance = self.advances.get(ord(character))
        return 0.5 if advance is None else advance / self.units_per_em


@dataclass(frozen=True)
class _EmbeddedFont:
    """The face one fill embeds: a subset of the packaged font, and its metrics.

    The subset is built once per fill from every character the **package**
    writes — the main form's values and every sheet copy's — so each document
    embeds the glyphs it draws and nothing else. Embedding the whole face in
    every document is what put a continuation-sheet package over
    ``MAX_UPLOAD_BYTES``.
    """

    data: bytes
    metrics: _FontMetrics


def _metrics_of(font: TTFont) -> _FontMetrics:
    """One (whole or subset) TTFont's metrics."""
    units_per_em = int(font["head"].unitsPerEm)
    cmap = {code: name for code, name in font.getBestCmap().items()}
    hmtx = font["hmtx"]
    order = tuple(font.getGlyphOrder())
    head, hhea, os2, post = font["head"], font["hhea"], font["OS/2"], font["post"]
    return _FontMetrics(
        glyph_order=order,
        cmap=cmap,
        advances={code: int(hmtx[name][0]) for code, name in cmap.items()},
        units_per_em=units_per_em,
        bbox=(int(head.xMin), int(head.yMin), int(head.xMax), int(head.yMax)),
        ascent=int(hhea.ascent),
        descent=int(hhea.descent),
        cap_height=int(getattr(os2, "sCapHeight", hhea.ascent)),
        italic_angle=int(post.italicAngle),
    )


@lru_cache(maxsize=4)
def _font_metrics(path: str) -> _FontMetrics:
    return _metrics_of(TTFont(path))


def _has_cjk(value: str) -> bool:
    """Whether a value carries a character outside Latin-1 (needs the overlay)."""
    return any(ord(character) > 0xFF for character in value)


def _used_characters(writes: Mapping[str, object]) -> set[str]:
    """Every character of every value the plan writes.

    The embedding covers all of them, not only the CJK ones: the continuation
    sheets' flattening (F4c) draws Latin values through the same face, and the
    spike's first overflow build printed tofu for exactly that reason.
    """
    return {
        character
        for value in writes.values()
        if not isinstance(value, bool) and value != STRIKE
        for character in str(value)
    }


def _package_characters(
    writes: Mapping[str, object], copies: Sequence["_SheetCopy"]
) -> set[str]:
    """Every character the package's documents will draw.

    The main form and each flattened duplicate are separate documents with
    their own embedded face, so the fill's subset has to cover both — the
    spike's own finding ("embed coverage must include ALL values' characters").
    """
    characters = _used_characters(writes)
    for copy in copies:
        characters |= _used_characters(copy.writes)
    return characters


def _embedding_font(
    font_bytes: bytes, metrics: _FontMetrics, characters: Collection[str]
) -> _EmbeddedFont:
    """Subset the packaged face to one fill's characters (fontTools).

    A released package is stored through the records registry, whose per-file
    cap (``app.domain.documents.MAX_UPLOAD_BYTES``) the whole ~12 MB face per
    document blows once a continuation sheet duplicates a page — so each fill
    embeds a subset covering exactly its written characters, and the
    ``CIDToGIDMap`` and ``/W`` widths are read back from that same subset (the
    appearance streams address glyphs through the map, so the map must match
    the embedded bytes). Fills that write no character never embed a font at
    all and get the face back unchanged.
    """
    if not characters:
        return _EmbeddedFont(data=font_bytes, metrics=metrics)
    font = TTFont(BytesIO(font_bytes))
    options = subset.Options()
    options.layout_features = []  # Identity-H drawing needs no layout tables
    subsetter = subset.Subsetter(options=options)
    subsetter.populate(unicodes={ord(character) for character in characters})
    subsetter.subset(font)
    buffer = BytesIO()
    font.save(buffer)
    return _EmbeddedFont(data=buffer.getvalue(), metrics=_metrics_of(font))


def _require_glyphs(drawn: Mapping[str, str], metrics: _FontMetrics) -> None:
    """Refuse the fill when a character the overlay will draw has no glyph.

    Checked against the **whole** face's cmap (``fill_template`` runs it before
    any subset exists): subsetting would silently drop a character the font
    does not carry, and the refusal has to fire instead.
    """
    missing = sorted(
        {
            character
            for value in drawn.values()
            for character in value
            if ord(character) not in metrics.cmap
        }
    )
    if missing:
        raise GlyphUnavailableError("".join(missing))


def _embed_cjk_font(
    writer: PdfWriter, characters: Collection[str], font: _EmbeddedFont
) -> None:
    """Embed the overlay font as Type0/Identity-H under the AcroForm's ``/DR``.

    The ``/FontFile2`` stream is the fill's **subset**; the ``/W`` array's real
    per-glyph advances (``/DW 1000`` alone makes every glyph 1em wide and Latin
    overflows) and the ``CIDToGIDMap`` are read from that same subset, so an
    appearance or overlay stream may address a glyph by its code point.
    """
    metrics = font.metrics
    covered = sorted(code for code in {ord(c) for c in characters} if code in metrics.cmap)
    cid_to_gid = bytearray((max(covered) + 1) * 2 if covered else 2)
    w_entries: list[Any] = []
    for code in covered:
        glyph = metrics.cmap[code]
        cid_to_gid[code * 2 : code * 2 + 2] = metrics.glyph_order.index(glyph).to_bytes(2, "big")
        w_entries += [
            NumberObject(code),
            ArrayObject(
                [NumberObject(round(metrics.advances[code] / metrics.units_per_em * 1000))]
            ),
        ]

    font_file = _stream(writer, font.data, {"/Length1": NumberObject(len(font.data))})
    cid_map = _stream(writer, bytes(cid_to_gid))
    descriptor = writer._add_object(
        DictionaryObject(
            {
                NameObject("/Type"): NameObject("/FontDescriptor"),
                NameObject("/FontName"): NameObject("/NotoSansHK"),
                NameObject("/Flags"): NumberObject(4),
                NameObject("/FontBBox"): ArrayObject(
                    [NumberObject(value) for value in metrics.bbox]
                ),
                NameObject("/ItalicAngle"): NumberObject(metrics.italic_angle),
                NameObject("/Ascent"): NumberObject(metrics.ascent),
                NameObject("/Descent"): NumberObject(metrics.descent),
                NameObject("/CapHeight"): NumberObject(metrics.cap_height),
                NameObject("/StemV"): NumberObject(80),
                NameObject("/FontFile2"): font_file,
            }
        )
    )
    cid_font = writer._add_object(
        DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/CIDFontType2"),
                NameObject("/BaseFont"): NameObject("/NotoSansHK"),
                NameObject("/CIDSystemInfo"): DictionaryObject(
                    {
                        NameObject("/Registry"): TextStringObject("Adobe"),
                        NameObject("/Ordering"): TextStringObject("Identity"),
                        NameObject("/Supplement"): NumberObject(0),
                    }
                ),
                NameObject("/FontDescriptor"): descriptor,
                NameObject("/DW"): NumberObject(1000),
                NameObject("/W"): ArrayObject(w_entries),
                NameObject("/CIDToGIDMap"): cid_map,
            }
        )
    )
    type0 = writer._add_object(
        DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type0"),
                NameObject("/BaseFont"): NameObject("/NotoSansHK"),
                NameObject("/Encoding"): NameObject("/Identity-H"),
                NameObject("/DescendantFonts"): ArrayObject([cid_font]),
            }
        )
    )
    fonts = _acro_form_resources(writer, "/Font")
    fonts[NameObject(CJK_FONT_REF)] = type0


def _stream(writer: PdfWriter, data: bytes, extra: Mapping[str, Any] | None = None) -> Any:
    stream = DecodedStreamObject()
    stream.set_data(data)
    for key, value in (extra or {}).items():
        stream[NameObject(key)] = value
    return writer._add_object(stream)


def _acro_form_resources(writer: PdfWriter, kind: str) -> DictionaryObject:
    """The AcroForm ``/DR``'s named sub-dictionary, created when absent."""
    root: Any = writer._root_object
    acro_form: DictionaryObject = root["/AcroForm"].get_object()
    resources = acro_form.get("/DR")
    if resources is None:
        resources = DictionaryObject()
        acro_form[NameObject("/DR")] = resources
    else:
        resources = resources.get_object()
    section = resources.get(kind)
    if section is None:
        section = DictionaryObject()
        resources[NameObject(kind)] = section
    else:
        section = section.get_object()
    return cast(DictionaryObject, section)


def _rewrite_cjk_appearances(
    writer: PdfWriter,
    widgets: Mapping[str, Sequence[tuple[int, Any]]],
    drawn: Mapping[str, str],
    metrics: _FontMetrics,
) -> None:
    """Give the CJK widgets a rendering an editable copy can show.

    The widget's ``/DA`` is repointed at the embedded font and its ``/AP /N``
    replaced with a stream that draws the value as UTF-16BE hex in that font —
    pypdf's regenerated appearance is literal ``???`` for anything outside
    Latin-1. **The appearance stream is transient on this path:**
    ``_flatten_overlay`` runs straight after and pops it, so what survives on a
    widget whose page is drawn over is the ``/DA`` repoint (and the fitted size
    recorded here); the rendering itself is the overlay's.
    """
    fonts = _acro_form_resources(writer, "/Font")
    for name, text in drawn.items():
        for _page, obj in _cjk_widgets(widgets, name):
            left, bottom, right, top = _rect(obj)
            size = _fit_size(right - left, text, _font_size(obj), metrics)
            declaration = TextStringObject(f"{CJK_FONT_REF} {size} Tf 0 g")
            obj[NameObject("/DA")] = declaration
            holder = _holder(obj)
            if "/DA" in holder:
                holder[NameObject("/DA")] = declaration
            stream = DecodedStreamObject()
            stream.set_data(
                f"q\n/Tx BMC \nq\n2 1 {right - left} {top - bottom} re\nW\nBT\n"
                f"{CJK_FONT_REF} {size} Tf 0 g\n"
                f"{_text_x(right - left, text, size, metrics)} {top - bottom - size * 0.55} Td\n"
                f"<{text.encode('utf-16-be').hex().upper()}> Tj\nET\nQ\nEMC\nQ\n".encode()
            )
            stream[NameObject("/Resources")] = DictionaryObject(
                {NameObject("/Font"): fonts}
            )
            obj[NameObject("/AP")] = DictionaryObject(
                {NameObject("/N"): writer._add_object(stream)}
            )


def _cjk_widgets(
    widgets: Mapping[str, Sequence[tuple[int, Any]]], name: str
) -> list[tuple[int, DictionaryObject]]:
    """The text widgets under one field name (choice fields render separately)."""
    return [
        (page, obj)
        for page, obj in widgets.get(name, ())
        if not _is_choice(obj) and obj.get("/Subtype") == "/Widget"
    ]


# --- The overlay -------------------------------------------------------------


def _flatten_overlay(
    writer: PdfWriter,
    widgets: Mapping[str, Sequence[tuple[int, Any]]],
    drawn: Mapping[str, str],
    strike_lines: Mapping[int, Sequence[tuple[float, float, float]]],
    metrics: _FontMetrics,
    appearances: Mapping[int, Sequence[tuple[tuple[float, float, float, float], Any]]]
    | None = None,
) -> None:
    """Merge one overlay page per touched page, then hide what it replaced.

    The templates' legacy content streams leave a polluted graphics state
    behind — text appended to ``/Contents`` lands at transformed coordinates —
    so the values and lines are drawn on a blank overlay page merged with
    ``merge_page`` (an XObject inside a balanced q/Q). The widgets stay in the
    file flagged hidden with ``/V`` intact, so the form keeps the data and the
    page carries the rendering.

    ``appearances`` maps a page to the ticked checkboxes' own on-state streams,
    drawn into their rectangles: a flattened copy has no widget left to render a
    tick, so the stream the widget would have shown is drawn instead.
    """
    cjk_pages: dict[int, list[tuple[DictionaryObject, str]]] = {}
    for name, text in drawn.items():
        for widget_page, obj in _cjk_widgets(widgets, name):
            cjk_pages.setdefault(widget_page, []).append((obj, text))
    ticks = appearances or {}
    for page_index in sorted(set(cjk_pages) | set(strike_lines) | set(ticks)):
        page = writer.pages[page_index]
        overlay = writer.add_blank_page(
            width=float(page.mediabox.width), height=float(page.mediabox.height)
        )
        parts: list[str] = []
        for obj, text in cjk_pages.get(page_index, []):
            left, bottom, right, top = _rect(obj)
            width, height = right - left, top - bottom
            size = _fit_size(width, text, _font_size(obj), metrics)
            x = left + _text_x(width, text, size, metrics)
            y = bottom + max(2.0, (height - size) / 2 + size * 0.2)
            parts.append(
                f"BT {CJK_FONT_REF} {size} Tf 0 g {x} {y} Td "
                f"<{text.encode('utf-16-be').hex().upper()}> Tj ET"
            )
        for x0, y, x1 in strike_lines.get(page_index, ()):
            parts.append(f"{STRIKE_LINE_WIDTH} w 0 g {x0} {y} m {x1} {y} l S")
        xobjects = DictionaryObject()
        for number, (rect, appearance) in enumerate(ticks.get(page_index, ()), start=1):
            name = NameObject(f"/Ftk{number}")
            xobjects[name] = appearance
            parts.append(f"q 1 0 0 1 {rect[0]} {rect[1]} cm {name} Do Q")
        stream = _stream(writer, "\n".join(parts).encode())
        resources = DictionaryObject()
        if cjk_pages.get(page_index):
            resources[NameObject("/Font")] = DictionaryObject(
                {NameObject(CJK_FONT_REF): _acro_form_resources(writer, "/Font")[CJK_FONT_REF]}
            )
        if len(xobjects):
            resources[NameObject("/XObject")] = xobjects
        overlay[NameObject("/Resources")] = resources
        overlay[NameObject("/Contents")] = stream
        page.merge_page(overlay, over=True)
        writer.remove_page(overlay)
        for obj, _text in cjk_pages.get(page_index, []):
            obj[NameObject("/F")] = NumberObject(_HIDDEN_FLAG)
            obj.pop(NameObject("/AP"), None)


# --- Fitting text to its box -------------------------------------------------


def _font_size(obj: DictionaryObject) -> float:
    """The size a widget's own ``/DA`` declares (the template's default else)."""
    holder = _holder(obj)
    declaration = obj.get("/DA") or holder.get("/DA")
    parts = str(declaration or _DEFAULT_DA).split()
    try:
        return max(_MIN_FONT_SIZE, float(parts[1]))
    except (IndexError, ValueError):
        return _DEFAULT_FONT_SIZE


def _est_width(text: str, size: float, metrics: _FontMetrics) -> float:
    """The rendered width, from the embedded font's real advances."""
    return sum(metrics.em_width(character) for character in text) * size


def _fit_size(rect_width: float, text: str, size: float, metrics: _FontMetrics) -> float:
    """Shrink the size (down to the floor) so the text fits the box's width."""
    estimate = _est_width(text, size, metrics)
    if estimate <= rect_width - 4:
        return size
    return max(_MIN_FONT_SIZE, size * (rect_width - 4) / estimate)


def _text_x(rect_width: float, text: str, size: float, metrics: _FontMetrics) -> float:
    """The left offset that centres the text (CJK full-width, Latin half)."""
    return max(2.0, (rect_width - _est_width(text, size, metrics)) / 2)


__all__ = [
    "DraftFontUnavailableError",
    "GlyphUnavailableError",
    "draft_font_path",
    "fill_template",
    "packaged_font_path",
]
