"""The deterministic fill plan: a draft's resolved state → the pinned CR
templates' fields (scope §2.9; contract §2.8, appendix A; ADR-0004 as amended
2026-08-19 — "fields carry data, overlay carries rendering").

``plan_fill`` is the whole of this slice: it takes one draft's resolved snapshot
(``app.domain.drafts.resolve_draft``'s output), the version's filing metadata
and statement flag, the pinned registry's items and the checkbox option table
(``app.adapters.pdf.options``) — all plain data; nothing here reads a database
or a PDF — and returns the value to write per widget, one outcome per registry
item, and the named escalations a human has to act on. The writer takes the plan
and the templates; it never has to guess and nothing is dropped silently.

**Every registry item is accounted for.** ``ItemOutcome.status`` is ``filled``
(the item's boxes were written), ``blank`` (intentionally — the detail says
why), ``escalated`` (the item cannot be completed from the draft and a human
must), or ``not_applicable`` (this build prints no such item: Schedule 2, the
listed-company schedule). Outcomes are one per registry *entry*, in the order
the items were given, because the ND2A mapping repeats item ids across its
blocks — the sequence, not the id, is what pairs an outcome with its item. An
item a sheet copy carries more than once has one outcome — the first copy's —
while the escalation list accumulates every copy's: an escalation that names
the record it is about (the PI-ND2A sheet's protected boxes, per appointee)
stands for its own copy, and entries that read identically collapse into one —
the ledger, not the outcome line, is where "nothing was left out" is kept.

**The package's sheets are planned, not just named.** The pinned templates'
continuation sheets are fills like any other page: ``FillPlan.sheets`` is the
package's sheet roster — one ``SheetPlan`` per *copy*, in package order, each
carrying the complete value set for that copy (its records plus the BRN/date
headers a used sheet prints). The first copy of a sheet is the template's own
page, filled in place like the rest of the form; each later copy is a
duplicate the writer fills in its own pass, hard-flattens and inserts after the
page it copies (duplicated pages share field names and would collide in the
AcroForm). The roster's arithmetic follows: item 8A's counts are the used
sheets per continuation type, each Schedule 1 copy carries its own footer
page-number pair, and ND2A's ``continuation_sheets`` grid counts its sheets —
all derived from the plan's sheets, never written by hand. A sheet whose page
is unused carries no roster entry and stays in the package blank, its headers
empty (the specimens' convention; the writer applies it to the header items'
every-occurrence writes).

**Escalations are named and closed.** One of ``officer_overflow``,
``member_overflow``, ``records_overflow``, ``share_classes_over_4``,
``multiple_changes``, ``protected_class_manual`` or ``missing_value``, each
naming the role, path or count a human needs. Overflow is never dropped or
approximated. The continuation sheets carry what the pinned blocks cannot
(every extra officer, member pair, records row and ND2A change), so the live
escalations are narrower than the vocabulary: ``share_classes_over_4`` (the
item 11 grid has no continuation — the spike's disposition),
``protected_class_manual`` (the PI sheet's §1 boxes), ``missing_value``
wherever the form requires what the draft does not state or the draft states a
value this build cannot honour, and ``officer_overflow`` for the two shapes the
templates genuinely cannot express — a second reserve director (no further
reserve block, no sheet) and a name history longer than the boxes that carry
it. Every escalation is listed with the record it is about: the escalation list
is the "nothing was left out" ledger.

**Where the values come from — the reader mirrored.** A registry target joins
individual contract paths with `` + ``; the planner resolves each part against
the record the item is about (the block's officer entry, a member, a share
class, the change payload's officer, the company block, the filing metadata)
and writes it into the item's fields positionally:

- parts == fields: one part per field, in order;
- one part, several fields: a §2.5 ``Address`` object expands in its own key
  order (``flat, building, street, district, country``); anything else is the
  same value in every field (the BRN headers, written to all their
  occurrences);
- one field, several parts: the item's documented composition (item 1's
  "Chinese name first, a space, the English name" — the spike's
  ``nar1_cjk.json``);
- ``date_parts``: the fields are consecutive DD/MM/YYYY triplets, one triplet
  per page, and every triplet takes the zero-padded day, month and year.

**The conventions are transcribed from the fill spike, never invented.** Where
a fixture shows a value, that value is the rule: the ``Nil`` of ND2A's empty
previous-name and alias boxes and of NAR1's zero charges, the
licence-number-or-not-required rendering of ``tcsp_licence``, the
thousands-separated figures of the share-capital grid and its derived total
row, item 14's delivery tick, item 16's statement tick, the signature date's
``DD/MM/YYYY``. The strike-out groups ("delete whichever does not apply") are a
curated table: every ``Dropdown_*`` line whose option is not the item's value
is struck, per the registry's notes and the spike's stage-4/7 tests.

**The §1 protected class is never filled.** The ND2A PI sheet's full HKID, its
check digit, the full passport number and the usual residential address are
left untouched — no field holds them (the change-event grammar refuses the
class by name) — and each box raises its own ``protected_class_manual``
escalation naming the path, so the sheet is completed by hand and never
guessed. The same sheet's public values (name, the partial identifiers, the
passport's issuing country) fill normally, as the spike's PI/public separation
proves.

**Not this module's to state.** The package's page *selection* — which unused
sheets stay in the submission and the blank headers they keep — is the writer's
page convention, applied to the header items' every-occurrence writes; this
plan states the sheets that are *used* (the roster) and their complete value
sets. The pinned blocks are what the main pages fill, and the continuation
sheets (the templates' own CS-A…CS-E, S1, A-2A/B-3/C-4 and PI pages) carry the
rest — an officer, member pair, records row or change beyond a sheet's capacity
is a duplicate on a further copy, never a drop.

Pure and framework-free per ADR-0006: the standard library and the domain
vocabulary it builds on.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from app.domain.company_master import CompanyType
from app.domain.prior_filing import FormCode, individual_paths

# A choice field to strike out (the spike's sentinel; the writer resolves it to
# the field's own non-blank strike option).
STRIKE = "$STRIKE"

# The per-item dispositions.
FILLED = "filled"
BLANK = "blank"
ESCALATED = "escalated"
NOT_APPLICABLE = "not_applicable"
OUTCOMES: frozenset[str] = frozenset({FILLED, BLANK, ESCALATED, NOT_APPLICABLE})

# The closed escalation vocabulary. The continuation sheets closed the overflow
# escalations that could be filled (officer/member/records overflow and a second
# ND2A change), so the kinds those cases used stay in the vocabulary — an
# escalation recorded before this slice is still readable — while the live ones
# are ``share_classes_over_4``, ``protected_class_manual``, ``missing_value``
# and ``officer_overflow`` for the two shapes no sheet can express (a second
# reserve director, a name history past its boxes).
OFFICER_OVERFLOW = "officer_overflow"
MEMBER_OVERFLOW = "member_overflow"
RECORDS_OVERFLOW = "records_overflow"
SHARE_CLASSES_OVER_4 = "share_classes_over_4"
MULTIPLE_CHANGES = "multiple_changes"
PROTECTED_CLASS_MANUAL = "protected_class_manual"
MISSING_VALUE = "missing_value"
ESCALATION_KINDS: frozenset[str] = frozenset(
    {
        OFFICER_OVERFLOW,
        MEMBER_OVERFLOW,
        RECORDS_OVERFLOW,
        SHARE_CLASSES_OVER_4,
        MULTIPLE_CHANGES,
        PROTECTED_CLASS_MANUAL,
        MISSING_VALUE,
    }
)

# §2.5's address object key order: the boxes a form's address row prints.
_ADDRESS_KEYS = ("flat", "building", "street", "district", "country")

# The licence object's rendering order (form note 20): the licence number, the
# "not required" tick, and the reason beside it. The ND2A items name the object
# as one target part, so its boxes are the object's own key order.
_LICENCE_KEYS = ("licence_no", "not_required", "not_required_reason")

# The share-capital grid's columns, in the registry's row order (item 11).
_SHARE_GRID_COLUMNS = ("class_name", "currency", "total_shares", "total_amount", "total_paid_up")

# §3.3's listed-company delivery channel. ``cb_2_P.8`` is the *listed*
# company's box and every ``S2.*`` item prints no item — listed companies are
# outside Wave 1 — so the plan refuses to tick it rather than tick a box it
# cannot honour. The other two stay: ``schedule_1`` is the non-listed company's
# schedule, and ``cd_rom`` is the TCSP's own physical delivery, the form only
# ticking the channel.
_SCHEDULE_2 = "schedule_2"


class FillPlanError(ValueError):
    """A registry item this planner has no rule for, or a field written twice.

    The registry is human-curated and pinned, so a mismatch is a change to be
    reviewed rather than something to guess around: an unknown item fails the
    plan loudly instead of falling out of the count.
    """


@dataclass(frozen=True)
class RegistryItem:
    """One registry item as the planner takes it — plain data from the adapter.

    The application layer builds these from the pinned
    ``backend/forms/<form>/mapping.json``
    (``app.adapters.forms.registry.FieldMapping``), so the domain never imports
    the adapter.
    """

    item: str
    item_type: str
    target: str
    pdf_fields: tuple[str, ...]


@dataclass(frozen=True)
class ItemOutcome:
    """One registry item's disposition (see the module docstring)."""

    item: str
    status: str
    detail: str | None = None


@dataclass(frozen=True)
class Escalation:
    """One named case a human must complete by hand."""

    kind: str
    detail: str


@dataclass(frozen=True)
class SheetPage:
    """One sheet page of the pinned template, as plain data from the registry.

    ``sheet`` is the page's slug in the registry's own vocabulary (``cs-c``,
    ``s1``, ``a-2a`` — ``app.adapters.forms.registry.sheet_page_fields``'s
    keys) and ``fields`` every widget the template prints on the page, in the
    page's order. A sequence of these arrives in the template's page order;
    the plan lays each sheet out against its own page's boxes and nothing else.
    """

    sheet: str
    fields: tuple[str, ...]


@dataclass(frozen=True)
class SheetPlan:
    """One sheet *copy* of the assembled package: its page and its value set.

    ``sheet`` names the template sheet page the copy prints (``SheetPage``'s
    slug vocabulary); ``fields`` is the **complete** value set for this copy —
    its records plus the BRN/date headers a used sheet carries — so the writer
    fills the copy in one pass and never has to merge anything into it. Copies
    arrive in package order: the first plan of a sheet is the template's own
    page (filled in place with the rest of the form), each later one a
    duplicate the writer flattens and inserts after it.

    ``page_no`` is the footer page number this copy prints, where the
    template numbers its sheets: NAR1's Schedule 1 copies carry ``1 1``,
    ``2 2``, … numbered among themselves (``_schedule1_copies``'s own
    arithmetic, printed through the registry's ``S1.page_no`` item).
    Continuation sheets and ND2A's grid print no footer numbers, so their
    copies — and any caller-built plan that does not state one — carry
    ``None``: the number's absence is the fact, not a zero.
    """

    sheet: str
    fields: Mapping[str, object]
    page_no: int | None = None


