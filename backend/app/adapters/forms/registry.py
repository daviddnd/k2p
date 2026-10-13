"""Form-registry loader: ``backend/forms/`` mapping, manifest, and geometry
(ADR-0006/0010).

The registry is the single source of truth for a form's structure: the
pinned bilingual template (its SHA-256 in ``manifest.json``), the
positional field names (``mapping.json`` — CR field names carry no
semantics, so the registry's curation is what makes them meaningful), and
the widget rectangles (``geometry.json``) the extraction paths match
against. This module reads it; nothing here parses a PDF.

Data-only, no writes: the registry is human-curated and pinned (a
human-in-the-loop zone per AGENTS.md), so the loader never repairs,
defaults, or reorders what it reads — a malformed file is a hard error, not
something to work around.

**The licensed-pack seam (ADR-0012).** A cached ``form_mapping`` pack carries
the same three documents per form, and ``load_form_registry`` prefers one
when a caller supplies it — the pack's entry for that form wins, and any
form the pack does not carry falls back to the image. The seam is a
parameter, not a switch: **no caller passes a payload today**, because the
consumers (``app.adapters.pdf.extractor``, ``app.adapters.pdf.ocr``) are
synchronous and the pack cache is a database read. *Whether the base image
may hold mapping intelligence* was the open question this seam raised —
decided by ADR-0012's 2026-10-05 amendment (the founder's base-image forms
split): "form-mapping intelligence" is precisely the **reading
intelligence** (the prior-filing extraction signals) and mapping/fee
updates, which ride packs and never the image, while the image itself
legitimately carries the CR's public templates and the field-fill geometry
the ungated line (manual fill → sign → export, no licence check) requires.
By that ruling the seam stays inert until the first reading/update pack
ships — wiring it earlier is a live code path with no consumer — so today
the in-image registry is what every caller reads; the pack path is
implemented and tested so a real pack can be adopted without writing this
loader again.

One dependency beyond the standard library (``app.domain.prior_filing``)
and no framework imports: this is an adapter, per ADR-0006's layer rule.
"""

import json
from collections.abc import Mapping
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any

from app.domain.prior_filing import FormCode


class FormRegistryError(RuntimeError):
    """The registry entry is missing or malformed (never silently defaulted)."""


@dataclass(frozen=True)
class FieldMapping:
    """One curated registry item: a form box (or group) and its target."""

    item: str
    item_type: str
    target: str
    pdf_fields: tuple[str, ...]


@dataclass(frozen=True)
class WidgetGeometry:
    """One widget's position on the pinned template.

    ``rect`` is ``[x0, y0, x1, y1]`` in PDF user space (points, origin at
    the page's bottom-left) — the same space the mapping's dumps use.
    """

    name: str
    page: int
    rect: tuple[float, float, float, float]
    widget_type: str


@dataclass(frozen=True)
class FormRegistry:
    """One form's registry entry, loaded and cross-checked."""

    form: FormCode
    form_code: str
    specification_no: str
    template: str
    template_sha256: str
    items: tuple[FieldMapping, ...]
    geometry: dict[str, WidgetGeometry]


def forms_root() -> Path:
    """The registry root: ``backend/forms`` in the repo, ``/app/forms`` in
    the deployment image (both are the package's grandparent, per
    ``deploy/backend.Dockerfile``)."""
    return Path(__file__).resolve().parents[3] / "forms"


def template_path(form: FormCode) -> Path:
    """The pinned template file for one form."""
    return forms_root() / form.value / load_form_registry(form).template


