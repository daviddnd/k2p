"""The export flattener: the release's filled PDF → the filing's flat PDF
(ADR-0004 — "flatten only at the final export step"; contract §2.8, §2.12,
§5.1; the F4c hard-flatten machinery, reused).

``fill_template`` deliberately releases an **editable** package: the widgets
stay live so the signer and the registry keep a working form, and CJK values
ride the embedded-font overlay with the widgets hidden and ``/V`` preserved
(``app.adapters.pdf.filler``'s own docstring). The filing itself is another
question: what leaves the building must print what it says and carry no live
form fields — the CR's paper channel and the e-Registry upload both read a
flat document. So the export fills exactly as the release did (the same
plan, the same template, the same writer — the release path's own seam) and
**this module** then makes the package flat, reusing the filler's proven
machinery rather than re-implementing it:

- **every valued text widget is drawn through the overlay** — Latin values
  too, not only the CJK ones, because the widgets are about to be stripped
  and the overlay is what the page will print (``filler._flattened_sheet``'s
  own rule);
- **each ticked checkbox's own on-state appearance stream is drawn into its
  rectangle** — a flat page has no widget left to render the tick;
- **struck terms and hidden chrome need nothing**: the release's fill
  already drew their vector lines into the page content and hid the widgets;
- **the CJK embedding is preserved**: the export reuses the same
  Type0/Identity-H subset machinery (the package's characters, the
  ``CIDToGIDMap`` and the ``/W`` widths all read from that one subset), so
  the flat document draws Chinese exactly as the filled one did;
- **every page's widgets are stripped and the AcroForm dropped** — no field
  name survives for a viewer to regenerate, and ``pypdf.get_fields()`` reads
  nothing.

The function refuses **nothing the release did not already refuse**: the
fill — plan, template, glyph coverage — ran first and answered every
named refusal before this flatten ever starts, so the only errors that can
surface here are the filler's own deployment faults (a missing font), which
propagate by the same doctrine (raised, not smoothed, with nothing written).
"""

from io import BytesIO
from pathlib import Path
from typing import Any

from pypdf import PdfWriter
from pypdf.generic import DictionaryObject, NameObject

from app.adapters.pdf import filler


def flatten_filled_pdf(pdf_bytes: bytes, *, font_path: Path | None = None) -> bytes:
    """One filled package's bytes → the flat filing document's bytes.

    The values are read back from the widgets' ``/V`` (the data the release
    wrote), every valued text field is drawn through the overlay, every
    ticked checkbox's own appearance stream is drawn into its rectangle,
    and the widgets are stripped page by page — the F4c sheet-copy flatten,
    applied to the whole package. The AcroForm dictionary goes with them:
    no widget survived, so the form would only name dead fields.
    """
    font_bytes, metrics = filler._read_font(font_path)
    writer = PdfWriter(clone_from=BytesIO(pdf_bytes))
    widgets = filler._widget_index(writer)
    text_values = _valued_texts(widgets)
    filler._require_glyphs(text_values, metrics)
    characters = {character for value in text_values.values() for character in value}
    draw_metrics = metrics
    if characters:
        embedding = filler._embedding_font(font_bytes, metrics, characters)
        filler._embed_cjk_font(writer, characters, embedding)
        draw_metrics = embedding.metrics
    appearances = _ticked_appearances(widgets)
    filler._flatten_overlay(writer, widgets, text_values, {}, draw_metrics, appearances)
    for page_index in range(len(writer.pages)):
        filler._strip_page_widgets(writer, page_index)
    if "/AcroForm" in writer._root_object:
        writer._root_object.pop(NameObject("/AcroForm"))
    return filler._to_bytes(writer)


def _valued_texts(
    widgets: dict[str, list[tuple[int, DictionaryObject]]],
) -> dict[str, str]:
    """Every field's drawn value, keyed by fully-qualified name.

    A widget contributes its ``/V`` when it holds a non-blank text and is
    neither a choice field (strikes and unanswered groups render through the
    page content the fill already drew — their chrome stays hidden) nor a
    checkbox (a ticked box renders through its own appearance stream,
    ``_ticked_appearances``). Blank boxes stay blank: nothing is invented
    for a field the fill left empty.
    """
    values: dict[str, str] = {}
    for name, entries in widgets.items():
        for _page, obj in entries:
            if filler._is_choice(obj) or filler._on_state(obj) is not None:
                continue
            # The fill writes /V where pypdf's own update puts it — the
            # widget itself when it carries /FT and /T, else its group
            # parent (``filler._set_value``'s rule); the flatten reads both.
            value = obj.get("/V")
            if value is None:
                value = filler._holder(obj).get("/V")
            if value is None or isinstance(value, bool):
                continue
            text = str(value)
            if not text.strip():
                continue
            values.setdefault(name, text)
            break
    return values


def _ticked_appearances(
    widgets: dict[str, list[tuple[int, DictionaryObject]]],
) -> dict[int, list[tuple[tuple[float, float, float, float], Any]]]:
    """The on-state appearance stream of every ticked checkbox, by page.

    ``filler._tick_appearances``'s read-back twin: where the sheet-copy
    flatten reads the writes it just applied, the export flatten reads the
    ``/AS`` the release's fill left on the widget. An unticked box (``/Off``,
    or no appearance states at all) draws nothing.
    """
    placements: dict[int, list[tuple[tuple[float, float, float, float], Any]]] = {}
    for entries in widgets.values():
        for page_index, obj in entries:
            if filler._is_choice(obj):
                continue
            state = filler._on_state(obj)
            if state is None or str(obj.get("/AS") or "/Off") == "/Off":
                continue
            appearance = obj.get("/AP")
            if appearance is None:
                continue
            normal = appearance.get_object().get("/N")
            if not isinstance(normal, DictionaryObject):
                continue
            key = NameObject(state)
            if key not in normal:
                continue
            placements.setdefault(page_index, []).append((filler._rect(obj), normal[key]))
    return placements


__all__ = ["flatten_filled_pdf"]