@dataclass(frozen=True)
class FillPlan:
    """What to write, which sheet copies the package carries, what each item's
    disposition is, and what needs a human."""

    fields: Mapping[str, object]
    sheets: tuple[SheetPlan, ...]
    outcomes: tuple[ItemOutcome, ...]
    escalations: tuple[Escalation, ...]


def plan_fill(
    *,
    form: str,
    resolved_state: Mapping[str, Any],
    filing_metadata: Mapping[str, Any],
    private_company_statement: bool | None,
    items: Sequence[RegistryItem],
    options: Mapping[str, Mapping[str, object]],
    sheet_pages: Sequence[SheetPage],
) -> FillPlan:
    """Plan one draft's fill for one form (``nar1`` or ``nd2a``).

    ``resolved_state`` is the draft version's snapshot (``company``,
    ``officers``, ``shareholders``, ``changes``), ``filing_metadata`` its §2.8
    document, and ``private_company_statement`` NAR1 item 16's flag. ``items``
    and ``options`` are the registry's plain data: what to account for, and the
    checkbox option table keyed by the target of each group
    (``app.adapters.pdf.options``). ``sheet_pages`` is the pinned template's
    sheet pages, in page order, each with every widget it prints
    (``app.adapters.forms.registry.sheet_page_fields``) — the plan cannot lay a
    continuation sheet out without them, so they are required. Pure: no I/O,
    no clock, no random.
    """
    context = _context(
        form=FormCode(form),
        state=resolved_state,
        metadata=filing_metadata,
        statement=private_company_statement,
        options=options,
    )
    slots = _slots(context, items, sheet_pages)
    writes_by_slot: list[dict[str, object]] = [{} for _slot in slots]
    outcomes: list[ItemOutcome] = []
    escalations: list[Escalation] = list(context.unattached)
    for entry in items:
        entry_fields = set(entry.pdf_fields)
        planned = [index for index, slot in enumerate(slots) if slot.fields & entry_fields] or [0]
        plans = [_plan_item(entry, slots[index].context) for index in planned]
        outcomes.append(ItemOutcome(entry.item, plans[0].status, plans[0].detail))
        for index, plan in zip(planned, plans, strict=True):
            for pdf_field, value in plan.writes.items():
                if pdf_field not in entry_fields:
                    raise FillPlanError(
                        f"{entry.item}: {pdf_field} is not one of the item's own boxes"
                    )
                if pdf_field not in slots[index].fields:
                    continue  # a repeated box's other occurrence, another slot's to print
                if pdf_field in writes_by_slot[index]:
                    raise FillPlanError(
                        f"{entry.item}: {pdf_field} is written by two registry items"
                    )
                writes_by_slot[index][pdf_field] = value
            for escalation in plan.escalations:
                if escalation not in escalations:
                    escalations.append(escalation)
    fields = dict(writes_by_slot[0])
    for index, slot in enumerate(slots):
        if slot.sheet and not slot.copy:
            # A sheet page the package does not use: the header items write
            # their every-occurrence here, and the writer's unused-sheet
            # convention is what empties them in the package.
            fields.update(writes_by_slot[index])
    return FillPlan(
        fields=fields,
        sheets=tuple(
            # The copy's printed footer number rides the plan the roster
            # computed (0 = unnumbered, the sheets that print no footer).
            SheetPlan(
                sheet=slot.sheet,
                fields=writes_by_slot[index],
                page_no=slot.context.sheet.page_no or None,
            )
            for index, slot in enumerate(slots)
            if slot.copy
        ),
        outcomes=tuple(outcomes),
        escalations=tuple(escalations),
    )


# --- One item's plan --------------------------------------------------------


@dataclass(frozen=True)
class _ItemPlan:
    """One item's disposition: what to write, how it stands, and what it raises."""

    writes: Mapping[str, object] = field(default_factory=dict)
    status: str = BLANK
    detail: str | None = None
    escalations: tuple[Escalation, ...] = ()


def _blank(detail: str) -> _ItemPlan:
    return _ItemPlan(status=BLANK, detail=detail)


def _filled(writes: Mapping[str, object], detail: str | None = None) -> _ItemPlan:
    return _ItemPlan(writes=writes, status=FILLED, detail=detail)


def _escalated(
    detail: str, *, escalations: Sequence[Escalation], writes: Mapping[str, object] | None = None
) -> _ItemPlan:
    return _ItemPlan(
        writes=writes or {}, status=ESCALATED, detail=detail, escalations=tuple(escalations)
    )


# --- The resolved state, read once ------------------------------------------


@dataclass(frozen=True)
class _Sheet:
    """One Schedule 1 sheet's worth: its class context, its member blocks and
    its own number in the package (``page_no``; 0 = no sheet is used)."""

    classes: tuple[Mapping[str, Any], ...]
    members: tuple[Mapping[str, Any], ...]
    page_no: int = 0


@dataclass(frozen=True)
class _Context:
    """Everything one plan is made from, resolved once and read-only.

    One of these is the *main* view — the template's own pages, where the
    pinned blocks hold the first officers and the first records — and each
    sheet copy plans against a scoped view of it whose record fields hold only
    what that copy carries (see ``_sheet_copies``). ``sheet_counts`` is the
    package's roster arithmetic (slug → copies), which only the main view
    needs: the derived items (8A, ND2A's ``continuation_sheets``) are written
    on the main pages.
    """

    form: FormCode
    flat: Mapping[str, object]
    options: Mapping[str, Mapping[str, object]]
    classes: tuple[Mapping[str, Any], ...]
    officers: tuple[Mapping[str, Any], ...]
    blocks: Mapping[str, Mapping[str, Any]]
    officer_sheets: Mapping[str, tuple[tuple[Mapping[str, Any], ...], ...]]
    members: tuple[Mapping[str, Any], ...]
    changes: tuple[Mapping[str, Any], ...]
    cessations: tuple[Mapping[str, Any], ...]
    natural_appointments: tuple[Mapping[str, Any], ...]
    body_appointments: tuple[Mapping[str, Any], ...]
    undecided_appointments: tuple[Mapping[str, Any], ...]
    sheet: _Sheet
    sheet_counts: Mapping[str, int] = field(default_factory=dict)
    unattached: tuple[Escalation, ...] = ()


def _context(
    *,
    form: FormCode,
    state: Mapping[str, Any],
    metadata: Mapping[str, Any],
    statement: bool | None,
    options: Mapping[str, Mapping[str, object]],
) -> _Context:
    company = _mapping(state.get("company"))
    share_capital = _mapping(company.get("share_capital"))
    classes = tuple(
        _mapping(entry) for entry in _sequence(share_capital.get("classes"))
    )
    flat = _company_paths(company)
    flat.update(_metadata_paths(metadata, statement))
    officers = tuple(
        sorted((_mapping(entry) for entry in _sequence(state.get("officers"))), key=_officer_order)
    )
    assignment = _nar1_assignment(officers)
    members = tuple(_mapping(entry) for entry in _sequence(state.get("shareholders")))
    changes = tuple(
        sorted((_mapping(entry) for entry in _sequence(state.get("changes"))), key=_change_order)
    )
    undecided = tuple(
        change
        for change in changes
        if _change_kind(change) == _APPOINTMENT and _appointment_person(change) is None
    )
    unattached = list(assignment.undecided)
    unattached.extend(_change_escalations(undecided))
    return _Context(
        form=form,
        flat=flat,
        options=options,
        classes=classes,
        officers=officers,
        blocks=assignment.blocks,
        officer_sheets=assignment.sheets,
        members=members,
        changes=changes,
        cessations=tuple(change for change in changes if _change_kind(change) == _CESSATION),
        natural_appointments=tuple(
            change for change in changes if _appointment_person(change) == _NATURAL
        ),
        body_appointments=tuple(
            change for change in changes if _appointment_person(change) == _BODY
        ),
        undecided_appointments=undecided,
        sheet=_Sheet(classes=(), members=()),
        unattached=tuple(unattached),
    )


# --- The plan's slots -------------------------------------------------------


@dataclass(frozen=True)
class _Slot:
    """One place the plan writes: the template's main pages, or one sheet page.

    ``sheet`` is the ``SheetPage`` slug — empty for the main pages — and
    ``fields`` the boxes this slot owns, so an item's writes are routed to the
    slot that prints them and nowhere else. ``context`` is the slot's own view
    of the resolved state (see ``_Context``), and ``copy`` is the slot's
    1-based copy number within its sheet, or 0 for the main pages and for a
    sheet page the package does not use: an unused page still owns its boxes
    (the header items write their every-occurrence there, and the writer's
    unused-sheet convention prints none of them) but it contributes no
    ``SheetPlan``, and its own items plan against an empty view.
    """

    sheet: str
    fields: frozenset[str]
    context: _Context
    copy: int = 0


def _slots(
    context: _Context, items: Sequence[RegistryItem], sheet_pages: Sequence[SheetPage]
) -> tuple[_Slot, ...]:
    """The plan's slots: the main pages first, then each sheet page in order.

    A page the registry names and the roster uses contributes one slot per
    copy; a page it names but no copy uses contributes one empty-view slot, so
    a sheet item plans its own blank rather than a pinned block's record; a
    roster entry the registry does not name is a registry the plan cannot lay
    out — refused by name, never dropped.
    """
    copies = _sheet_copies(context)
    named = {page.sheet: frozenset(page.fields) for page in sheet_pages}
    unnamed = sorted(set(copies) - set(named))
    if unnamed:
        raise FillPlanError(
            f"the registry names no sheet page for {', '.join(unnamed)} the plan needs"
        )
    sheet_fields = set().union(*named.values()) if named else set()
    every = {pdf_field for entry in items for pdf_field in entry.pdf_fields}
    slots = [
        _Slot(
            sheet="",
            fields=frozenset(every - sheet_fields),
            context=replace(
                context, sheet_counts={sheet: len(view) for sheet, view in copies.items()}
            ),
        )
    ]
    for page in sheet_pages:
        views = copies.get(page.sheet) or (_unused_sheet_view(context),)
        for number, view in enumerate(views, start=1):
            slots.append(
                _Slot(
                    sheet=page.sheet,
                    fields=named[page.sheet],
                    context=view,
                    copy=number if page.sheet in copies else 0,
                )
            )
    return tuple(slots)