def page_header_fields(registry: FormRegistry) -> frozenset[str]:
    """The boxes the form repeats as its page header.

    Every sheet of both pinned forms carries the same BRN box top-right —
    NAR1's scheduled pages also the made-up-date triplet — which the registry
    expresses as one item whose fields sit on many pages (``HDR-BRN`` on 15,
    ``HDR-DATE`` on 7, ND2A's ``0`` on 7, per ``geometry.json``). That is what
    makes them the *header*: the fill assembly may print them only where the
    page carries the package's own content (the official specimens leave an
    unused sheet's header empty).

    "On more than two pages" is the discriminator, and the one item it excludes
    besides the genuinely single-page ones is ND2A's consent block, which
    prints a signatory on the appointment page *and* the body-corporate
    continuation sheet: two pages, and not a header. The exclusion is safe by
    construction — a box can only be dropped from a sheet page carrying nothing
    else, and a consent signatory never is.
    """
    headers: set[str] = set()
    for item in registry.items:
        pages = {
            registry.geometry[name].page
            for name in item.pdf_fields
            if name in registry.geometry
        }
        if len(pages) > 2:
            headers.update(item.pdf_fields)
    return frozenset(headers)


def sheet_pages(registry: FormRegistry) -> frozenset[int]:
    """The template's schedule and continuation-sheet pages.

    Both pinned forms print those sheets *after* the pages a declarant signs —
    NAR1's main form is pages 1-8 and its sheets 9-15, ND2A's main form 1-3 and
    its sheets 4-7 — so the main form is every page up to and including the one
    carrying the signature block (``SIG.name``'s geometry), and the rest are
    sheets. The distinction is what the assembly's header convention turns on:
    the main form prints its header on every page, used or not (a block the
    company does not fill, such as NAR1's 12A beside a body-corporate
    secretary, still has one), while an unused *sheet* stays blank — the
    official specimens' convention, mirrored by ``HDR-DATE`` covering exactly
    the sheet pages.
    """
    signed = [
        registry.geometry[name].page
        for item in registry.items
        if item.item.startswith("SIG.")
        for name in item.pdf_fields
        if name in registry.geometry
    ]
    if not signed:
        return frozenset()
    last_main = max(signed)
    return frozenset(
        page
        for page in {entry.page for entry in registry.geometry.values()}
        if page > last_main
    )


def sheet_page_fields(registry: FormRegistry) -> dict[str, tuple[str, ...]]:
    """The template's sheet pages, keyed by the sheet's slug, in page order.

    Each value is **every widget the template prints on that page** — what a
    sheet plan's value set must cover for the page to come out complete — and
    the order is the template's own page order, so the plan's sheet roster and
    the assembled package agree without a page table in either.

    The slug is the page's vocabulary in the registry's own terms: the leading
    segment of the first mapping item whose fields *all* sit on the page,
    lowercased — ``s1``/``s2`` and ``cs-a``…``cs-e`` for NAR1, ``a-2a``,
    ``b-3``, ``c-4``, ``pi`` for ND2A. A page-carrying item is what names the
    page; the repeated headers (``HDR-BRN``, ``HDR-DATE``, ND2A's ``0``) span
    pages and are therefore excluded from the naming, as is any item whose
    fields straddle two pages.

    A sheet page no item names in full is a registry the caller cannot plan
    (the sheet has no vocabulary); it is left out rather than guessed at, and
    the tests pin both forms' complete vocabularies.
    """
    fields_by_page: dict[int, list[str]] = {}
    for name, geometry in registry.geometry.items():
        fields_by_page.setdefault(geometry.page, []).append(name)
    slugs: dict[int, str] = {}
    for item in registry.items:
        pages = {
            registry.geometry[name].page
            for name in item.pdf_fields
            if name in registry.geometry
        }
        if len(pages) != 1:
            continue
        page = next(iter(pages))
        slugs.setdefault(page, item.item.split(".")[0].lower())
    return {
        slugs[page]: tuple(fields_by_page[page])
        for page in sorted(sheet_pages(registry))
        if page in slugs
    }


@cache
def image_registry(form: FormCode) -> FormRegistry:
    """The in-image registry entry, parsed once per process.

    Raises ``FormRegistryError`` when a file is missing, unparsable, or
    inconsistent (a mapping field without geometry, or geometry the mapping
    does not name) — a registry that cannot be trusted is never
    half-read.
    """
    directory = forms_root() / form.value
    return _registry_from(
        form,
        manifest=_load_json(directory / "manifest.json"),
        mapping=_load_json(directory / "mapping.json"),
        geometry=_load_json(directory / "geometry.json"),
    )