def _unused_sheet_view(context: _Context) -> _Context:
    """A sheet page no copy uses: the header items' own view of it.

    The page still prints its header boxes — the writer's convention empties
    them, and the plan writes every occurrence so the convention has something
    to empty — so this view keeps the flat map (the company and the filing
    metadata). Every record the sheet's own items would print is gone: no
    block, no member, no records row, no change, so a ``CS-C.name`` or ``B-3``
    planned here states its own blank instead of a pinned block's record.
    """
    return replace(
        context,
        flat={**context.flat, "company.records_kept[]": []},
        blocks={},
        officer_sheets={},
        members=(),
        changes=(),
        cessations=(),
        natural_appointments=(),
        body_appointments=(),
        undecided_appointments=(),
        sheet=_Sheet(classes=(), members=()),
    )


# The NAR1 continuation pages' block keys, per sheet: one officer block each,
# except Continuation Sheet D, which prints two.
_SHEET_BLOCKS: Mapping[str, tuple[str, ...]] = {
    "cs-a": ("CS-A",),
    "cs-b": ("CS-B",),
    "cs-c": ("CS-C",),
    "cs-d": ("CS-D.1", "CS-D.2"),
}


def _sheet_copies(context: _Context) -> dict[str, tuple[_Context, ...]]:
    """The package's sheet copies: slug → one scoped context per copy, in order.

    Every record the pinned blocks cannot hold lands on its continuation sheet
    in the template's own order: officers past 12A/12B/13A/13B.1/13B.2 on
    CS-A…CS-D, records rows past item 15's on CS-E, member pairs and classes on
    Schedule 1, and ND2A's extra cessations and appointees on Sheet A, B, C and
    the PI-ND2A sheet. Each copy's view holds only the records it carries, so
    the same item handlers plan a copy without a second set of rules.
    """
    copies: dict[str, tuple[_Context, ...]] = {}
    if context.form is FormCode.NAR1:
        for sheet, groups in context.officer_sheets.items():
            keys = _SHEET_BLOCKS[sheet]
            copies[sheet] = tuple(_officer_sheet_view(context, keys, group) for group in groups)
        rows = [_mapping(row) for row in _sequence(context.flat.get("company.records_kept[]"))]
        if len(rows) > 1:
            copies["cs-e"] = tuple(
                replace(
                    context,
                    flat={**context.flat, "company.records_kept[]": [row]},
                    blocks={},
                    officers=(),
                )
                for row in rows[1:]
            )
        schedule = _schedule1_copies(context.members, context.classes)
        if schedule:
            copies["s1"] = tuple(replace(context, sheet=sheet) for sheet in schedule)
        return copies
    for sheet, changes in (
        ("a-2a", context.cessations[1:]),
        ("b-3", context.natural_appointments[1:]),
        ("c-4", context.body_appointments[1:]),
        ("pi", context.natural_appointments),
    ):
        if changes:
            copies[sheet] = tuple(
                _change_sheet_view(context, change=change, sheet=sheet) for change in changes
            )
    return copies


def _officer_sheet_view(
    context: _Context, keys: Sequence[str], officers: Sequence[Mapping[str, Any]]
) -> _Context:
    """One officer sheet copy's view: its block key(s) hold the copy's officers."""
    return replace(
        context,
        blocks=dict(zip(keys, officers, strict=False)),
        officers=(),
        members=(),
        changes=(),
        cessations=(),
        natural_appointments=(),
        body_appointments=(),
        undecided_appointments=(),
    )


def _change_sheet_view(
    context: _Context, *, change: Mapping[str, Any], sheet: str
) -> _Context:
    """One ND2A sheet copy's view: the change the copy carries, and nothing else.

    Sheet A carries an extra cessation, Sheet B an extra natural-person
    appointment, Sheet C an extra body-corporate one, and the PI-ND2A sheet one
    natural person's protected-sheet details — the mapping's own
    ``One PI-ND2A sheet per natural person appointee`` — so a second appointee
    of either office gets a second PI sheet rather than an unstated one.
    """
    kind = _change_kind(change)
    return replace(
        context,
        blocks={},
        officers=(),
        members=(),
        changes=(change,),
        cessations=(change,) if kind == _CESSATION and sheet == "a-2a" else (),
        natural_appointments=(
            (change,) if kind == _APPOINTMENT and sheet in {"b-3", "pi"} else ()
        ),
        body_appointments=(change,) if kind == _APPOINTMENT and sheet == "c-4" else (),
        undecided_appointments=(),
    )


def _company_paths(company: Mapping[str, Any]) -> dict[str, object]:
    """The registry's ``company.*`` paths, from the snapshot's own blocks.

    The snapshot groups its facts (``company.name.name_en``) while the registry
    addresses them flat (``company.name_en``) — this is that one translation,
    the resolution's own registration mirrored. The ``change_event.company.*``
    paths ND2A writes are the company's own values under the change-event
    notation (an event carries no company name of its own).
    """
    name = _mapping(company.get("name"))
    office = _mapping(company.get("registered_office"))
    nature = _mapping(company.get("business_nature"))
    contact = _mapping(company.get("contact"))
    filing = _mapping(company.get("filing"))
    share_capital = _mapping(company.get("share_capital"))
    values: dict[str, object] = {
        "company.br_number": company.get("br_number"),
        "company.cr_number": company.get("cr_number"),
        "company.type": company.get("type"),
        "company.date_of_incorporation": company.get("date_of_incorporation"),
        "company.status": company.get("status"),
        "company.name_en": name.get("name_en"),
        "company.name_zh": name.get("name_zh"),
        "company.business_name": name.get("business_name"),
        "company.business_nature_code": nature.get("business_nature_code"),
        "company.business_nature_description": nature.get("business_nature_description"),
        "company.email": contact.get("email"),
        "company.contact_phone_hk": contact.get("contact_phone_hk"),
        "company.mortgages_and_charges_total": filing.get("mortgages_and_charges_total"),
        "company.member_count": filing.get("member_count"),
        "company.records_kept[]": company.get("records_kept") or [],
        "share_capital.classes[]": share_capital.get("classes") or [],
        "share_capital.total": share_capital.get("total") or {},
    }
    for part in _ADDRESS_KEYS:
        values[f"company.registered_office.{part}"] = office.get(part)
    values["change_event.company.br_number"] = values["company.br_number"]
    values["change_event.company.name_en"] = values["company.name_en"] or values["company.name_zh"]
    return values


def _metadata_paths(metadata: Mapping[str, Any], statement: bool | None) -> dict[str, object]:
    """The §2.8 filing-metadata paths the registry's items target."""
    returned = _mapping(metadata.get("return"))
    presentor = _mapping(metadata.get("presentor"))
    signature = _mapping(metadata.get("signature"))
    consent = _mapping(metadata.get("consent"))
    return {
        "return.made_up_date": returned.get("made_up_date"),
        "return.fs_period_start": returned.get("fs_period_start"),
        "return.fs_period_end": returned.get("fs_period_end"),
        "return.private_company_statement": statement,
        "draft.filing_metadata.member_particulars_delivery": metadata.get(
            "member_particulars_delivery"
        ),
        "presentor.name": presentor.get("name"),
        "presentor.address": presentor.get("address"),
        "presentor.tel": presentor.get("tel"),
        "presentor.fax": presentor.get("fax"),
        "presentor.email": presentor.get("email"),
        "presentor.reference": presentor.get("reference"),
        "signature.name": signature.get("name"),
        "signature.date": signature.get("date"),
        "signature.capacity": signature.get("capacity"),
        "consent.signatory_name": consent.get("signatory_name"),
        "consent.signatory_capacity": consent.get("signatory_capacity"),
    }


# --- The NAR1 blocks --------------------------------------------------------

_SECRETARY = "company_secretary"
_DIRECTOR = "director"
_RESERVE_DIRECTOR = "reserve_director"
_NATURAL = "natural"
_BODY = "body"
_CESSATION = "cessation"
_APPOINTMENT = "appointment"


@dataclass(frozen=True)
class _Block:
    """One pinned officer block: the office it prints and the person it prints for."""

    role: str
    body_corporate: bool


# The pinned NAR1 blocks, each with the continuation sheet that carries the
# same block once it is full. The continuation blocks (CS-A…CS-D.2) print on
# the sheets themselves: the plan writes them through the same block rules,
# with the sheet copy's officer in the block (``_SHEET_BLOCKS`` names each
# sheet's keys).
_NAR1_BLOCKS: Mapping[str, _Block] = {
    "12A": _Block(_SECRETARY, False),
    "12B": _Block(_SECRETARY, True),
    "13A": _Block(_DIRECTOR, False),
    "13B.1": _Block(_DIRECTOR, True),
    "13B.2": _Block(_DIRECTOR, True),
    "13C": _Block(_RESERVE_DIRECTOR, False),
    "CS-A": _Block(_SECRETARY, False),
    "CS-B": _Block(_SECRETARY, True),
    "CS-C": _Block(_DIRECTOR, False),
    "CS-D.1": _Block(_DIRECTOR, True),
    "CS-D.2": _Block(_DIRECTOR, True),
}

# Each sheet's pinned occupancy: the records one copy of the sheet holds.
# Continuation Sheet D prints two body-corporate director blocks, and a
# Schedule 1 sheet two member blocks (``_schedule1_copies``), so their copies
# take two records each; the rest take one.
_SHEET_CAPACITY: Mapping[str, int] = {"cs-d": 2, "s1": 2}


@dataclass(frozen=True)
class _Assignment:
    """The pinned blocks' officers, the sheets' rosters, and the records whose
    person type cannot be decided at all."""

    blocks: Mapping[str, Mapping[str, Any]]
    sheets: Mapping[str, tuple[tuple[Mapping[str, Any], ...], ...]]
    undecided: tuple[Escalation, ...]


def _nar1_assignment(officers: Sequence[Mapping[str, Any]]) -> _Assignment:
    """Place each in-office officer in the pinned block or sheet it belongs to.

    The secretaryship is one office, so the first secretary takes its pinned
    block (12A natural, 12B body corporate) and any further one goes to the
    same office's continuation sheet, CS-A or CS-B; directors fill 13A (natural)
    and 13B.1/13B.2 (body corporate) in the officers' deterministic order, the
    natural ones beyond 13A going to CS-C and the body-corporate ones beyond
    13B.2 to Continuation Sheet D — two blocks per copy, so a copy takes two
    officers; the reserve director takes 13C. Everything past a sheet's capacity
    is a further copy of the same sheet (``_sheet_copies``), never a drop and
    never a second officer in one block. A record whose person type cannot be
    decided (a payload stating neither the natural-person name parts nor an
    address) is placed nowhere and escalates by name; a second reserve director
    has no further block and no sheet, so it escalates too.
    """
    blocks: dict[str, Mapping[str, Any]] = {}
    sheets: dict[str, list[tuple[Mapping[str, Any], ...]]] = {}
    undecided: list[Escalation] = []
    seen = {"secretary": 0, "natural": 0, "body": 0, "reserve": 0}
    for officer in officers:
        role = _text(officer.get("role"))
        if role not in {_SECRETARY, _DIRECTOR, _RESERVE_DIRECTOR}:
            continue
        body = _is_body_corporate(officer)
        if body is None:
            undecided.append(
                Escalation(
                    MISSING_VALUE,
                    f"{_label(officer)}, {role}, states neither the natural-person name parts nor "
                    "an address, so no block can be chosen — complete it by hand",
                )
            )
            continue
        if role == _SECRETARY:
            seen["secretary"] += 1
            if seen["secretary"] == 1:
                blocks["12B" if body else "12A"] = officer
            else:
                _add_sheet(sheets, "cs-b" if body else "cs-a", (officer,))
        elif role == _DIRECTOR:
            key = "body" if body else "natural"
            seen[key] += 1
            if key == "natural":
                if seen["natural"] == 1:
                    blocks["13A"] = officer
                else:
                    _add_sheet(sheets, "cs-c", (officer,))
            elif seen["body"] <= 2:
                blocks["13B.1" if seen["body"] == 1 else "13B.2"] = officer
            else:
                _add_sheet(sheets, "cs-d", (officer,))
        else:
            seen["reserve"] += 1
            if seen["reserve"] == 1:
                blocks["13C"] = officer
            else:
                undecided.append(
                    Escalation(
                        OFFICER_OVERFLOW,
                        f"{_label(officer)}, {_RESERVE_DIRECTOR}, is a second reserve director, "
                        "and the pinned form prints no further reserve-director block or sheet",
                    )
                )
    return _Assignment(
        blocks=blocks,
        sheets={sheet: tuple(groups) for sheet, groups in sheets.items()},
        undecided=tuple(undecided),
    )


def _add_sheet(
    sheets: dict[str, list[tuple[Mapping[str, Any], ...]]],
    sheet: str,
    records: tuple[Mapping[str, Any], ...],
) -> None:
    """Add records to a sheet's roster, filling each copy to its capacity.

    Continuing from copy 2 of CS-D drops to its third copy's first block, and
    Schedule 1's second member pair is the sheet's second copy — the roster is
    the package's own order, so a partial copy exists only as the last one.
    """
    groups = sheets.setdefault(sheet, [])
    capacity = _SHEET_CAPACITY.get(sheet, 1)
    if groups and len(groups[-1]) < capacity:
        groups[-1] += records
    else:
        groups.append(records)


# --- The ND2A blocks --------------------------------------------------------

# item id -> (change kind, person type). The continuation items (A-2A/A-2B,
# B-3, C-4, PI) print on the sheet copies and plan through the same rules: a
# copy's scoped context holds the single change it carries, so
# ``_primary_change`` reads that copy's payload.
_ND2A_BLOCKS: Mapping[str, tuple[str, str | None]] = {
    "2A": (_CESSATION, None),
    "2B": (_CESSATION, None),
    "A-2A": (_CESSATION, None),
    "A-2B": (_CESSATION, None),
    "3": (_APPOINTMENT, _NATURAL),
    "B-3": (_APPOINTMENT, _NATURAL),
    "4": (_APPOINTMENT, _BODY),
    "C-4": (_APPOINTMENT, _BODY),
    "PI": (_APPOINTMENT, _NATURAL),
}

# The continuation items' sheets, by the name the form prints: what a blank one
# says when no copy carries it.
_ND2A_CONTINUATION_ITEMS: Mapping[str, str] = {
    "A-2A": "A",
    "A-2B": "A",
    "B-3": "B",
    "C-4": "C",
    "PI": "PI-ND2A",
}

# The cessation block prints both person halves under one item id, and the
# boxes are positional (2A on page 1, A-2A on page 4), so the split the
# mapping's notes state is transcribed.
_CESSATION_NATURAL_BOXES = frozenset(
    {
        "fill_4_P.1",
        "fill_5_P.1",
        "fill_6_P.1",
        "fill_7_P.1",
        "fill_8_P.1",
        "fill_3_P.4",
        "fill_4_P.4",
        "fill_5_P.4",
        "fill_6_P.4",
        "fill_7_P.4",
    }
)
_CESSATION_BODY_BOXES = frozenset({"fill_9_P.1", "fill_10_P.1", "fill_8_P.4", "fill_9_P.4"})

_APPOINTMENT_TYPES = frozenset({"director_appointment", "secretary_appointment"})
_CESSATION_TYPES = frozenset({"director_cessation", "secretary_cessation"})

_CAPACITY_SUFFIX = ".capacity[]"
_CONSENT_SENTENCE_TARGET = "change_event.payload.appointment.officer.role"
_EXISTING_DIRECTOR_TARGET = (
    "change_event.payload.appointment.officer.existing_director_at_appointment"
)
_OFFICER_PREFIX = "officers[]"
_SHAREHOLDER_PREFIX = "shareholders[]"

# The tick groups the *form* requires an answer to, with the question each asks.
# The payload grammar requires a cessation's ``cessation_reason`` but leaves the
# other two optional, so an unanswered one is a completion the draft's edit owes
# rather than a box to leave silently unticked (scope §2.9 step 5) — unlike the
# genuinely optional groups (item 5's financial period, a body corporate's
# optional Chinese name, a director's licence group), which stay blank.
_REQUIRED_TICKS: Mapping[str, str] = {
    "change_event.payload.cessation.officer.cessation_reason": "the reason for the cessation",
    "change_event.payload.cessation.officer.continues_office_after_cessation": (
        "whether the officer continues in office after the cessation"
    ),
    _EXISTING_DIRECTOR_TARGET: "whether the appointee already holds office",
}


def _required_tick(entry: RegistryItem, officer: Mapping[str, Any]) -> str | None:
    """The question a required tick group asks, or ``None`` when it is optional.

    The one exemption the registry states: a company-secretary appointment does
    not answer the existing-director question (item 3's note, "not required when
    appointing a company secretary only"), so that group is genuinely optional
    there and stays blank.
    """
    if entry.target == _EXISTING_DIRECTOR_TARGET and _text(officer.get("role")) == _SECRETARY:
        return None
    return _REQUIRED_TICKS.get(entry.target)


# The PI-ND2A sheet's §1 protected boxes, box by box: the whole identifier and
# the usual residential address. The paths are the contract's protected-class
# fields under the appointment prefix (a test holds them to
# ``app.domain.change_events.PROTECTED_CLASS_FIELDS``).
_PI_PREFIX = "change_event.payload.appointment.officer"
_PROTECTED_BOXES: Mapping[str, tuple[str, ...]] = {
    f"{_PI_PREFIX}.hkid_full": ("fill_5_P.7",),
    f"{_PI_PREFIX}.hkid_check_digit": ("fill_6_P.7",),
    f"{_PI_PREFIX}.passport_full": ("fill_8_P.7",),
    f"{_PI_PREFIX}.usual_residential_address": (
        "fill_9_P.7",
        "fill_10_P.7",
        "fill_11_P.7",
        "fill_12_P.7",
        "fill_13_P.7",
    ),
}

# The PI sheet's passport box beside the protected number: the issuing
# country/region is public information and fills normally.
_PI_PASSPORT_COUNTRY_BOX = "fill_7_P.7"


def _change_kind(change: Mapping[str, Any]) -> str:
    kind = _text(change.get("type"))
    if kind in _CESSATION_TYPES:
        return _CESSATION
    if kind in _APPOINTMENT_TYPES:
        return _APPOINTMENT
    return ""


def _change_officer(change: Mapping[str, Any]) -> Mapping[str, Any]:
    """The change's officer object (the payload's own key for its kind)."""
    kind = _change_kind(change)
    if not kind:
        return {}
    officer = _mapping(_mapping(change.get("payload")).get(kind)).get("officer")
    return _mapping(officer)


def _appointment_person(change: Mapping[str, Any]) -> str | None:
    """Whether the appointed officer is a natural person or a body corporate.

    §2.4's shape split is the discriminator: a natural person carries the name
    parts (surname/other names) and a correspondence address, a body corporate
    its registered ``name_en``, its BR number and a registered office address.
    A change-event payload carries no ``person_type`` of its own, so the
    officer's own shape decides — and a payload stating neither is undecided,
    which is an escalation rather than a guess.
    """
    if _change_kind(change) != _APPOINTMENT:
        return None
    body = _is_body_corporate(_change_officer(change))
    if body is None:
        return None
    return _BODY if body else _NATURAL


def _change_escalations(changes: Sequence[Mapping[str, Any]]) -> tuple[Escalation, ...]:
    """One ``missing_value`` per appointee whose block cannot be chosen."""
    return tuple(
        Escalation(
            MISSING_VALUE,
            f"{_change_label(change)}: the appointee's person type cannot be decided from the "
            "payload (no natural-person name parts, no body-corporate name or address) — "
            "complete item 3 (natural person) or item 4 (body corporate) by hand",
        )
        for change in changes
    )


def _change_label(change: Mapping[str, Any]) -> str:
    return (
        f"{_change_kind(change) or 'change'} {change.get('event_id')} "
        f"({_label(_change_officer(change))})"
    )


# --- Item dispatch ----------------------------------------------------------


def _plan_item(entry: RegistryItem, context: _Context) -> _ItemPlan:
    if context.form is FormCode.NAR1:
        return _nar1_item(entry, context)
    return _nd2a_item(entry, context)


def _nar1_item(entry: RegistryItem, context: _Context) -> _ItemPlan:
    if entry.item.startswith("S2."):
        return _ItemPlan(
            status=NOT_APPLICABLE,
            detail="Schedule 2 is the listed-company schedule; this build files the NAR1 for a "
            "non-listed company (item 14's schedule_1)",
        )
    if entry.item == "8A":
        return _sheet_count_item(entry, context)
    if entry.item.startswith("S1."):
        return _schedule1_item(entry, context)
    block = _block_of(entry.item, _NAR1_BLOCKS)
    if block is not None:
        return _nar1_block_item(entry, block, _NAR1_BLOCKS[block], context)
    if entry.item in {"15", "CS-E.records"}:
        return _records_item(entry, context)
    if entry.item == "PR":
        return _presentor_item(entry, context)
    if entry.item.startswith("SIG."):
        return _signature_item(entry, context)
    if entry.item == "1":
        return _company_name_item(entry, context)
    if entry.item == "9":
        return _charges_item(entry, context)
    if entry.item == "10":
        return _member_count_item(entry, context)
    if entry.item == "14":
        return _delivery_item(entry, context)
    if entry.item == "16":
        return _statement_item(entry, context)
    if entry.item_type == "date_parts":
        return _date_item(entry, None, context, required=entry.item == "4")
    if entry.item_type == "repeat_grid":
        return _share_capital_item(entry, context)
    if entry.item_type == "checkbox_group":
        return _checkbox_item(entry, context)
    return _text_item(entry, None, context)