def load_form_registry(
    form: FormCode, *, pack_payload: Mapping[str, Any] | None = None
) -> FormRegistry:
    """The form's registry entry: a supplied ``form_mapping`` pack, else the image.

    The one consumption point for licensed mapping intelligence (ADR-0012).
    A caller holding a cached pack payload passes it and gets the pack's
    entry for this form when it carries one; a form the pack does not carry,
    or no payload at all, reads the in-image registry — so the fallback is
    the same path that runs today, unchanged. See the module note for why no
    caller passes a payload yet.
    """
    if pack_payload is not None:
        packed = registry_from_pack(form, pack_payload)
        if packed is not None:
            return packed
    return image_registry(form)


def registry_from_pack(
    form: FormCode, payload: Mapping[str, Any]
) -> FormRegistry | None:
    """One ``form_mapping`` pack's entry for this form; ``None`` if it has none.

    A pack carries the three registry documents per form — manifest, mapping,
    geometry — so the entry it produces is validated exactly like an in-image
    one: the mapping and the geometry must name the same fields, and a pack
    that is malformed is a hard error rather than a partial read.
    """
    forms = payload.get("forms")
    if not isinstance(forms, Mapping):
        raise FormRegistryError("form_mapping pack: no 'forms' object")
    entry = forms.get(form.value)
    if entry is None:
        return None
    if not isinstance(entry, Mapping):
        raise FormRegistryError(f"{form.value}: pack entry is not an object")
    return _registry_from(
        form,
        manifest=_section(entry, "manifest", form),
        mapping=_section(entry, "mapping", form),
        geometry=_section(entry, "geometry", form),
    )


def _registry_from(
    form: FormCode,
    *,
    manifest: Mapping[str, Any],
    mapping: Mapping[str, Any],
    geometry: Mapping[str, Any],
) -> FormRegistry:
    """One form's registry entry from its three documents, cross-checked."""
    items = tuple(
        FieldMapping(
            item=str(item["item"]),
            item_type=str(item["type"]),
            target=str(item["target"]),
            pdf_fields=tuple(item["pdf_fields"]),
        )
        for item in mapping["items"]
    )
    widgets = {
        str(field["name"]): WidgetGeometry(
            name=str(field["name"]),
            page=int(field["page"]),
            rect=_rect(field["rect"]),
            widget_type=str(field["type"]),
        )
        for field in geometry["fields"]
    }
    mapped = {name for item in items for name in item.pdf_fields}
    if mapped != set(widgets):
        missing = sorted(mapped - set(widgets))
        extra = sorted(set(widgets) - mapped)
        raise FormRegistryError(
            f"{form.value}: geometry does not match the mapping "
            f"(missing geometry: {missing}, unmapped: {extra})"
        )
    return FormRegistry(
        form=form,
        form_code=str(manifest["form_code"]),
        specification_no=str(manifest["specification_no"]),
        template=str(manifest["template"]),
        template_sha256=str(manifest["version"]["sha256"]),
        items=items,
        geometry=widgets,
    )


def _section(entry: Mapping[str, Any], name: str, form: FormCode) -> Mapping[str, Any]:
    section = entry.get(name)
    if not isinstance(section, Mapping):
        raise FormRegistryError(f"{form.value}: pack entry has no '{name}' object")
    return section


def _rect(values: list[float]) -> tuple[float, float, float, float]:
    """One geometry rectangle, narrowed to its four corners."""
    x0, y0, x1, y1 = (float(value) for value in values)
    return (x0, y0, x1, y1)


def _load_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise FormRegistryError(f"registry file missing: {path}") from None
    except json.JSONDecodeError as exc:
        raise FormRegistryError(f"registry file is not valid JSON: {path}: {exc}") from None
    if not isinstance(payload, dict):
        raise FormRegistryError(f"registry file is not a JSON object: {path}")
    return payload