def _nd2a_item(entry: RegistryItem, context: _Context) -> _ItemPlan:
    if entry.item == "continuation_sheets":
        return _sheet_count_item(entry, context)
    if entry.item in {"0", "1"}:
        return _text_item(entry, None, context)
    if entry.item == "PR":
        return _presentor_item(entry, context)
    if entry.item.startswith("SIG."):
        return _signature_item(entry, context)
    if entry.item in {"CONSENT.name", "CONSENT.capacity"}:
        return _consent_item(entry, context)
    block = _ND2A_BLOCKS.get(entry.item)
    if block is None:
        raise FillPlanError(f"{entry.item}: no rule for this registry item")
    return _nd2a_block_item(entry, block, context)


# The derived sheet-count grids, field by field: NAR1's item 8A counts the used
# continuation and schedule sheets (the spike's consistency proof — the declared
# counts must equal the sheets actually filed), and ND2A's unnumbered box grid
# counts its own sheets. Both read the plan's roster; neither is master data.
_NAR1_SHEET_COUNTS: Mapping[str, str] = {
    "fill_4_P.8": "cs-a",
    "fill_5_P.8": "cs-b",
    "fill_6_P.8": "cs-c",
    "fill_7_P.8": "cs-d",
    "fill_8_P.8": "cs-e",
    "fill_9_P.8": "s1",
    "fill_10_P.8": "s2",
}
_ND2A_SHEET_COUNTS: Mapping[str, str] = {
    "fill_18_P.3": "a-2a",
    "fill_19_P.3": "b-3",
    "fill_20_P.3": "c-4",
    "fill_21_P.3": "pi",
}


def _sheet_count_item(entry: RegistryItem, context: _Context) -> _ItemPlan:
    """A sheet-count grid: the used sheets of each type, from the roster.

    Every box is written, zeros included — the specimen's 8A prints ``0`` in an
    unused type's box, and a count that is left blank would be indistinguishable
    from an unfinished form.
    """
    counts = _NAR1_SHEET_COUNTS if context.form is FormCode.NAR1 else _ND2A_SHEET_COUNTS
    writes: dict[str, object] = {
        pdf_field: str(context.sheet_counts.get(sheet, 0))
        for pdf_field, sheet in counts.items()
        if pdf_field in entry.pdf_fields
    }
    if len(writes) != len(entry.pdf_fields):
        raise FillPlanError(f"{entry.item}: the sheet-count grid's boxes moved")
    return _filled(writes)


def _block_of(item: str, blocks: Mapping[str, Any]) -> str | None:
    """The longest block id ``item`` is scoped to (``13B.1.capacity`` → 13B.1)."""
    matches = [block for block in blocks if item.startswith(f"{block}.")]
    return max(matches, key=len) if matches else None


# --- The shared item kinds --------------------------------------------------


@dataclass(frozen=True)
class _Record:
    """A record the registry's paths are resolved against, and where it sits.

    The registry addresses a *box* by its whole path (``officers[].name_zh``,
    ``change_event.payload.cessation.officer.name_zh``); the planner works on
    the record itself, so each handler pairs the two — the record and the prefix
    its paths start with — and only the tail past the prefix is walked.
    """

    values: Mapping[str, Any]
    prefix: str


def _officer_record(officer: Mapping[str, Any]) -> _Record:
    return _Record(officer, _OFFICER_PREFIX)


def _change_record(change: Mapping[str, Any]) -> _Record:
    """The change payload's officer as a record (the kind is its own prefix)."""
    kind = _change_kind(change)
    return _Record(_change_officer(change), f"change_event.payload.{kind}.officer")


def _text_item(
    entry: RegistryItem, record: _Record | None, context: _Context
) -> _ItemPlan:
    """The default composition: the item's parts against the record it is about."""
    parts = individual_paths(entry.target)
    fields = entry.pdf_fields
    writes: dict[str, object] = {}
    if len(parts) == 1:
        value = _resolve(record, parts[0], context)
        if isinstance(value, Mapping) and len(fields) > 1:
            # A documented composition of one object into several boxes: a
            # §2.5 Address in its own key order (the four Hong Kong boxes, or
            # five where the form prints Country/Region), or the licence
            # object's number / "not required" tick / reason (form note 20).
            keys = (
                _ADDRESS_KEYS
                if any(key in value for key in _ADDRESS_KEYS)
                else _LICENCE_KEYS
            )
            for key, pdf_field in zip(keys, fields, strict=False):
                text = _value_of(value.get(key))
                if text is not None:
                    writes[pdf_field] = text
        else:
            text = _value_of(value)
            if text is not None:
                writes = dict.fromkeys(fields, text)
    elif len(parts) == len(fields):
        for part, pdf_field in zip(parts, fields, strict=True):
            text = _value_of(_resolve(record, part, context))
            if text is not None:
                writes[pdf_field] = text
    else:
        raise FillPlanError(
            f"{entry.item}: target {entry.target!r} has {len(parts)} parts for {len(fields)} boxes"
        )
    if writes:
        return _filled(writes)
    return _ItemPlan(detail=f"nothing is recorded for {entry.target}")


def _date_item(
    entry: RegistryItem,
    record: _Record | None,
    context: _Context,
    *,
    required: bool,
) -> _ItemPlan:
    """A ``date_parts`` item: DD/MM/YYYY, zero-padded, into every triplet."""
    parts = individual_paths(entry.target)
    if len(parts) != 1:
        raise FillPlanError(f"{entry.item}: a date item names one date, not {len(parts)}")
    fields = entry.pdf_fields
    if len(fields) % 3:
        raise FillPlanError(f"{entry.item}: {len(fields)} boxes are not whole date triplets")
    parsed = _iso_date(_resolve(record, parts[0], context))
    if parsed is None:
        if required:
            return _escalated(
                f"{parts[0]} is not set, so this date box cannot be filled",
                escalations=(
                    Escalation(
                        MISSING_VALUE,
                        f"{parts[0]}: a required date the draft does not state — complete it "
                        "through the draft's edit",
                    ),
                ),
            )
        return _blank(
            f"{parts[0]} is not set (the form leaves this date blank when it does not apply)"
        )
    writes: dict[str, object] = {}
    for start in range(0, len(fields), 3):
        day, month, year = fields[start : start + 3]
        writes[day] = f"{parsed.day:02d}"
        writes[month] = f"{parsed.month:02d}"
        writes[year] = f"{parsed.year:04d}"
    return _filled(writes)


def _tick_item(
    entry: RegistryItem,
    value: object,
    context: _Context,
    *,
    required: str | None = None,
) -> _ItemPlan:
    """A tick group: set every box whose option is the item's value.

    ``required`` labels the question a form-required group asks. A group whose
    value is not stated and whose boxes were therefore left alone is a
    ``missing_value`` escalation when the form demands an answer — the draft's
    edit owes it — and a plain ``blank`` when the group is genuinely optional.
    """
    boxes = context.options.get(entry.target)
    if not boxes:
        raise FillPlanError(f"{entry.item}: no checkbox options for {entry.target!r}")
    writes = {pdf_field: True for pdf_field in entry.pdf_fields if boxes.get(pdf_field) == value}
    if writes:
        return _filled(writes)
    if value is None and required is not None:
        return _missing_value(
            f"nothing to tick: the draft does not answer {required}",
            f"{entry.target}: the form requires {required} and the draft states no answer — "
            "complete it through the draft's edit",
        )
    return _blank(f"nothing to tick: {entry.target} is {value!r}")


def _checkbox_item(entry: RegistryItem, context: _Context) -> _ItemPlan:
    """A tick group whose value comes from the flat map (item 3's company type)."""
    return _tick_item(entry, _resolve(None, entry.target, context), context)


def _strike_item(
    entry: RegistryItem, value: object, context: _Context, *, required: str | None = None
) -> _ItemPlan:
    """A delete-whichever group: strike every line whose option is not the value."""
    boxes = context.options.get(entry.target)
    if not boxes:
        raise FillPlanError(f"{entry.item}: no strike options for {entry.target!r}")
    if value is None:
        if required is not None:
            return _escalated(
                f"{required} is not set, so the inapplicable wording cannot be struck",
                escalations=(
                    Escalation(
                        MISSING_VALUE,
                        f"{required}: not stated — the delete-whichever line is completed by hand",
                    ),
                ),
            )
        return _blank(f"{entry.target} is not set, so no line is struck")
    writes = {
        pdf_field: STRIKE for pdf_field in entry.pdf_fields if boxes.get(pdf_field) != value
    }
    if not writes:
        return _blank(f"every line of this group applies to {value!r}")
    return _filled(writes)


# --- The NAR1 items ---------------------------------------------------------


def _nar1_block_item(
    entry: RegistryItem, block: str, spec: _Block, context: _Context
) -> _ItemPlan:
    officer = context.blocks.get(block)
    if officer is None:
        body = "body-corporate" if spec.body_corporate else "natural-person"
        role = spec.role.replace("_", " ")
        if block.startswith("CS-"):
            return _blank(f"no additional {body} {role} fills {block} as of the target date")
        return _blank(f"no {body} {role} in office as of the target date")
    record = _officer_record(officer)
    part = entry.item[len(block) + 1 :]
    if part == "capacity":
        return _capacity_item(entry, officer, context)
    if part in {"previous_names", "alias"}:
        return _name_history_item(entry, officer, context, part=part)
    if part == "identification":
        if any(
            _text(officer.get(key))
            for key in ("hkid_partial", "passport_partial", "passport_issuing_country_region")
        ):
            return _text_item(entry, record, context)
        return _missing_value(
            f"{_label(officer)}: the block's identification boxes have no source",
            f"{entry.target}: the record states neither a partial HKID nor a passport number — "
            "the form requires one of them",
        )
    if part == "tcsp_licence":
        if _mapping(officer.get("tcsp_licence")):
            return _text_item(entry, record, context)
        return _missing_value(
            f"{_label(officer)}: the block's licence boxes have no source",
            f"{entry.target}: a company secretary files a licence number or the 'not required' "
            "tick with its reason — the record states neither",
        )
    if part == "name" and _nameless(officer):
        return _missing_value(
            f"{_label(officer)}: the record states no name",
            f"{entry.target}: the officer's record states no name — the block's name boxes are "
            "completed by hand",
        )
    if part == "address":
        address = officer.get(
            "registered_office_address" if spec.body_corporate else "correspondence_address"
        )
        if not _mapping(address):
            return _missing_value(
                f"{_label(officer)}: the block's address boxes have no source",
                f"{entry.target}: the officer's record states no "
                f"{'registered office' if spec.body_corporate else 'correspondence'} address — "
                "the form requires one",
            )
    return _text_item(entry, record, context)


def _missing_value(detail: str, escalation: str) -> _ItemPlan:
    """An item whose whole group has no source: nothing to write, one named case."""
    return _escalated(detail, escalations=(Escalation(MISSING_VALUE, escalation),))


def _nameless(record: Mapping[str, Any]) -> bool:
    return not any(
        _text(record.get(key)) for key in ("name_zh", "name_en", "surname_en", "other_names_en")
    )


def _capacity_item(
    entry: RegistryItem, officer: Mapping[str, Any], context: _Context
) -> _ItemPlan:
    """The capacity group: the officer's ticks, and the 代替 box beside them."""
    boxes = context.options.get(entry.target)
    if not boxes:
        raise FillPlanError(f"{entry.item}: no checkbox options for {entry.target!r}")
    values = [value for value in _sequence(officer.get("capacity")) if isinstance(value, str)]
    writes: dict[str, object] = {
        pdf_field: True for pdf_field in entry.pdf_fields if boxes.get(pdf_field) in values
    }
    for pdf_field in entry.pdf_fields:
        if pdf_field in boxes:
            continue
        text = _value_of(officer.get("alternate_to"))
        if text is not None:
            writes[pdf_field] = text
    if not values:
        return _escalated(
            f"{_label(officer)}: no capacity (checkbox) is recorded for this block",
            escalations=(
                Escalation(
                    MISSING_VALUE,
                    f"{entry.target}: the officer's capacity list is empty — tick the block's "
                    "box(es) by hand",
                ),
            ),
            writes=writes,
        )
    return _filled(writes)


def _name_history_item(
    entry: RegistryItem, officer: Mapping[str, Any], context: _Context, *, part: str
) -> _ItemPlan:
    """Previous names / aliases: the first entry's pair, and the form's convention.

    The spike's fixtures are the rule: NAR1 leaves an empty previous-name or
    alias box blank, ND2A writes "Nil" (the official specimens). A second entry
    has no box and escalates.
    """
    plan = _text_item(entry, _officer_record(officer), context)
    entries = _sequence(officer.get(part))
    writes = dict(plan.writes)
    if not writes and context.form is FormCode.ND2A:
        writes = dict.fromkeys(entry.pdf_fields, "Nil")
    if len(entries) > 1:
        return _escalated(
            f"{_label(officer)}: more {part} entries than the form's boxes carry",
            escalations=(
                Escalation(
                    OFFICER_OVERFLOW,
                    f"{entry.target}: the record states {len(entries)} {part} entries and the "
                    "form's boxes carry one — the rest are completed by hand",
                ),
            ),
            writes=writes,
        )
    if writes:
        return _filled(writes)
    return plan


def _company_name_item(entry: RegistryItem, context: _Context) -> _ItemPlan:
    """Item 1: the bilingual name box — Chinese first, a space, the English name."""
    chinese = _text(_resolve(None, "company.name_zh", context))
    english = _text(_resolve(None, "company.name_en", context))
    value = " ".join(part for part in (chinese, english) if part)
    if not value:
        return _blank("company.name_zh and company.name_en are both unset")
    return _filled(dict.fromkeys(entry.pdf_fields, value))


def _charges_item(entry: RegistryItem, context: _Context) -> _ItemPlan:
    """Item 9: the charges total — "Nil" when nothing is owed (the specimens)."""
    value = _resolve(None, "company.mortgages_and_charges_total", context)
    figure = _figure(value)
    if figure is None or _is_zero(value):
        return _filled(dict.fromkeys(entry.pdf_fields, "Nil"))
    return _filled(dict.fromkeys(entry.pdf_fields, figure))


def _share_capital_company(context: _Context) -> bool:
    """Whether the company's type is the share-capital one this build files.

    Only a private company reaches the NAR1 filler — ``app.domain.compliance``
    dates no other type's return, so generation refuses them before a draft
    exists — and a private company has share capital by construction. No class
    resolving is therefore a fact that has not landed, never "the company has
    none": items 11 and 14 are required of it and item 10 is not.
    """
    return _text(context.flat.get("company.type")) == CompanyType.PRIVATE


def _member_count_item(entry: RegistryItem, context: _Context) -> _ItemPlan:
    """Item 10: only for a company *without* share capital (form note 11)."""
    if context.classes:
        return _blank("the company has share capital, so it completes item 11 and not item 10")
    if _share_capital_company(context):
        return _missing_value(
            "the company's share-capital facts have not landed, so item 10 is not this company's "
            "to complete",
            "share_capital.classes[]: a private company has share capital by construction, so "
            "item 10 (member count) does not apply — append the share-capital facts, or complete "
            "item 11 by hand",
        )
    value = _value_of(_resolve(None, "company.member_count", context))
    if value is None:
        return _blank("company.member_count is not set")
    return _filled(dict.fromkeys(entry.pdf_fields, value))


def _delivery_item(entry: RegistryItem, context: _Context) -> _ItemPlan:
    """Item 14: how the member particulars are delivered (a share-capital company).

    The listed company's ``schedule_2`` box is refused rather than ticked — this
    build files only for non-listed companies, so the tick would be a claim the
    package cannot honour — and no share class resolving for the private company
    this build files is a missing fact, not a company without capital.
    """
    if not context.classes:
        if _share_capital_company(context):
            return _missing_value(
                "the company's share-capital facts have not landed, so a required item cannot "
                "be completed",
                "share_capital.classes[]: a private company has share capital by construction "
                "and must state item 14's channel — append the share-capital facts, or tick the "
                "channel by hand",
            )
        return _blank(
            "item 14 is for a company with share capital; the company's type states none"
        )
    value = _resolve(None, "draft.filing_metadata.member_particulars_delivery", context)
    if value is None:
        return _missing_value(
            "the draft states no delivery channel for the member particulars",
            "filing_metadata.member_particulars_delivery: not stated — a company with share "
            "capital must complete item 14, so the channel is chosen through the draft's edit",
        )
    if _text(value) == _SCHEDULE_2:
        return _escalated(
            "the draft states Schedule 2, which this build cannot produce",
            escalations=(
                Escalation(
                    MISSING_VALUE,
                    "draft.filing_metadata.member_particulars_delivery: schedule_2 lists a listed "
                    "company's member particulars in Schedule 2, and listed companies are outside "
                    "Wave 1 — this build files only for non-listed companies, so state schedule_1 "
                    "or cd_rom through the draft's edit",
                ),
            ),
        )
    return _tick_item(entry, value, context)


def _statement_item(entry: RegistryItem, context: _Context) -> _ItemPlan:
    """Item 16: the private-company statement — a tick, or the escalation."""
    value = _resolve(None, "return.private_company_statement", context)
    if value is None:
        return _missing_value(
            "the draft does not say whether the private-company statement is made",
            "return.private_company_statement: not stated — the statement is confirmed by hand "
            "before filing",
        )
    if value is False:
        return _blank("the draft does not make the private-company statement")
    return _tick_item(entry, True, context)


def _share_capital_item(entry: RegistryItem, context: _Context) -> _ItemPlan:
    """Item 11: the class grid and its derived total row.

    Four class rows of five boxes, then the 總數 Total row's four (it has no
    class cell). The figures are the spike's rendering — thousands separators,
    no redundant decimals — and the total is the snapshot's derived one (summed
    over the classes, §2.3). More than four classes has no continuation on this
    form: the grid takes four and every class beyond raises
    ``share_classes_over_4``. For the private company this build files, no class
    resolving is a fact that has not landed rather than an empty grid — the
    required item escalates by name (``_share_capital_company``).
    """
    classes = context.classes
    if not classes and _share_capital_company(context):
        return _missing_value(
            "the company's share-capital facts have not landed, so item 11's grid has no rows",
            "share_capital.classes[]: a private company has share capital by construction, so no "
            "class resolving means the facts have not landed — append them before the filing, or "
            "complete the grid by hand",
        )
    fields = entry.pdf_fields
    rows = [fields[start : start + 5] for start in range(0, len(fields) - 4, 5)]
    total_fields = fields[len(rows) * 5 :]
    writes: dict[str, object] = {}
    for row, share_class in zip(rows, classes, strict=False):
        for pdf_field, key in zip(row, _SHARE_GRID_COLUMNS, strict=True):
            raw = share_class.get(key)
            value = _text(raw) if key == "class_name" else _figure(raw)
            if value is not None:
                writes[pdf_field] = value
    if classes:
        total = _mapping(context.flat.get("share_capital.total")) or _derived_total(classes)
        for pdf_field, key in zip(
            total_fields, ("currency", "total_shares", "total_amount", "total_paid_up"), strict=True
        ):
            value = _text(total.get(key)) if key == "currency" else _figure(total.get(key))
            if value is not None:
                writes[pdf_field] = value
    if len(classes) > len(rows):
        beyond = ", ".join(
            str(share_class.get("class_name")) for share_class in classes[len(rows) :]
        )
        return _escalated(
            f"{len(classes)} share classes and the grid prints {len(rows)} rows",
            escalations=(
                Escalation(
                    SHARE_CLASSES_OVER_4,
                    f"share_capital.classes[] beyond row {len(rows)} ({beyond}) have no box on "
                    "this form — the form has no continuation for item 11, so those classes are "
                    "completed by hand",
                ),
            ),
            writes=writes,
        )
    if not writes:
        return _blank("no share capital classes are in force as of the target date")
    return _filled(writes)


def _records_item(entry: RegistryItem, context: _Context) -> _ItemPlan:
    """Item 15 / CS-E: one row of {description, address} each.

    The item prints the *first* row of the records its context holds: the main
    page's context holds every row (so item 15 is the first), and a
    Continuation Sheet E copy's context holds that copy's single row
    (``_sheet_copies``), so the same rule fills sheet after sheet.
    """
    rows = [_mapping(row) for row in _sequence(context.flat.get("company.records_kept[]"))]
    if not rows:
        return _blank(
            "no records-kept entry is in force as of the target date (the records are kept at "
            "the registered office)"
        )
    writes: dict[str, object] = {}
    for pdf_field, key in zip(entry.pdf_fields, ("description", "address"), strict=False):
        value = _text(rows[0].get(key))
        if value is not None:
            writes[pdf_field] = value
    if not writes:
        return _blank("the records-kept entry states neither a description nor an address")
    return _filled(writes)


def _presentor_item(entry: RegistryItem, context: _Context) -> _ItemPlan:
    """The presentor's reference: the presenting firm's own contact details."""
    recorded = any(
        _resolve(None, part, context) is not None for part in individual_paths(entry.target)
    )
    if not recorded:
        return _escalated(
            "the draft states no presentor's reference",
            escalations=(
                Escalation(
                    MISSING_VALUE,
                    "filing_metadata.presentor: not stated — the presenting firm's own name, "
                    "address, tel, fax, email and reference are completed through the draft's edit",
                ),
            ),
        )
    return _text_item(entry, None, context)


def _signature_item(entry: RegistryItem, context: _Context) -> _ItemPlan:
    if entry.item == "SIG.capacity":
        return _strike_item(
            entry,
            _text(_resolve(None, "signature.capacity", context)),
            context,
            required="filing_metadata.signature.capacity",
        )
    parts = individual_paths(entry.target)
    value = _resolve(None, parts[0], context) if parts else None
    if entry.item == "SIG.date" and value is not None:
        parsed = _iso_date(value)
        if parsed is not None:
            return _filled(dict.fromkeys(entry.pdf_fields, f"{parsed:%d/%m/%Y}"))
    text = _value_of(value)
    if text is None:
        return _escalated(
            f"{entry.target} is not set",
            escalations=(
                Escalation(
                    MISSING_VALUE,
                    f"{entry.target}: not stated — complete it through the draft's edit",
                ),
            ),
        )
    return _filled(dict.fromkeys(entry.pdf_fields, text))


# --- The NAR1 Schedule 1 ----------------------------------------------------


def _schedule1_copies(
    members: Sequence[Mapping[str, Any]], classes: Sequence[Mapping[str, Any]]
) -> tuple[_Sheet, ...]:
    """The Schedule 1 sheets the members need, in package order.

    Schedule 1 prints one class per sheet and two member blocks, so the sheets
    are laid out by class (in the members' own order) and each class's members
    fill two blocks per sheet — further copies of the same page carry the rest,
    numbered as they are filed. Every sheet names its class, the mapping's own
    ``One class block per Schedule 1 sheet``, and a class the share capital
    does not state prints by its members' own label alone.
    """
    sheets: list[_Sheet] = []
    for class_name in dict.fromkeys(_text(member.get("class_name")) for member in members):
        of_class = tuple(
            member for member in members if _text(member.get("class_name")) == class_name
        )
        matching = tuple(
            share_class
            for share_class in classes
            if _text(share_class.get("class_name")) == class_name
        )
        if not matching:
            matching = ({"class_name": class_name, "total_shares": None},)
        for start in range(0, len(of_class), _SHEET_CAPACITY["s1"]):
            sheets.append(
                _Sheet(
                    classes=matching,
                    members=of_class[start : start + _SHEET_CAPACITY["s1"]],
                    page_no=len(sheets) + 1,
                )
            )
    return tuple(sheets)


def _schedule1_item(entry: RegistryItem, context: _Context) -> _ItemPlan:
    sheet = context.sheet
    if entry.item == "S1.page_no":
        if not sheet.members:
            return _blank("no Schedule 1 sheet is used, so its footer carries no page number")
        return _filled(dict.fromkeys(entry.pdf_fields, str(sheet.page_no)))
    if entry.item == "S1.class":
        if not sheet.members:
            return _blank("no share class has members as of the target date")
        share_class = sheet.classes[0]
        writes: dict[str, object] = {}
        name = _text(share_class.get("class_name"))
        if name is not None:
            writes[entry.pdf_fields[0]] = name
        total = _figure(share_class.get("total_shares"))
        if total is not None:
            writes[entry.pdf_fields[1]] = total
        return _filled(writes)
    index = 1 if entry.item.startswith("S1.member.1.") else 2
    part = entry.item.rsplit(".", 1)[-1]
    member = sheet.members[index - 1] if len(sheet.members) >= index else None
    if member is None:
        return _blank(f"no member fills block {index} of Schedule 1")
    if part == "name":
        return _member_name_item(entry, member, context)
    if part == "shares":
        return _member_shares_item(entry, member, context)
    return _text_item(entry, _Record(member, _SHAREHOLDER_PREFIX), context)


def _member_name_item(
    entry: RegistryItem, member: Mapping[str, Any], context: _Context
) -> _ItemPlan:
    """A member's name block: Chinese name, the person's split, or the OR box.

    The spike's Schedule 1 fixtures are the rule: a natural person's surname and
    other names fill their two boxes, and a body corporate's English name fills
    the "OR Name in English" box after 或.
    """
    body = _is_body_corporate(member)
    chinese = _text(member.get("name_zh"))
    english = _text(member.get("name_en"))
    surname = _text(member.get("surname_en"))
    other = _text(member.get("other_names_en"))
    fields = entry.pdf_fields
    writes: dict[str, object] = {}
    if chinese is not None:
        writes[fields[0]] = chinese
    if body:
        if english is not None:
            writes[fields[3]] = english
    elif surname is not None or other is not None:
        if surname is not None:
            writes[fields[1]] = surname
        if other is not None:
            writes[fields[2]] = other
    elif english is not None:
        return _escalated(
            f"{_member_label(member)}: the English name is not split into surname and other names",
            escalations=(
                Escalation(
                    MISSING_VALUE,
                    f"shareholders[] ({_member_label(member)}): surname_en/other_names_en are not "
                    "stated — the natural person's 姓氏 and 名字 boxes are completed by hand",
                ),
            ),
            writes=writes,
        )
    if not writes:
        return _blank(f"{_member_label(member)}: the record states no name")
    return _filled(writes)


def _member_shares_item(
    entry: RegistryItem, member: Mapping[str, Any], context: _Context
) -> _ItemPlan:
    """A member's share block: the holding, the jointly-held tick, the remarks."""
    fields = entry.pdf_fields
    writes: dict[str, object] = {}
    shares = _figure(member.get("shares_held"))
    if shares is not None:
        writes[fields[0]] = shares
    if member.get("jointly_held"):
        writes[fields[1]] = True
    remarks = _text(member.get("remarks"))
    if remarks is not None:
        writes[fields[2]] = remarks
    if shares is None:
        return _escalated(
            f"{_member_label(member)}: the holding states no number of shares",
            escalations=(
                Escalation(
                    MISSING_VALUE,
                    f"shareholders[] ({_member_label(member)}): shares_held is not stated — the "
                    "No. of Shares Held box is completed by hand",
                ),
            ),
            writes=writes,
        )
    return _filled(writes)


# --- The ND2A items ---------------------------------------------------------


def _nd2a_block_item(
    entry: RegistryItem, block: tuple[str, str | None], context: _Context
) -> _ItemPlan:
    kind, person = block
    change = _primary_change(kind, person, context)
    if change is None:
        if kind == _APPOINTMENT and context.undecided_appointments:
            return _escalated(
                "the appointee's person type cannot be decided from the payload",
                escalations=_change_escalations(context.undecided_appointments),
            )
        if entry.item in _ND2A_CONTINUATION_ITEMS:
            what = (
                "cessation"
                if kind == _CESSATION
                else f"{'natural-person' if person == _NATURAL else 'body-corporate'} appointment"
            )
            sheet = _ND2A_CONTINUATION_ITEMS[entry.item]
            return _blank(f"no further {what} needs Continuation Sheet {sheet}")
        what = "appointment" if kind == _APPOINTMENT else "cessation"
        who = "" if person is None else f" ({person.replace('_', ' ')})"
        return _blank(f"the resolved changes carry no {what}{who}")
    if entry.item == "PI":
        return _pi_item(entry, change, context)
    return _nd2a_identity_item(entry, change, context)


def _primary_change(kind: str, person: str | None, context: _Context) -> Mapping[str, Any] | None:
    if kind == _CESSATION:
        return context.cessations[0] if context.cessations else None
    appointments = (
        context.natural_appointments if person == _NATURAL else context.body_appointments
    )
    return appointments[0] if appointments else None


def _nd2a_identity_item(
    entry: RegistryItem, change: Mapping[str, Any], context: _Context
) -> _ItemPlan:
    """One box (or group) of the cessation/appointment blocks.

    The cessation block prints both person halves under one item id, so the
    item's own boxes say which half it is (the appointment blocks are already
    one half each), and a record whose person type cannot be decided leaves the
    item escalated by name.
    """
    kind = _change_kind(change)
    officer = _change_officer(change)
    record = _change_record(change)
    person = _appointment_person(change) if kind == _APPOINTMENT else _person_of(officer)
    if person is None:
        return _escalated(
            f"the person type of {_change_label(change)} cannot be decided from the payload",
            escalations=(
                Escalation(
                    MISSING_VALUE,
                    f"{_change_label(change)}: the record states neither the natural-person name "
                    "parts nor a body-corporate name or address — complete this block by hand",
                ),
            ),
        )
    if entry.item_type == "checkbox_group":
        if entry.target.endswith(_CAPACITY_SUFFIX):
            return _capacity_item(entry, officer, context)
        if entry.target == _CONSENT_SENTENCE_TARGET:
            return _consent_sentence_item(entry, officer, context)
        return _tick_item(
            entry,
            _resolve(record, entry.target, context),
            context,
            required=_required_tick(entry, officer),
        )
    if entry.item_type == "date_parts":
        return _date_item(entry, record, context, required=True)
    if "previous_names[]" in entry.target:
        return _name_history_item(entry, officer, context, part="previous_names")
    if "aliases[]" in entry.target:
        return _name_history_item(entry, officer, context, part="aliases")
    if not _cessation_half_applies(entry, kind, person):
        other = "body corporate" if person == _BODY else "natural person"
        return _blank(
            f"the officer is a {other}, so this half of the cessation block does not apply"
        )
    parts = individual_paths(entry.target)
    leaf = parts[0].rsplit(".", 1)[-1] if parts else ""
    if leaf == "tcsp_licence" and _mapping(officer.get("tcsp_licence")):
        return _text_item(entry, record, context)
    if (
        leaf == "tcsp_licence"
        and _text(officer.get("role")) == _SECRETARY
        and kind == _APPOINTMENT
    ):
        return _missing_value(
            f"{_change_label(change)}: the block's licence boxes have no source",
            f"{entry.target}: a company secretary files a licence number or the 'not required' "
            "tick with its reason — the record states neither",
        )
    if leaf in {"name_zh", "name_en", "surname_en", "other_names_en"} and _nameless(officer):
        return _missing_value(
            f"{_change_label(change)}: the record states no name",
            f"{entry.target}: the record states no name — the block's name boxes are completed "
            "by hand",
        )
    if leaf.endswith("_address") and not _mapping(_resolve(record, parts[0], context)):
        return _missing_value(
            f"{_change_label(change)}: the block's address boxes have no source",
            f"{entry.target}: the record states no address — the form requires one",
        )
    return _text_item(entry, record, context)


def _consent_sentence_item(
    entry: RegistryItem, officer: Mapping[str, Any], context: _Context
) -> _ItemPlan:
    """The appointment's consent sentence: the line that survives is its office.

    The 候補董事/alternate director line is the capacity ticked beside a
    directorship (scope §2.4: the alternate directorship is not a role), so the
    capacity decides which wording survives; a company secretary strikes both
    lines, and the registry keeps this item's target on the role path its notes
    name.
    """
    capacity = [value for value in _sequence(officer.get("capacity")) if isinstance(value, str)]
    if "alternate_director" in capacity:
        return _strike_item(entry, "alternate_director", context)
    return _strike_item(entry, _text(officer.get("role")), context)


def _person_of(officer: Mapping[str, Any]) -> str | None:
    body = _is_body_corporate(officer)
    if body is None:
        return None
    return _BODY if body else _NATURAL


def _cessation_half_applies(entry: RegistryItem, kind: str, person: str) -> bool:
    """Whether a cessation item's boxes belong to this person's half of the block."""
    if kind != _CESSATION:
        return True
    natural = any(box in _CESSATION_NATURAL_BOXES for box in entry.pdf_fields)
    body = any(box in _CESSATION_BODY_BOXES for box in entry.pdf_fields)
    if natural and body:
        raise FillPlanError(f"{entry.item}: the item spans both halves of the cessation block")
    return natural if person == _NATURAL else body


def _pi_item(entry: RegistryItem, change: Mapping[str, Any], context: _Context) -> _ItemPlan:
    """One box (or group) of the PI-ND2A sheet — the §1 protected class apart.

    Each protected box's escalation names the appointee it is about
    (``_change_label``) as well as the boxes: the PI sheet is one per natural
    appointee, and identical details would dedupe into a single entry in the
    release's checklist — leaving the second (flattened, widget-free) sheet's
    hand-completion unstated.
    """
    officer = _change_officer(change)
    if entry.item_type == "checkbox_group":
        return _capacity_item(entry, officer, context)
    protected = [
        (path, boxes)
        for path, boxes in _PROTECTED_BOXES.items()
        if any(box in entry.pdf_fields for box in boxes)
    ]
    if not protected:
        return _text_item(entry, _change_record(change), context)
    writes: dict[str, object] = {}
    if _PI_PASSPORT_COUNTRY_BOX in entry.pdf_fields:
        country = _text(officer.get("passport_issuing_country_region"))
        if country is not None:
            writes[_PI_PASSPORT_COUNTRY_BOX] = country
    return _escalated(
        "the PI-ND2A sheet's protected boxes are left untouched, box by box",
        escalations=tuple(
            Escalation(
                PROTECTED_CLASS_MANUAL,
                f"{_change_label(change)}: {path}: §1 protected information, which no field of "
                f"this build holds and the planner never fills — complete {', '.join(boxes)} by "
                "hand on the PI-ND2A sheet",
            )
            for path, boxes in protected
        ),
        writes=writes,
    )


def _consent_item(entry: RegistryItem, context: _Context) -> _ItemPlan:
    """The body-corporate consent block: only a body corporate signs it."""
    if not context.body_appointments:
        return _blank(
            "the consent block signs for a body-corporate appointee, and the notice carries no "
            "body-corporate appointment"
        )
    if entry.item == "CONSENT.name":
        name = _text(_resolve(None, "consent.signatory_name", context))
        if name is None:
            return _escalated(
                "the body-corporate appointee's consent statement names no signatory",
                escalations=(
                    Escalation(
                        MISSING_VALUE,
                        "filing_metadata.consent.signatory_name: not stated — the consent "
                        "statement is signed and named by hand",
                    ),
                ),
            )
        return _filled(dict.fromkeys(entry.pdf_fields, name))
    return _strike_item(
        entry,
        _text(_resolve(None, "consent.signatory_capacity", context)),
        context,
        required="filing_metadata.consent.signatory_capacity",
    )


# --- Reading the snapshot ---------------------------------------------------


def _resolve(record: _Record | None, path: str, context: _Context) -> object | None:
    """One part of a registry target, from the flat map or the record.

    The flat map holds the paths that name no record (the company block and the
    filing metadata, ``app.domain.drafts``' own registration mirrored). Every
    other part is resolved against the record the item is about, whose prefix is
    the path it sits at (``officers[]``, the change payload's officer): the part
    must start with it, and only the tail past it is walked. Nothing is guessed
    from a coincidental key — a record without ``previous_names`` yields no
    value for ``officers[].previous_names[].name_zh``, never the officer's own
    name. A ``[]`` segment takes the first entry; the compositions that carry
    several entries are the curated ones (``previous_names``, the member
    blocks).
    """
    if path in context.flat:
        return context.flat[path]
    if record is None:
        return None
    if path != record.prefix and not path.startswith(f"{record.prefix}."):
        return None
    if path == record.prefix:
        return record.values
    return _descend(record.values, path[len(record.prefix) + 1 :].split("."))


def _descend(value: object, segments: Sequence[str]) -> object | None:
    for segment in segments:
        if segment.endswith("[]"):
            if isinstance(value, Mapping):
                value = value.get(segment[:-2])
            if not isinstance(value, list) or not value:
                return None
            value = value[0]
            continue
        if not isinstance(value, Mapping):
            return None
        value = value.get(segment)
        if value is None:
            return None
    return value


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _sequence(value: object) -> tuple[Any, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        return ()
    return tuple(value)


def _text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _value_of(value: object) -> object | None:
    """A resolved value as a box's content: text, a tick, or nothing to write."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return _text(value)
    if isinstance(value, (date, Decimal)):
        return str(value)
    if isinstance(value, int):
        return str(value)
    return None


def _iso_date(value: object) -> date | None:
    text = _text(value)
    if text is None:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _figure(value: object) -> str | None:
    """A share-capital figure as the spike's fixtures render it.

    Thousands separators, and no decimal part when the figure is whole:
    ``1000`` and ``"1000.00"`` both print as ``1,000``, while a fractional
    figure keeps its own decimals (``"1234.50"`` → ``1,234.50``).
    """
    if isinstance(value, bool):
        return None
    text = _text(value) if isinstance(value, str) else None
    if text is None:
        if isinstance(value, int):
            return f"{value:,}"
        if not isinstance(value, (Decimal, float)):
            return None
        text = str(value)
    try:
        number = Decimal(text.replace(",", ""))
    except InvalidOperation:
        return text
    if number == number.to_integral_value():
        return f"{int(number):,}"
    whole, _, fraction = text.partition(".")
    try:
        whole_number = Decimal(whole.replace(",", ""))
    except InvalidOperation:
        return text
    return f"{int(whole_number):,}.{fraction}"


def _is_zero(value: object) -> bool:
    if value is None:
        return True
    if isinstance(value, bool):
        return False
    try:
        return Decimal(str(value).replace(",", "")) == 0
    except InvalidOperation:
        return False


def _derived_total(classes: Sequence[Mapping[str, Any]]) -> Mapping[str, Any]:
    """The total row's figures, summed over the classes (the snapshot's rule)."""
    currencies = {share_class.get("currency") for share_class in classes}
    shares = [share_class.get("total_shares") for share_class in classes]
    amounts = [share_class.get("total_amount") for share_class in classes]
    paid_up = [share_class.get("total_paid_up") for share_class in classes]
    counted = [value for value in shares if isinstance(value, int) and not isinstance(value, bool)]
    return {
        "currency": currencies.pop() if len(currencies) == 1 else None,
        "total_shares": sum(counted) if counted else None,
        "total_amount": _sum_amounts(amounts),
        "total_paid_up": _sum_amounts(paid_up),
    }


def _sum_amounts(values: Sequence[object]) -> str | None:
    stated = [value for value in values if value is not None]
    if not stated:
        return None
    try:
        return str(sum(Decimal(str(value).replace(",", "")) for value in stated))
    except InvalidOperation:
        return None


def _is_body_corporate(record: Mapping[str, Any]) -> bool | None:
    """Whether a person record describes a body corporate (``None`` = not stated).

    The purpose-split is the strongest signal — a natural person files a
    correspondence address, a body corporate a registered office — and §2.4's
    name shape decides what the addresses do not: a natural person carries the
    surname/other-name parts, a body corporate a registered ``name_en`` and a
    BR number.
    """
    declared = _text(record.get("person_type"))
    if declared is not None:
        return declared != "natural_person"
    if record.get("registered_office_address") is not None:
        return True
    if record.get("correspondence_address") is not None:
        return False
    if _text(record.get("surname_en")) or _text(record.get("other_names_en")):
        return False
    if _text(record.get("name_en")) or _text(record.get("br_number")):
        return True
    return None


def _officer_order(entry: Mapping[str, Any]) -> tuple[str, str, str]:
    """The officers' deterministic order: valid_from, then the stable id, then the name."""
    source = _mapping(entry.get("source"))
    return (
        str(entry.get("valid_from") or ""),
        str(entry.get("person_id") or source.get("id") or ""),
        _label(entry),
    )


def _change_order(change: Mapping[str, Any]) -> tuple[str, str]:
    return (str(change.get("effective_date") or ""), str(change.get("event_id") or ""))


def _label(record: Mapping[str, Any]) -> str:
    """A record as an escalation names it: its name and the id a human can find.

    The id matters: two officers may share a name, and an escalation that names
    neither id nor count would be indistinguishable from its twin.
    """
    for key in ("name_zh", "name_en", "surname_en", "other_names_en"):
        text = _text(record.get(key))
        if text is not None:
            return _with_id(text, record)
    return _with_id("the unnamed record", record)


def _with_id(name: str, record: Mapping[str, Any]) -> str:
    identifier = record.get("person_id") or _mapping(record.get("source")).get("id")
    return name if identifier is None else f"{name} ({identifier})"


def _member_label(member: Mapping[str, Any]) -> str:
    parts = [
        part
        for part in (_text(member.get("name_zh")), _text(member.get("name_en")))
        if part is not None
    ]
    return " ".join(parts) if parts else "the unnamed member"


__all__ = [
    "BLANK",
    "ESCALATED",
    "ESCALATION_KINDS",
    "FILLED",
    "MEMBER_OVERFLOW",
    "MISSING_VALUE",
    "MULTIPLE_CHANGES",
    "NOT_APPLICABLE",
    "OFFICER_OVERFLOW",
    "OUTCOMES",
    "PROTECTED_CLASS_MANUAL",
    "RECORDS_OVERFLOW",
    "SHARE_CLASSES_OVER_4",
    "STRIKE",
    "Escalation",
    "FillPlan",
    "FillPlanError",
    "ItemOutcome",
    "RegistryItem",
    "SheetPage",
    "SheetPlan",
    "plan_fill",
]
