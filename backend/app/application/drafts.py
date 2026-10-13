"""Draft orchestration: generation, the reads, the §4.2 executor and §4.1's
invalidation (scope §2.9, §2.11; data-model §2.8; contract §4.2, §5.1).

This module is the only path that writes a draft row, and the only place the
§4.2 table is consulted:

- ``generate_draft`` is the **gated automation** (DoD §9 item 27): it consults
  the single §5.1 gate with ``AutomationType.AUTOMATED_PREFILL`` and fails
  closed with the gate's reason, then resolves the draft through
  ``app.domain.drafts.resolve_draft`` and writes one version. The reads it
  feeds the resolution — the as-of master data, F1's in-office officers and
  shareholdings, the company's approved change events and the prior-filing
  basis — are the layers' own seams, never a second read of a table another
  module owns.
- ``get_draft`` / ``list_drafts`` are the reads. Both are grant-gated per
  company with **no permission flag** (ADR-0012: the customer's own data reads
  in every tier; the two draft permission flags gate the transitions, not the
  reads), and neither writes anything.
- ``transitions_for`` is the §5.1 read behind the action buttons: the wired,
  leg-admitted rows from the §4.2 table for the actor's three resolved legs.
- ``transition`` is **the one executor** ADR-0008 asks for: lock → guard →
  actor leg → write → audit. It refuses an unwired action by name before the
  guard (a property of the build, not of the draft's status), a status that
  does not admit the action, and a missing leg. ``send_for_signature`` is the
  **release**: it plans (F4a's ``fill_plan_for``), fills the pinned template
  (``app.adapters.pdf.filler``), stores the PDF through the records registry's
  write path (``origin=generated``, ``kind=generated_draft``), moves the draft
  and the signature lifecycle, and returns the package beside the view; the
  fill's hard refusals (a missing font, a character the font cannot render, a
  plan the registry contradicts) are raised before the store write, so a
  refused release writes nothing.
- ``record_signed_upload`` is the ``upload_signed`` row's own performer, the
  one action the contract's dedicated route (``POST /drafts/{id}/signature``)
  calls instead of the transitions POST: it stores the client-signed copy
  through the same registry write path (WORM ``signed_draft``, scanned,
  validated by declared type and magic bytes) and moves the draft to
  ``signature_received`` on the same session, every refusal raised before
  anything is written.
- ``mark_reviewed`` / ``reject`` / ``approve`` are the two-layer review/
  approval gate (scope §2.11), the moves the transitions POST performs
  directly: ``reject`` requires its rationale at both legs, and ``approve``
  carries the item-16 statement confirmation (``confirm_statement``) — the
  approval stamp and the statement pair are written with the move, and the
  stamp convention (``_move_row``: a landing clears the stamps it does not
  carry) keeps the row telling one story through rejections, recalls and
  invalidations.
- ``regenerate_draft`` is P6-3's ``regenerate`` row: generation with a
  lineage. It reuses ``generate_draft``'s version-row builder
  (``_build_version``) so a regeneration resolves exactly like a generation —
  current master data, the still-approved events, a fresh snapshot and a clean
  signature lifecycle and stamp pairs — after the same §5.1 gate (a denial
  writes nothing). The refused version stays ``rejected`` as history, and the
  audit story is the generation's own single ``draft_generated`` entry naming
  the superseded version — never a second transition record. The row is
  rationale-free: a stated rationale refuses 422 (``regenerate: carries
  rationale``), the body's-shape pattern, never a silent drop.
- ``export_draft`` is P6-4's ``export`` row: the release's sibling, and the
  last human-gated seam before a filing leaves the building (§2.11, §2.12).
  After the same lock → guard → leg order as every action — ``approved``
  alone admits it, and the ``approve_drafts`` leg answers for the actor — the
  statement gate asks ``ensure_exportable`` (409 ``statement_not_confirmed``
  for an NAR1 declaration nobody confirmed), and then the package assembles:
  the fill plan and the filled template exactly as the release built them,
  flattened for the two PDF modes (ADR-0004's "flatten only at the final
  export step" — ``app.adapters.pdf.export_flatten``) or serialized to
  deterministic JSON for ``api_ready``; the artifacts stored through the
  records registry's write path as WORM ``export_package`` documents (the
  primary plus the mode's companion — the e-Registry helper, the paper
  checklist with its sourced fee block); the ``filing_packages`` row with the
  derived envelope; the applied events submitted through the change-events
  seam on the same session; and the audit chain's ``draft_exported`` entry
  beside the transition's. The row is rationale-free — an export goes out or
  it does not, and a stated note refuses 422 (``export: carries rationale``),
  the ``regenerate`` precedent — never a silently dropped key. Every assembly
  refusal raises **before** any store
  write — a refused export leaves the draft where it stood, the store
  untouched and the chain silent. No mode submits anything: the e-Registry
  prep mode walks a human through the upload, the paper mode covers the
  filing with its checklist, and the API-ready mode stores the structured
  JSON a future channel would send — v1.0 never files unattended.
- ``invalidate_for_event`` is §4.1's "invalidates affected drafts": the
  change-events executor calls it when an applied event is reverted, edited,
  deleted or withdrawn, and it moves every affected lineage's current version
  to ``invalidated`` with the draft's own audit entry.

**Generation moves the events it applied.** The applied events are the ones
``app.domain.drafts`` resolved into the snapshot; each ``confirmed`` one is
moved ``confirmed → applied_to_draft`` through the change-events module's own
write path (``change_events.apply_to_draft``) — the §4.1 system ``apply``,
whose "draft linkage recorded" side effect is this draft's
``applied_event_ids`` — on the same session, so the version and the events'
new status commit or roll back together.

**One transaction, one audit entry per act.** ``draft_generated`` carries the
company, the form, the version and the applied event ids; each transition
carries the action and the before/after; each invalidation carries the draft,
the version and the event that caused it. The API session dependency commits at
the request edge, so a refused request leaves no partial writes.

Services flush but never commit. Reads write nothing.
"""

from collections.abc import Collection, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import and_, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.forms.registry import (
    load_form_registry,
    page_header_fields,
    sheet_page_fields,
    sheet_pages,
    template_path,
)
from app.adapters.pdf.export_flatten import flatten_filled_pdf
from app.adapters.pdf.filler import fill_template
from app.adapters.pdf.options import CHECKBOX_OPTIONS
from app.adapters.persistence.company_master import _EffectiveDatedFact
from app.adapters.persistence.documents import Document
from app.adapters.persistence.drafts import Draft
from app.adapters.persistence.filing_packages import FilingPackage
from app.adapters.persistence.models import Company
from app.adapters.persistence.officers import OfficerAppointment, Shareholding
from app.application import (
    audit,
    company_master,
    documents,
    officers,
    prior_filing,
    service_packages,
)
from app.application.company_master import CompanyMasterSnapshot
from app.application.documents import DocumentStore
from app.application.officers import AppointmentWithPerson, ShareholdingWithPerson
from app.application.scanning import Scanner
from app.domain.audit import AuditAction
from app.domain.change_events import ChangeEventStatus, ChangeEventType
from app.domain.documents import DocumentKind, DocumentOrigin
from app.domain.draft_fill import Escalation, FillPlan, RegistryItem, SheetPage, plan_fill
from app.domain.drafts import (
    APPROVAL_STAMP_STATUSES,
    DRAFT_TRANSITIONS,
    REVIEW_STAMP_STATUSES,
    STATEMENT_CARRIED_STATUSES,
    AllowedAction,
    DraftAction,
    DraftLeg,
    DraftStatus,
    EventInput,
    OfficerInput,
    ShareholdingInput,
    SignatureStatus,
    SourceRef,
    admits,
    allowed_actions,
    build_field_provenance,
    ensure_exportable,
    filing_metadata_template,
    filing_metadata_violations,
    is_wired,
    leg_for,
    merge_filing_metadata,
    nar1_made_up_date,
    prior_filing_comparison,
    rationale_required,
    resolve_draft,
    unwired_reason,
)
from app.domain.drafts import CompanyInput as DraftCompanyInput
from app.domain.drafts import MasterFact as DraftMasterFact
from app.domain.export_package import (
    ExportMode,
    api_ready_bytes,
    build_envelope,
    ered_registry_helper_text,
    paper_checklist_text,
)
from app.domain.prior_filing import FormCode
from app.domain.rbac import ActorContext, Permission, can_access_company, is_allowed
from app.domain.service_packages import AutomationType, GatingReason

# The columns a fact group's values are read from, per group: the draft's
# resolution names the appendix-A paths, so the application hands it one plain
# value per column and never a row.
_NAME_COLUMNS = ("name_en", "name_zh", "business_name")
_OFFICE_COLUMNS = ("flat", "building", "street", "district", "country")
_NATURE_COLUMNS = ("business_nature_code", "business_nature_description")
_CONTACT_COLUMNS = ("email", "contact_phone_hk")
_FILING_COLUMNS = ("mortgages_and_charges_total", "member_count")

# The statuses §4.1's invalidation leaves alone: an exported draft is a filing
# already made (its event's record is what a reader follows), and an invalidated
# one is the idempotence case — a second withdrawal of the same event changes
# nothing.
_INVALIDATION_SKIPPED: tuple[str, ...] = (
    DraftStatus.EXPORTED.value,
    DraftStatus.INVALIDATED.value,
)


class AutomationDeniedError(Exception):
    """The §5.1 gate refused generation; ``reason`` names the failing leg."""

    def __init__(self, reason: GatingReason) -> None:
        self.reason = reason
        super().__init__(f"draft generation is not licensed for this company: {reason.value}")


class DraftNotFoundError(Exception):
    """No draft with that id in the actor's tenant (cross-tenant → unknown)."""


class DraftForbiddenError(Exception):
    """The actor has no per-company access grant for the draft's company."""


class DraftConflictError(Exception):
    """Another generation for this lineage committed while this one was writing.

    The primary key refused a second row for one ``(id, version_no)`` — the same
    lost race ``change_event_conflict`` names, and the caller's remedy is the
    same: re-read and retry.
    """


class DraftInvalidError(Exception):
    """The edit leaves the rules; ``reasons`` names every one.

    The reasons are the domain's own strings (``filing_metadata_invalid:
    <field>``, ``edit: nothing to change``), and the API prints them verbatim —
    the officers/change-events precedent.
    """

    def __init__(self, reasons: list[str]) -> None:
        self.reasons = reasons
        super().__init__("; ".join(reasons))


class DraftRationaleRequiredError(Exception):
    """The action is one §4.2 marks "rationale required" (a rejection), and
    none arrived — the change-events precedent, whose §5.1.3 code the API
    answers with (``rationale_required``)."""


class DraftTransitionError(Exception):
    """The draft's current status does not admit the action; the guard refused.

    Carries both operands so the API can name the refusal without re-reading the
    table (``app.domain.drafts`` owns the legality; this is only the report of
    it). A system row answers the same error — no staff actor may perform it —
    and, since P6-1, so does ``upload_signed`` through this door: the table
    admits it, but its performer is the contract's dedicated
    ``POST /drafts/{id}/signature`` route, which a transitions body cannot
    carry. In every case the refusal is named, never a half-performed move.
    """

    def __init__(
        self, *, status: DraftStatus, action: DraftAction, note: str | None = None
    ) -> None:
        self.status = status
        self.action = action
        super().__init__(
            note or f"a {status.value} draft does not admit {action.value}"
        )


class DraftExportRefusedError(Exception):
    """The export's gate refused after the guard passed; ``reason`` is the
    §5.1.3 name the API answers with (today: ``statement_not_confirmed`` —
    the NAR1 item-16 declaration ``ensure_exportable`` found unconfirmed).

    Named separately from ``DraftTransitionError`` because this is not the
    table's legality but §2.11's additional statement leg, and separately
    from ``DraftInvalidError`` because the API answers it 409 (a conflict
    with the draft's state), not 422 (a malformed request). Raised before
    anything is written, like every export refusal.
    """

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class DraftActionNotWiredError(Exception):
    """The action is in §4.2's table and not in this build's executor.

    ``reason`` is the §5.1.3 name the API answers with — never a silent
    acceptance and never a half-performed transition. The exception is
    currently unreachable: Phase 6 wired the §4.2 table whole and
    ``UNWIRED_REASONS`` is empty (``signature_not_wired``/``approval_not_wired``
    deleted in P6-1/P6-2, ``regenerate_not_wired`` retired in P6-3,
    ``export_not_wired`` in P6-4); it stays because the executor's unwired
    arm and the API's mapping test pin the refusal path for any row a later
    slice adds ahead of its side effects.
    """

    def __init__(self, *, action: DraftAction, reason: str) -> None:
        self.action = action
        self.reason = reason
        super().__init__(f"{action.value} is not wired: {reason}")


@dataclass(frozen=True)
class DraftView:
    """One draft as the reads return it: its current version, and its past.

    ``history`` holds the versions *before* the current one, newest first, as
    metadata only (``version_no``, ``status``, ``created_at``, ``created_by``):
    the lineage is what a reader needs to see ("this was regenerated twice after
    a withdrawal"), and a past version's snapshot is one query per version away
    for anyone who wants it — the current version is the draft.
    """

    current: Draft
    history: list[Draft]

    @property
    def id(self) -> UUID:
        return self.current.id


@dataclass(frozen=True)
class DraftTransitions:
    """The §5.1 transitions read: the draft as it stands, and the actor's actions."""

    row: Draft
    actions: list[AllowedAction]


@dataclass(frozen=True)
class SignaturePackage:
    """The signature-ready package one release produced (scope §2.10).

    The WORM ``generated_draft`` document the filled draft is stored as, and
    the escalations its fill plan raised — the human's checklist for the boxes
    the draft does not state (the presentor's reference, the §1 protected
    class): named, recorded, and never a silent omission.
    """

    document: Document
    escalations: tuple[Escalation, ...]


@dataclass(frozen=True)
class TransitionResult:
    """One performed transition: the draft as it stands now, and what it produced.

    ``signature_package`` is set by ``send_for_signature`` alone — the action
    whose side effect is the release — and is ``None`` for every other action.
    """

    view: DraftView
    signature_package: SignaturePackage | None = None


@dataclass(frozen=True)
class FilingPackageView:
    """One export as the route answers it (§2.8's FilingPackage, §5.1).

    The mode, the WORM ``export_package`` document the primary artifact was
    stored as, the derived envelope (sheet counts, page-number pairs, the
    companion documents' ids), and the exporting actor with the moment — the
    coordinates a reader needs to fetch the package from the records
    registry; the row itself is ``app.adapters.persistence.filing_packages``.
    """

    id: UUID
    draft_id: UUID
    draft_version_no: int
    mode: ExportMode
    document_id: UUID
    envelope: dict[str, Any]
    exported_by: UUID
    exported_at: datetime


def _require_company_access(context: ActorContext, company_id: UUID) -> None:
    """The grant-only gate for the id-scoped routes (no ``company_id`` in path).

    The same single decision ``app.api.deps.require_company_access`` applies to
    company-scoped routes; these routes learn their company from the row, so the
    check happens here instead — one mechanism, one place it is consulted. No
    permission flag: draft reads are the customer's own data (ADR-0012).
    """
    if not can_access_company(context, company_id):
        raise DraftForbiddenError


def _amount(value: object) -> str | None:
    """A ``Numeric`` column as the string the house convention carries it as."""
    return None if value is None else str(value)


# --- The fill plan (F4a) ----------------------------------------------------


def fill_plan_for(
    *,
    form_type: str,
    resolved_state: Mapping[str, Any],
    filing_metadata: Mapping[str, Any],
    private_company_statement: bool | None,
) -> FillPlan:
    """The deterministic fill plan for one draft version's snapshot.

    The one seam where the adapter's plain data meets the domain's planner: the
    version's own ``resolved_state``/``filing_metadata``/statement flag (contract
    §2.8), the pinned registry's items, the shared checkbox option table
    (``app.adapters.pdf.options`` — the same table the extraction reader reads),
    and the registry's sheet pages (``sheet_page_fields``: each continuation
    page's slug and every box it prints, which is what the plan lays a sheet
    copy out against). No session, no writes, no PDF: the writer that turns the
    plan into the official templates takes it up from here.
    """
    registry = load_form_registry(FormCode(form_type))
    items = tuple(
        RegistryItem(
            item=entry.item,
            item_type=entry.item_type,
            target=entry.target,
            pdf_fields=entry.pdf_fields,
        )
        for entry in registry.items
    )
    return plan_fill(
        form=form_type,
        resolved_state=resolved_state,
        filing_metadata=filing_metadata,
        private_company_statement=private_company_statement,
        items=items,
        options=CHECKBOX_OPTIONS,
        sheet_pages=tuple(
            SheetPage(sheet=sheet, fields=fields)
            for sheet, fields in sheet_page_fields(registry).items()
        ),
    )


# --- Generation -------------------------------------------------------------


async def generate_draft(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    company_id: UUID,
    actor_user_id: UUID | None,
    form_type: FormCode,
    target_date: date,
) -> DraftView:
    """Generate one draft version for ``(company, form)`` on ``target_date``.

    The refusals, in the order a caller meets them: an unknown or cross-tenant
    company (``CompanyNotFoundError`` → 404); the §5.1 gate
    (``AutomationDeniedError`` → 403 ``automation_denied: <reason>``, with
    **nothing written** — the gate is consulted before any read of the
    resolution's data); an NAR1 target for a company type whose anniversary rule
    is not established (``DueDateRuleNotEstablishedError`` → 422
    ``nar1_rule_not_established``, before anything is written); and a lost race
    on the version insert (``DraftConflictError`` → 409 ``draft_conflict``).

    One transaction: the version row, the applied events' status moves and both
    audit entries commit or roll back together.
    """
    company = await company_master.get_company(
        session, tenant_id=tenant_id, company_id=company_id
    )
    decision = await service_packages.check_automation(
        session,
        tenant_id=tenant_id,
        company_id=company.id,
        automation_type=AutomationType.AUTOMATED_PREFILL,
        as_of=date.today(),
    )
    if not decision.allowed:
        assert decision.reason is not None  # denied decisions always carry one
        raise AutomationDeniedError(decision.reason)
    row = await _build_version(
        session,
        tenant_id=tenant_id,
        company=company,
        form_type=form_type,
        target_date=target_date,
        actor_user_id=actor_user_id,
    )
    await session.flush()
    return await _lineage(session, tenant_id=tenant_id, draft_id=row.id)


async def regenerate_draft(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    draft_id: UUID,
    actor_user_id: UUID | None,
) -> DraftView:
    """Append the next version of a rejected lineage (§4.2's ``regenerate`` row).

    P6-3 / ruling R3: regeneration **is** generation with a lineage. Called from
    the executor's dispatch after the guard, the ``REVIEW_DRAFTS`` leg and the
    body-shape check that refuses a stated rationale (the row is
    rationale-free — the rejection carried it), it consults the same §5.1 gate
    with ``AUTOMATED_PREFILL`` — a denial raises
    ``AutomationDeniedError`` (403 ``automation_denied: <reason>``) with nothing
    written — and then rebuilds the version from the world as it stands: the
    same company/form/target-date lineage, resolved against the current master
    data and the events still in the approved set (``APPROVED_EVENT_STATUSES``),
    with a fresh snapshot, provenance, QC flags, filing-metadata document,
    re-read registry pin and a clean signature lifecycle and stamp pairs (the
    new row carries none by construction).

    The refused version is not touched: it stays ``rejected`` as the lineage's
    history, its rationale intact. The version append is the move, so the audit
    story is the generation's own — one ``draft_generated`` entry naming the
    superseded version (``regenerated_from_version``), never a second
    ``draft_transition_recorded`` entry.

    One transaction, as generation's: the new version row, the events' status
    moves and the audit entry commit or roll back together.
    """
    row = await _locked_current(session, tenant_id=tenant_id, draft_id=draft_id)
    company = await company_master.get_company(
        session, tenant_id=tenant_id, company_id=row.company_id
    )
    decision = await service_packages.check_automation(
        session,
        tenant_id=tenant_id,
        company_id=company.id,
        automation_type=AutomationType.AUTOMATED_PREFILL,
        as_of=date.today(),
    )
    if not decision.allowed:
        assert decision.reason is not None  # denied decisions always carry one
        raise AutomationDeniedError(decision.reason)
    await _build_version(
        session,
        tenant_id=tenant_id,
        company=company,
        form_type=FormCode(row.form_type),
        target_date=row.target_date,
        actor_user_id=actor_user_id,
        audit_extra={"regenerated": True, "regenerated_from_version": row.version_no},
    )
    await session.flush()
    return await _lineage(session, tenant_id=tenant_id, draft_id=row.id)


async def _build_version(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    company: Company,
    form_type: FormCode,
    target_date: date,
    actor_user_id: UUID | None,
    audit_extra: Mapping[str, object] | None = None,
) -> Draft:
    """Resolve and append one version of ``(company, form)``'s lineage.

    The version-row builder generation and regeneration share: the resolution's
    reads (locked approved events, the as-of master data, F1's in-office
    officers and shareholdings, the prior-filing basis), the domain's
    ``resolve_draft`` and the cross-check, the append at ``version_no + 1`` and
    the events' status moves through the change-events seam — on the caller's
    session, one transaction. Callers consult the §5.1 gate and the §4.2 guard
    themselves, in their own order, before reaching for this.

    ``audit_extra`` merges into the single ``draft_generated`` entry's details
    (regeneration's ``regenerated``/``regenerated_from_version``); a lost race
    on the version insert raises ``DraftConflictError`` and the caller re-reads
    and retries.
    """
    # A function-level import: ``app.application.change_events`` imports this
    # module at module level for §4.1's invalidation leg, so reaching back for
    # the generation path's reads and the system ``apply`` from inside the
    # function keeps the two services' import graph acyclic.
    from app.application import change_events

    made_up_date: date | None = None
    if form_type is FormCode.NAR1:
        # The company-type rule's own refusal: a non-private company has no
        # established anniversary rule here (contract §5.1.3).
        made_up_date = nar1_made_up_date(
            company_type=company.type,
            anniversary_date=company.anniversary_date,
            target_date=target_date,
        )
    # The events first, and locked: the resolution must see the state no other
    # writer can move out from under it (§3.1's approved set).
    event_rows = await change_events.locked_approved_events(
        session, tenant_id=tenant_id, company_id=company.id
    )
    master = await company_master.get_company_master(
        session, tenant_id=tenant_id, company_id=company.id, as_of=target_date
    )
    appointments = await officers.list_appointments(
        session, tenant_id=tenant_id, company_id=company.id, as_of=target_date
    )
    shareholdings = await officers.list_shareholdings(
        session, tenant_id=tenant_id, company_id=company.id, as_of=target_date
    )
    batch_documents = await prior_filing.batch_document_ids(
        session,
        tenant_id=tenant_id,
        batch_ids={
            row.source_id
            for row in _fact_rows(master)
            if row.source_type == "import_batch" and row.source_id is not None
        },
    )
    prior = await prior_filing.latest_batch_values(
        session, tenant_id=tenant_id, company_id=company.id, form=form_type
    )
    registry = load_form_registry(form_type)
    resolution = resolve_draft(
        form_type=form_type,
        target_date=target_date,
        company=_company_input(
            master, company=company, batch_documents=batch_documents
        ),
        officers=[_officer_input(row) for row in appointments],
        shareholdings=[_shareholding_input(row) for row in shareholdings],
        events=[_event_input(row) for row in event_rows],
        made_up_date=made_up_date,
    )
    comparison = prior_filing_comparison(
        values=resolution.values,
        path_sources=resolution.path_sources,
        prior=prior,
    )
    provenance = build_field_provenance(
        ((item.item, item.target) for item in registry.items),
        path_sources=resolution.path_sources,
        changed_paths=comparison.changed_paths,
    )
    draft_id, version_no = await _next_version(
        session, tenant_id=tenant_id, company_id=company.id, form_type=form_type
    )
    row = Draft(
        id=draft_id,
        version_no=version_no,
        tenant_id=tenant_id,
        company_id=company.id,
        form_type=form_type.value,
        target_date=target_date,
        status=DraftStatus.GENERATED.value,
        resolved_state=resolution.resolved_state,
        field_provenance=provenance,
        # §2.9 steps 3-5 first (the resolution's own), then step 6's cross-check.
        qc_flags=[*resolution.qc_flags, *comparison.flags],
        filing_metadata=filing_metadata_template(made_up_date=made_up_date),
        applied_event_ids=resolution.applied_event_ids,
        # ADR-0010's pin: the registry revision this draft was built against.
        form_version={
            "specification_no": registry.specification_no,
            "template_sha256": registry.template_sha256,
        },
        # NAR1 item 16's declaration is pre-filled draft-only (§3.3); the human
        # confirmation it will need before export is a later slice's write.
        private_company_statement=True if form_type is FormCode.NAR1 else None,
        signature_status=SignatureStatus.NOT_SENT.value,
        created_by=actor_user_id,
    )
    session.add(row)
    try:
        await session.flush()
    except IntegrityError:
        # The primary key refused a second row for this (id, version_no): a
        # generation committed between this writer's read and its insert. The
        # caller re-reads and retries (the change_event_conflict remedy).
        raise DraftConflictError from None
    rows_by_id = {event.id: event for event in event_rows}
    for event in resolution.applied_events:
        await change_events.apply_to_draft(
            session,
            row=rows_by_id[event.event_id],
            actor_user_id=actor_user_id,
        )
    details: dict[str, object] = {
        "company_id": company.id,
        "form_type": row.form_type,
        "version_no": row.version_no,
        "target_date": target_date,
        "applied_event_ids": resolution.applied_event_ids,
        "qc_flags": len(row.qc_flags),
    }
    if audit_extra:
        details.update(audit_extra)
    await audit.record_event(
        session,
        tenant_id=tenant_id,
        action=AuditAction.DRAFT_GENERATED,
        actor_user_id=actor_user_id,
        company_id=company.id,
        entity_type="draft",
        entity_id=row.id,
        details=details,
    )
    await session.flush()
    return row


async def _next_version(
    session: AsyncSession, *, tenant_id: UUID, company_id: UUID, form_type: FormCode
) -> tuple[UUID, int]:
    """The lineage's id and the next version number — the append point.

    The company row is locked first: the **first** version has no row of the
    lineage to lock, and without that lock two concurrent first generations
    would mint two lineages for one ``(company, form_type)``. The version read
    that follows is a fresh statement, so a version another generation committed
    while this one waited on the lock is exactly what it sees.

    Nothing enforces a single *live* draft (the plan's ruling): versions coexist
    as history, and the current version is the lineage's highest number.
    """
    await session.execute(
        select(Company.id)
        .where(Company.id == company_id, Company.tenant_id == tenant_id)
        .with_for_update()
    )
    latest = await session.scalar(
        select(Draft)
        .where(
            Draft.tenant_id == tenant_id,
            Draft.company_id == company_id,
            Draft.form_type == form_type.value,
        )
        .order_by(Draft.version_no.desc(), Draft.id)
        .limit(1)
    )
    if latest is None:
        return uuid4(), 1
    return latest.id, latest.version_no + 1


def _fact_rows(master: CompanyMasterSnapshot) -> list[_EffectiveDatedFact]:
    """Every fact row the snapshot resolved, for the provenance lookup."""
    return [
        row
        for row in (
            master.name,
            master.registered_office,
            master.business_nature,
            master.contact,
            master.filing,
            master.records_kept,
            master.share_capital_classes,
        )
        if row is not None
    ]


def _fact_source(row: _EffectiveDatedFact, *, batch_documents: Mapping[UUID, UUID]) -> SourceRef:
    """Where one fact row came from, in the §3.5 vocabulary.

    A fact a change event wrote is *event-sourced* — the value the draft carries
    exists because that event was applied, which is the explanation a reviewer
    needs. A fact an import batch wrote backfills from that batch's stored
    document (the prior filing), and only when the batch's document cannot be
    resolved does the fact row itself answer. Everything else is the row.
    """
    if row.source_type == "change_event" and row.source_id is not None:
        return SourceRef("change_event_id", row.source_id)
    if row.source_type == "import_batch" and row.source_id is not None:
        document_id = batch_documents.get(row.source_id)
        if document_id is not None:
            return SourceRef("backfill_document_id", document_id)
    return SourceRef("master_record_id", row.id)


def _master_fact(
    *,
    row: _EffectiveDatedFact | None,
    values: Mapping[str, object],
    batch_documents: Mapping[UUID, UUID],
) -> DraftMasterFact | None:
    """One resolved fact group as the domain takes it, or ``None`` when absent."""
    if row is None:
        return None
    return DraftMasterFact(
        values=values,
        effective_date=row.effective_date,
        source=_fact_source(row, batch_documents=batch_documents),
    )


def _company_input(
    master: CompanyMasterSnapshot,
    *,
    company: Company,
    batch_documents: Mapping[UUID, UUID],
) -> DraftCompanyInput:
    """The company's statics and fact groups as the resolution takes them.

    The statics come from the anchor row the application already read (the
    snapshot's ``company`` is the same row), so a corrected static is what the
    draft resolves as of the generation — they are not effective-dated and are
    never overridden (§3.1).
    """
    facts: dict[str, DraftMasterFact] = {}
    for group, row, columns in (
        ("name", master.name, _NAME_COLUMNS),
        ("registered_office", master.registered_office, _OFFICE_COLUMNS),
        ("business_nature", master.business_nature, _NATURE_COLUMNS),
        ("contact", master.contact, _CONTACT_COLUMNS),
        ("filing", master.filing, _FILING_COLUMNS),
    ):
        fact = _master_fact(
            row=row,
            values=(
                {column: getattr(row, column) for column in columns} if row is not None else {}
            ),
            batch_documents=batch_documents,
        )
        if fact is not None:
            facts[group] = fact
    records_kept = _master_fact(
        row=master.records_kept,
        values={"records_kept": list(master.records_kept.records_kept or [])}
        if master.records_kept is not None
        else {},
        batch_documents=batch_documents,
    )
    if records_kept is not None:
        facts["records_kept"] = records_kept
    share_capital = _master_fact(
        row=master.share_capital_classes,
        values={"classes": list(master.share_capital_classes.classes or [])}
        if master.share_capital_classes is not None
        else {},
        batch_documents=batch_documents,
    )
    if share_capital is not None:
        facts["share_capital"] = share_capital
    return DraftCompanyInput(
        company_id=company.id,
        statics={
            "br_number": company.br_number,
            "cr_number": company.cr_number,
            "type": company.type,
            "date_of_incorporation": company.date_of_incorporation,
            "anniversary_date": company.anniversary_date,
        },
        status=None if master.status is None else master.status.status,
        facts=facts,
    )


def _person_snapshot(person: Any) -> dict[str, object]:
    """A person's §2.4 identity groups, as the resolution's entries carry them."""
    return {
        "person_id": str(person.id),
        "person_type": person.person_type,
        "name_zh": person.name_zh,
        "name_en": person.name_en,
        "surname_en": person.surname_en,
        "other_names_en": person.other_names_en,
        "previous_names": [dict(entry) for entry in person.previous_names or []],
        "aliases": [dict(entry) for entry in person.aliases or []],
        "email": person.email,
        "br_number": person.br_number,
        "hkid_partial": person.hkid_partial,
        "passport_partial": person.passport_partial,
        "passport_issuing_country_region": person.passport_issuing_country_region,
        "tcsp_licence": None if person.tcsp_licence is None else dict(person.tcsp_licence),
    }


def _officer_input(row: AppointmentWithPerson) -> OfficerInput:
    """One F1 appointment as the resolution takes it."""
    appointment: OfficerAppointment = row.appointment
    return OfficerInput(
        appointment_id=appointment.id,
        person=_person_snapshot(row.person),
        role=appointment.role,
        capacity=list(appointment.capacity or []),
        alternate_to=appointment.alternate_to,
        correspondence_address=appointment.correspondence_address,
        registered_office_address=appointment.registered_office_address,
        valid_from=appointment.valid_from,
        valid_to=appointment.valid_to,
    )


def _shareholding_input(row: ShareholdingWithPerson) -> ShareholdingInput:
    """One F1 holding as the resolution takes it (money as strings)."""
    holding: Shareholding = row.shareholding
    return ShareholdingInput(
        shareholding_id=holding.id,
        person=_person_snapshot(row.person),
        class_name=holding.class_name,
        shares_held=holding.shares_held,
        jointly_held=holding.jointly_held,
        remarks=holding.remarks,
        percentage_held=_amount(holding.percentage_held),
        allotment_date=holding.allotment_date,
        amount_paid=_amount(holding.amount_paid),
        amount_unpaid=_amount(holding.amount_unpaid),
        address=holding.address,
        valid_from=holding.valid_from,
        valid_to=holding.valid_to,
    )


def _event_input(row: Any) -> EventInput:
    """One stored change event as the resolution takes it."""
    return EventInput(
        event_id=row.id,
        type=ChangeEventType(row.type),
        status=ChangeEventStatus(row.status),
        effective_date=row.effective_date,
        payload=dict(row.payload),
    )


# --- Reads ------------------------------------------------------------------


async def _lineage(session: AsyncSession, *, tenant_id: UUID, draft_id: UUID) -> DraftView:
    """One draft's version rows, current first; ``DraftNotFoundError`` if none."""
    rows = list(
        (
            await session.scalars(
                select(Draft)
                .where(Draft.id == draft_id, Draft.tenant_id == tenant_id)
                .order_by(Draft.version_no.desc())
            )
        ).all()
    )
    if not rows:
        raise DraftNotFoundError
    return DraftView(current=rows[0], history=rows[1:])


async def get_draft(
    session: AsyncSession, *, tenant_id: UUID, context: ActorContext, draft_id: UUID
) -> DraftView:
    """One draft: its current version, and the versions before it.

    The current version's document fields are the response's own, so a caller
    that wants today's state reads one object; ``history`` is the lineage's
    metadata. The actor's per-company grant is checked here (the path carries no
    company). A read: no writes, no audit entries.
    """
    view = await _lineage(session, tenant_id=tenant_id, draft_id=draft_id)
    _require_company_access(context, view.current.company_id)
    return view


async def list_drafts(
    session: AsyncSession, *, tenant_id: UUID, company_id: UUID
) -> list[DraftView]:
    """One company's drafts: each lineage's current version, newest first.

    ``CompanyNotFoundError`` for an unknown or cross-tenant company (404), like
    every other company-scoped read. One lineage per ``(company, form_type)`` —
    generation appends to it — so a company has at most one current version per
    form, and the lineage is what the detail read returns. A read: no writes, no
    audit entries.
    """
    await company_master.get_company(session, tenant_id=tenant_id, company_id=company_id)
    rows = list(
        (
            await session.scalars(
                select(Draft)
                .where(Draft.tenant_id == tenant_id, Draft.company_id == company_id)
                .order_by(Draft.version_no.desc())
            )
        ).all()
    )
    by_id: dict[UUID, list[Draft]] = {}
    for row in rows:
        by_id.setdefault(row.id, []).append(row)
    views = [DraftView(current=versions[0], history=versions[1:]) for versions in by_id.values()]
    views.sort(key=lambda view: (view.current.created_at, str(view.current.id)), reverse=True)
    return views


async def transitions_for(
    session: AsyncSession, *, tenant_id: UUID, context: ActorContext, draft_id: UUID
) -> DraftTransitions:
    """The actor's allowed actions on one draft (§5.1).

    Read-only: the current version, then the wired, leg-admitted rows of the
    §4.2 table. The actor's legs are resolved for the draft's company through
    the single RBAC decisions (``is_allowed`` for the two flags,
    ``can_access_company`` for the no-flag leg), so a General account's grants
    and an Administrator's standing are one rule; an actor without the company's
    grant is refused before any action is computed.
    """
    current = await _current_version(session, tenant_id=tenant_id, draft_id=draft_id)
    if current is None:
        raise DraftNotFoundError
    _require_company_access(context, current.company_id)
    return DraftTransitions(
        row=current,
        actions=allowed_actions(
            status=DraftStatus(current.status),
            may_review=is_allowed(context, Permission.REVIEW_DRAFTS, current.company_id),
            may_approve=is_allowed(context, Permission.APPROVE_DRAFTS, current.company_id),
            has_grant=can_access_company(context, current.company_id),
        ),
    )


async def _current_version(
    session: AsyncSession, *, tenant_id: UUID, draft_id: UUID
) -> Draft | None:
    """The draft's current version — the highest ``version_no`` — or ``None``."""
    current: Draft | None = await session.scalar(
        select(Draft)
        .where(Draft.id == draft_id, Draft.tenant_id == tenant_id)
        .order_by(Draft.version_no.desc())
        .limit(1)
    )
    return current


async def _locked_current(session: AsyncSession, *, tenant_id: UUID, draft_id: UUID) -> Draft:
    """The current version, with every writer of the lineage serialized behind us.

    The module's two-statement pattern (``app.application.change_events``): the
    lock is taken over every version row of the lineage — the mutex every writer
    of this draft takes first — and the plain re-read that follows is a fresh
    statement, so the row acted on is the *current* one when a generation
    committed while this transaction waited.
    """
    locked = list(
        (
            await session.scalars(
                select(Draft.version_no)
                .where(Draft.id == draft_id, Draft.tenant_id == tenant_id)
                .order_by(Draft.version_no)
                .with_for_update()
            )
        ).all()
    )
    if not locked:
        raise DraftNotFoundError
    current = await _current_version(session, tenant_id=tenant_id, draft_id=draft_id)
    assert current is not None  # the lock read saw the rows, and nothing deletes them
    return current


# --- The executor -----------------------------------------------------------


async def transition(
    session: AsyncSession,
    store: DocumentStore,
    scanner: Scanner,
    *,
    tenant_id: UUID,
    context: ActorContext,
    draft_id: UUID,
    action: DraftAction,
    actor_user_id: UUID | None,
    stated: Collection[str] = (),
    filing_metadata: Mapping[str, Any] | None = None,
    private_company_statement: bool | None = None,
    rationale: str | None = None,
    confirm_statement: bool | None = None,
    mode: ExportMode | None = None,
) -> TransitionResult:
    """**The one executor** (ADR-0008): lock → guard → leg → write → audit.

    The refusals, in the order a caller meets them, and why that order:

    1. unknown or another tenant's id → ``DraftNotFoundError``;
    2. no per-company access grant → ``DraftForbiddenError`` (deny by default,
       before anything about the draft is used);
    3. an unwired action → ``DraftActionNotWiredError`` with the reason its side
       effects wait on. A property of the build, so it is answered before the
       guard: "the status does not admit it" would send a caller to review a
       draft whose next step does not exist yet;
    4. an action the current status does not admit, a system row, or
       ``upload_signed`` through this door (its performer is the contract's
       dedicated ``POST /drafts/{id}/signature`` route, which a transitions
       body cannot carry — a bare move would record a signature the registry
       does not hold) → ``DraftTransitionError`` (never a no-op, never
       half-performed);
    5. a missing leg — §4.2's ``review_drafts``/``approve_drafts``, or the
       no-flag company grant — → ``DraftForbiddenError``;
    6. a required rationale that did not arrive →
       ``DraftRationaleRequiredError`` (422 ``rationale_required`` — both of
       ``reject``'s legs are §4.2 "rationale required" rows, and a rejection
       without its reason is §2.11's record lost);
    7. the approval gate's statement leg (``approve`` alone) — a draft whose
       ``private_company_statement`` is true refuses without
       ``confirm_statement=true`` (422 ``statement_confirmation_required``)
       and records ``statement_confirmed_by/at`` when it arrives; a draft
       that makes no statement refuses any supplied confirmation (422
       ``statement_not_made``) — ruling R2: the approver's sign-off is the
       one confirmation moment, captured at approval rather than repeated
       at export;
    8. a malformed edit → ``DraftInvalidError`` (422, the reasons verbatim).

    ``stated`` names the edit fields the request actually carried
    (``model_fields_set``), so an absent field keeps what the version holds and
    an explicit ``null`` clears one; ``rationale`` is kept on the version and in
    the chain when given — except on ``regenerate``, whose row is
    rationale-free (the rejection carried it): a stated one is refused exactly
    like the other body's-shape keys (422 ``regenerate: carries rationale``);
    ``confirm_statement`` is ``approve``'s body's shape alone — on any other
    action it is refused exactly like the edit fields
    (``{action}: carries confirm_statement``).

    **``send_for_signature`` is the release**: it fills the pinned template from
    the version's own snapshot (``fill_plan_for`` → ``app.adapters.pdf.filler``),
    stores the PDF through the records registry's write path, moves the draft
    and records both the transition and the package. Its refusals — a missing
    font, a character the embedded font cannot render — are raised **before
    anything is written**, so a refused release leaves the draft where it stood
    and the store untouched; the two *deployment* faults (a ``FillPlanError``,
    the writer's missing-``/Opt`` ``ValueError``) are deliberately not caught
    here either — they propagate as 500s with the same nothing-written property
    (the API module's docstring carries the ruling). The store and scanner are
    taken as parameters (the confirmation generator's precedent): this is the
    one action that writes a document.
    """
    row = await _locked_current(session, tenant_id=tenant_id, draft_id=draft_id)
    _require_company_access(context, row.company_id)
    if not is_wired(action):
        # ``unwired_reason`` answers every action: a mapped reason for the
        # (currently empty) unwired set, ``KeyError`` for a wired one — the
        # executor asks only here, after ``is_wired`` said no.
        reason = unwired_reason(action)
        raise DraftActionNotWiredError(action=action, reason=reason)
    status = DraftStatus(row.status)
    leg = leg_for(status, action)
    # A row the status does not admit, and a system row (no staff leg at all),
    # are the same answer here: this actor cannot perform it from here.
    if not admits(status, action) or leg is None:
        raise DraftTransitionError(status=status, action=action)
    if leg is DraftLeg.REVIEW_DRAFTS and not is_allowed(
        context, Permission.REVIEW_DRAFTS, row.company_id
    ):
        raise DraftForbiddenError
    if leg is DraftLeg.APPROVE_DRAFTS and not is_allowed(
        context, Permission.APPROVE_DRAFTS, row.company_id
    ):
        raise DraftForbiddenError
    if leg is DraftLeg.COMPANY_GRANT and not can_access_company(context, row.company_id):
        raise DraftForbiddenError
    if action is DraftAction.UPLOAD_SIGNED:
        # The row's performer is the contract's dedicated route
        # (``POST /drafts/{id}/signature`` — ``record_signed_upload``), which
        # a transitions body cannot carry: it has no file to store, and a bare
        # status move would record a signature the registry does not hold.
        # The system-row precedent answers the same named refusal — the table
        # admits the row, but not through this door — never a half-perform.
        raise DraftTransitionError(
            status=status,
            action=action,
            note="upload_signed is performed by POST /drafts/{id}/signature",
        )
    if rationale_required(action) and not (rationale or "").strip():
        raise DraftRationaleRequiredError
    if action is DraftAction.APPROVE:
        # The statement leg of the approval gate (ruling R2): the NAR1 item-16
        # declaration, when the draft makes it, is confirmed by the approver
        # in this one moment — nothing is written on either refusal.
        if row.private_company_statement is True:
            if confirm_statement is not True:
                raise DraftInvalidError(["statement_confirmation_required"])
        elif confirm_statement is not None:
            raise DraftInvalidError(["statement_not_made"])
    elif confirm_statement is not None:
        # The confirmation is `approve`'s body's shape alone — the edit-fields
        # pattern, verbatim: anything else carrying it (including `edit`) is
        # a request the caller misunderstood, refused by name before any
        # write, not a key to ignore.
        raise DraftInvalidError([f"{action.value}: carries confirm_statement"])
    if action is DraftAction.EXPORT:
        # Ruling R4: one executor, two doors. `export` through this door
        # carries its mode in the body — without one the export cannot be
        # performed, and the body is refused by name (the canonical door is
        # `POST /drafts/{id}/exports {mode}`; both call the one
        # `export_draft`).
        if mode is None:
            raise DraftInvalidError(["export_mode_required"])
    elif mode is not None:
        # The mode is `export`'s body's shape alone — the confirm_statement
        # pattern verbatim.
        raise DraftInvalidError([f"{action.value}: carries mode"])
    stated_fields = {
        field
        for field in stated
        if field in ("filing_metadata", "private_company_statement")
    }
    if action is DraftAction.EDIT:
        merged, before, after = _edit_the_version(
            row,
            stated_fields=stated_fields,
            filing_metadata=filing_metadata,
            private_company_statement=private_company_statement,
        )
        if rationale is not None:
            row.rationale = rationale
        await _move_row(
            session,
            row=row,
            action=action,
            actor_user_id=actor_user_id,
            rationale=rationale,
            before_values=before,
            after_values=after,
            merged_metadata=merged,
        )
        return TransitionResult(
            view=await _lineage(session, tenant_id=tenant_id, draft_id=row.id)
        )
    if stated_fields or filing_metadata is not None or private_company_statement is not None:
        # The edit fields are the body's shape for `edit` alone: on any other
        # action they are a request the caller misunderstood, not keys to ignore
        # (which is how a "mark_under_review" would silently rewrite values).
        raise DraftInvalidError([f"{action.value}: carries edit fields"])
    if action is DraftAction.REGENERATE:
        # P6-3: ``regenerate`` is generation with a lineage. The version append
        # is the move — this row is not touched (it stays ``rejected``, the
        # lineage's history entry) — the §5.1 gate is the generation seam's
        # own (a denial writes nothing), and the audit story is the
        # generation's one ``draft_generated`` entry, not a transition record.
        # The row is rationale-free (the rejection carried it), so a stated
        # rationale is refused by name — the confirm_statement body's-shape
        # pattern — never silently dropped.
        if rationale is not None:
            raise DraftInvalidError([f"{action.value}: carries rationale"])
        view = await regenerate_draft(
            session, tenant_id=tenant_id, draft_id=draft_id, actor_user_id=actor_user_id
        )
        return TransitionResult(view=view)
    if action is DraftAction.EXPORT:
        # P6-4: the submission-package generator. The guard and the
        # ``approve_drafts`` leg above are the §4.2 door; ``export_draft``
        # re-asks them for the dedicated route's callers (the
        # ``record_signed_upload`` precedent) and then assembles, stores and
        # moves on this one session. The response is the moved draft, as for
        # every transition through this door — the FilingPackage itself is
        # the dedicated route's answer. The row is rationale-free (like the
        # regenerate's): an export either goes out or it does not, and a
        # stated note would be silently dropped by the arm's early return —
        # refused by name, the ``regenerate: carries rationale`` precedent.
        assert mode is not None  # the body's-shape check above refuses the absent mode
        if rationale is not None:
            raise DraftInvalidError([f"{action.value}: carries rationale"])
        await export_draft(
            session,
            store,
            scanner,
            tenant_id=tenant_id,
            context=context,
            draft_id=draft_id,
            mode=mode,
            actor_user_id=actor_user_id,
        )
        return TransitionResult(
            view=await _lineage(session, tenant_id=tenant_id, draft_id=draft_id)
        )
    package: SignaturePackage | None = None
    if action is DraftAction.SEND_FOR_SIGNATURE:
        package = await _release_for_signature(
            session, store, scanner, row=row, actor_user_id=actor_user_id
        )
    if rationale is not None:
        row.rationale = rationale
    await _move_row(
        session,
        row=row,
        action=action,
        actor_user_id=actor_user_id,
        rationale=rationale,
        before_values=None,
        after_values=None,
        merged_metadata=None,
        statement_confirmed=confirm_statement is True,
    )
    if package is not None:
        await _record_package(
            session, row=row, package=package, actor_user_id=actor_user_id
        )
    return TransitionResult(
        view=await _lineage(session, tenant_id=tenant_id, draft_id=row.id),
        signature_package=package,
    )


async def _release_for_signature(
    session: AsyncSession,
    store: DocumentStore,
    scanner: Scanner,
    *,
    row: Draft,
    actor_user_id: UUID | None,
) -> SignaturePackage:
    """Plan, fill and store one draft version's signature-ready package.

    The brief's own order: the plan (F4a's ``fill_plan_for`` over the version's
    stored snapshot, filing metadata and statement flag), the fill (the writer
    over the pinned template), then the store through the records registry's
    write path — ``origin=generated``, ``kind=generated_draft``, the tenant's
    ``generated`` naming template with this version's number, and the registry's
    own form code as ``{Form}`` (contract §5.3). The row's status move and both
    audit entries are the caller's, after this returns: nothing is stored for a
    fill that refused, and the release's own writes are on the caller's session
    either way.

    The escalations travel back with the document: they are the fill's ledger
    of what a human still has to complete by hand (scope §2.9 step 5), and the
    release is not refused for them — a draft whose presentor is not yet stated
    is ordinary work in progress, not an error.
    """
    form = FormCode(row.form_type)
    plan = fill_plan_for(
        form_type=row.form_type,
        resolved_state=row.resolved_state,
        filing_metadata=row.filing_metadata,
        private_company_statement=row.private_company_statement,
    )
    registry = load_form_registry(form)
    pdf = fill_template(
        template_path(form),
        plan,
        header_fields=page_header_fields(registry),
        sheet_pages=sheet_pages(registry),
    )
    document = await documents.register_upload(
        session,
        store,
        tenant_id=row.tenant_id,
        company_id=row.company_id,
        actor_user_id=actor_user_id,
        kind=DocumentKind.GENERATED_DRAFT,
        origin=DocumentOrigin.GENERATED,
        content_type="application/pdf",
        data=pdf,
        scanner=scanner,
        form=registry.form_code,
        version=row.version_no,
    )
    return SignaturePackage(document=document, escalations=tuple(plan.escalations))


async def _record_package(
    session: AsyncSession,
    *,
    row: Draft,
    package: SignaturePackage,
    actor_user_id: UUID | None,
) -> None:
    """Append the release's own audit entry, beside the transition's.

    The document's own ``document_registered`` entry is the registry's and the
    status move's is the transition's; this one ties the two together and
    carries what only the fill knows — the escalations a human reads as their
    checklist. Their words are recorded (the draft row itself holds the values
    they are about); the chain stays a record of what the product did.
    """
    await audit.record_event(
        session,
        tenant_id=row.tenant_id,
        action=AuditAction.DRAFT_SIGNATURE_PACKAGE_GENERATED,
        actor_user_id=actor_user_id,
        company_id=row.company_id,
        entity_type="draft",
        entity_id=row.id,
        details={
            "draft_id": str(row.id),
            "version_no": row.version_no,
            "document_id": str(package.document.id),
            "storage_key": package.document.storage_key,
            "sha256": package.document.sha256,
            "escalations": [
                {"kind": escalation.kind, "detail": escalation.detail}
                for escalation in package.escalations
            ],
        },
    )


async def record_signed_upload(
    session: AsyncSession,
    store: DocumentStore,
    scanner: Scanner,
    *,
    tenant_id: UUID,
    context: ActorContext,
    draft_id: UUID,
    actor_user_id: UUID | None,
    data: bytes,
    content_type: str,
) -> DraftView:
    """Store the client-signed copy and move the draft to ``signature_received``.

    The refusals, in the order a caller meets them — the executor's own order,
    because this is the ``upload_signed`` row's leg performed by the route the
    contract names for it (``POST /drafts/{id}/signature``) rather than by the
    transitions POST:

    1. unknown or another tenant's id → ``DraftNotFoundError``;
    2. no per-company access grant → ``DraftForbiddenError`` (the row's only
       leg, §4.2's no-flag "staff (any General)", is the grant);
    3. a status that is not ``sent_for_signature`` → ``DraftTransitionError``
       (never a no-op, and the double-upload idempotence refusal: the second
       upload lands on ``signature_received``, which admits nothing).

    Then the side effect, mirroring the release's order: the signed copy goes
    through the records registry's write path — validated (declared type +
    magic bytes, scope §8), scanned, hashed, stored under the tenant's
    ``document`` naming template as a WORM ``signed_draft`` document
    (``origin=upload``; the kind's default ``statutory_filing`` retention) —
    and every validation or scanner refusal raises **before** the status move,
    so a refused upload writes no document, moves nothing, and appends no
    audit entry. The document write and the move ride this one session: the
    signed copy and the draft's new state commit or roll back together, and
    the transition's audit entry names the document it was.
    """
    row = await _locked_current(session, tenant_id=tenant_id, draft_id=draft_id)
    _require_company_access(context, row.company_id)
    status = DraftStatus(row.status)
    if not admits(status, DraftAction.UPLOAD_SIGNED):
        raise DraftTransitionError(status=status, action=DraftAction.UPLOAD_SIGNED)
    document = await documents.register_upload(
        session,
        store,
        tenant_id=row.tenant_id,
        company_id=row.company_id,
        actor_user_id=actor_user_id,
        kind=DocumentKind.SIGNED_DRAFT,
        origin=DocumentOrigin.UPLOAD,
        content_type=content_type,
        data=data,
        scanner=scanner,
        form=row.form_type,
    )
    await _move_row(
        session,
        row=row,
        action=DraftAction.UPLOAD_SIGNED,
        actor_user_id=actor_user_id,
        rationale=None,
        before_values=None,
        after_values=None,
        merged_metadata=None,
        extra_details={"document_id": str(document.id)},
    )
    return await _lineage(session, tenant_id=tenant_id, draft_id=row.id)


async def export_draft(
    session: AsyncSession,
    store: DocumentStore,
    scanner: Scanner,
    *,
    tenant_id: UUID,
    context: ActorContext,
    draft_id: UUID,
    mode: ExportMode,
    actor_user_id: UUID | None,
) -> FilingPackageView:
    """Assemble, store and record one approved draft's submission package.

    The refusals, in the order a caller meets them — the executor's own order,
    because the transitions door performs this same function after the same
    guard and leg (ruling R4: one executor, two doors; the dedicated
    ``POST /drafts/{id}/exports`` route is the canonical one):

    1. unknown or another tenant's id → ``DraftNotFoundError``;
    2. no per-company access grant → ``DraftForbiddenError``;
    3. a status that is not ``approved`` → ``DraftTransitionError`` (§4.2's
       hard gate — the export is unreachable from anywhere else, and the
       second-export idempotence refusal: ``exported`` admits nothing);
    4. the ``approve_drafts`` leg → ``DraftForbiddenError``;
    5. the statement half of §2.11's gate → ``DraftExportRefusedError``
       (409 ``statement_not_confirmed``, the domain rule ``ensure_exportable``
       shared with ``approve``'s confirmation).

    Then the assembly, mirroring the release's order so its doctrine holds
    verbatim: the fill plan over the version's own snapshot, the fill of the
    pinned template — for the two PDF modes — and the **flatten** of that
    filled package (ADR-0004: fields stay editable through the signature
    workflow; the filing itself is flat), or the deterministic api-ready JSON
    for ``api_ready``. Every fill refusal — the plan, the font, the glyph
    coverage, the template — raises **before any store write**, so a refused
    export leaves the draft where it stood, the store untouched and the chain
    silent; the two ``FillPlanError``/writer-``ValueError`` deployment faults
    propagate the same way the release's do (raised, never smoothed, with
    nothing written).

    Then the writes, all on this one session: the mode's companion document
    (the e-Registry helper, the paper checklist — WORM ``export_package``
    documents, ``text/markdown``) and the primary artifact (the flattened
    PDF, or the JSON — ``export_package``, ``application/pdf``/``json``),
    each under the tenant's ``generated`` naming template with ``{Version}``
    and ``{Form}`` (the release precedent); the ``filing_packages`` row with
    the derived envelope; the applied events' ``submit`` through the
    change-events seam (§4.1's system row — an event already out of the
    applied set is skipped, never double-moved); the audit chain's
    ``draft_exported`` entry (mode, package, documents, envelope, submitted
    events); and the row's move to ``exported``. One transaction: the
    package, the events' filing and the move commit or roll back together.
    """
    row = await _locked_current(session, tenant_id=tenant_id, draft_id=draft_id)
    _require_company_access(context, row.company_id)
    status = DraftStatus(row.status)
    if not admits(status, DraftAction.EXPORT):
        raise DraftTransitionError(status=status, action=DraftAction.EXPORT)
    if not is_allowed(context, Permission.APPROVE_DRAFTS, row.company_id):
        raise DraftForbiddenError
    statement_refusal = ensure_exportable(row)
    if statement_refusal is not None:
        raise DraftExportRefusedError(statement_refusal)

    form = FormCode(row.form_type)
    registry = load_form_registry(form)
    company = await company_master.get_company(
        session, tenant_id=tenant_id, company_id=row.company_id
    )
    plan = fill_plan_for(
        form_type=row.form_type,
        resolved_state=row.resolved_state,
        filing_metadata=row.filing_metadata,
        private_company_statement=row.private_company_statement,
    )
    # The assembly: every refusal below raises before any document registers.
    pdf = b""
    if mode is not ExportMode.API_READY:
        pdf = fill_template(
            template_path(form),
            plan,
            header_fields=page_header_fields(registry),
            sheet_pages=sheet_pages(registry),
        )
        pdf = flatten_filled_pdf(pdf)
    helper_document: Document | None = None
    checklist_document: Document | None = None
    if mode is ExportMode.E_REGISTRY_PREP:
        helper_document = await documents.register_upload(
            session,
            store,
            tenant_id=row.tenant_id,
            company_id=row.company_id,
            actor_user_id=actor_user_id,
            kind=DocumentKind.EXPORT_PACKAGE,
            origin=DocumentOrigin.GENERATED,
            content_type="text/markdown",
            data=ered_registry_helper_text(
                company=company,
                form_code=registry.form_code,
                filing_metadata=row.filing_metadata,
            ).encode("utf-8"),
            scanner=scanner,
            form=registry.form_code,
            version=row.version_no,
        )
    if mode is ExportMode.PAPER:
        checklist_document = await documents.register_upload(
            session,
            store,
            tenant_id=row.tenant_id,
            company_id=row.company_id,
            actor_user_id=actor_user_id,
            kind=DocumentKind.EXPORT_PACKAGE,
            origin=DocumentOrigin.GENERATED,
            content_type="text/markdown",
            data=paper_checklist_text(
                company=company,
                form_code=registry.form_code,
                filing_metadata=row.filing_metadata,
            ).encode("utf-8"),
            scanner=scanner,
            form=registry.form_code,
            version=row.version_no,
        )
    if mode is ExportMode.API_READY:
        artifact = api_ready_bytes(row=row)
        content_type = "application/json"
    else:
        artifact = pdf
        content_type = "application/pdf"
    envelope = build_envelope(
        plan=plan,
        helper_document_id=None if helper_document is None else helper_document.id,
        checklist_document_id=None if checklist_document is None else checklist_document.id,
    )
    primary_document = await documents.register_upload(
        session,
        store,
        tenant_id=row.tenant_id,
        company_id=row.company_id,
        actor_user_id=actor_user_id,
        kind=DocumentKind.EXPORT_PACKAGE,
        origin=DocumentOrigin.GENERATED,
        content_type=content_type,
        data=artifact,
        scanner=scanner,
        form=registry.form_code,
        version=row.version_no,
    )
    # The export is a staff action — the routes always carry the actor — and
    # the package records who exported it (NOT NULL at the database).
    assert actor_user_id is not None
    package_row = FilingPackage(
        tenant_id=row.tenant_id,
        draft_id=row.id,
        draft_version_no=row.version_no,
        mode=mode.value,
        document_id=primary_document.id,
        envelope=envelope,
        exported_by=actor_user_id,
        exported_at=datetime.now(UTC),
    )
    session.add(package_row)
    await session.flush()
    # §4.1's system row: the filing carries the events the draft applied.
    # A function-level import, like ``_build_version``'s: this module and
    # ``app.application.change_events`` reach for each other, and one side
    # imports at module level so the graph stays acyclic.
    from app.application import change_events

    applied_ids = [UUID(value) for value in (row.applied_event_ids or [])]
    submitted_event_ids = await change_events.submit_applied_events(
        session, tenant_id=row.tenant_id, applied_event_ids=applied_ids
    )
    await audit.record_event(
        session,
        tenant_id=row.tenant_id,
        action=AuditAction.DRAFT_EXPORTED,
        actor_user_id=actor_user_id,
        company_id=row.company_id,
        entity_type="draft",
        entity_id=row.id,
        details={
            "draft_id": str(row.id),
            "version_no": row.version_no,
            "mode": mode.value,
            "filing_package_id": str(package_row.id),
            "document_id": str(primary_document.id),
            "helper_document_id": (
                None if helper_document is None else str(helper_document.id)
            ),
            "checklist_document_id": (
                None if checklist_document is None else str(checklist_document.id)
            ),
            "envelope": envelope,
            "submitted_event_ids": [str(event_id) for event_id in submitted_event_ids],
        },
    )
    await _move_row(
        session,
        row=row,
        action=DraftAction.EXPORT,
        actor_user_id=actor_user_id,
        rationale=None,
        before_values=None,
        after_values=None,
        merged_metadata=None,
        extra_details={
            "mode": mode.value,
            "filing_package_id": str(package_row.id),
        },
    )
    return FilingPackageView(
        id=package_row.id,
        draft_id=row.id,
        draft_version_no=row.version_no,
        mode=mode,
        document_id=primary_document.id,
        envelope=envelope,
        exported_by=package_row.exported_by,
        exported_at=package_row.exported_at,
    )


def _edit_the_version(
    row: Draft,
    *,
    stated_fields: set[str],
    filing_metadata: Mapping[str, Any] | None,
    private_company_statement: bool | None,
) -> tuple[dict[str, Any], dict[str, object], dict[str, object]]:
    """The edit's merged metadata and its before/after values, or a refusal.

    The merge is the domain's (``merge_filing_metadata``: one level into
    ``return``/``presentor``/``signature``/``consent``, an absent key keeping
    what is stored), and the *merged* document is what is validated — a request
    that corrects one key of a document has to leave the whole of it legal. An
    edit that states nothing is refused: a revision that would say exactly what
    the version already says is a no-op, and ``mark_under_review`` is the action
    for "just move it".
    """
    if not stated_fields:
        raise DraftInvalidError(["edit: nothing to change"])
    merged = dict(row.filing_metadata)
    if "filing_metadata" in stated_fields:
        if not isinstance(filing_metadata, Mapping):
            raise DraftInvalidError(["filing_metadata_invalid: filing_metadata: not an object"])
        merged = merge_filing_metadata(row.filing_metadata, filing_metadata)
    violations = filing_metadata_violations(merged)
    if violations:
        raise DraftInvalidError(violations)
    before: dict[str, object] = {
        "status": row.status,
        "filing_metadata": row.filing_metadata,
        "private_company_statement": row.private_company_statement,
    }
    row.filing_metadata = merged
    if "private_company_statement" in stated_fields:
        row.private_company_statement = private_company_statement
    after: dict[str, object] = {
        "status": DraftStatus.UNDER_REVIEW.value,
        "filing_metadata": row.filing_metadata,
        "private_company_statement": row.private_company_statement,
    }
    return merged, before, after


async def _move_row(
    session: AsyncSession,
    *,
    row: Draft,
    action: DraftAction,
    actor_user_id: UUID | None,
    rationale: str | None,
    before_values: Mapping[str, object] | None,
    after_values: Mapping[str, object] | None,
    merged_metadata: Mapping[str, Any] | None,
    extra_details: Mapping[str, object] | None = None,
    statement_confirmed: bool = False,
) -> None:
    """Write one §4.2 transition and append its audit entry.

    The status move is the table's; the edit's values are applied first (the
    caller merged and validated them), and the entry carries the action, the
    before/after — statuses always, and the edit's values when the action has
    any — and the rationale when one was given (§2.11: the reviewer's and
    approver's reasoning is captured in the audit trail, not only on the row a
    later write can move).

    **The stamp the target carries is written and the ones it does not carry
    are cleared** — the change-events convention (``app.application.
    change_events._move_row``), transcribed: ``mark_reviewed`` records the
    review pair, ``approve`` the approval pair and — when the approver
    confirmed the item-16 statement (``statement_confirmed``) — the statement
    pair; a landing outside a set clears it (a rejection, a recall, a later
    regeneration or invalidation carries no stamp it did not set). The sets
    are the domain's (``REVIEW_STAMP_STATUSES``/``APPROVAL_STAMP_STATUSES``/
    ``STATEMENT_CARRIED_STATUSES``); the pair is set or cleared whole, the
    migration 0026 paired-null CHECKs' rule.

    ``send_for_signature`` moves the signature lifecycle with the status — the
    client's signature is what the draft now waits on (§2.8's
    ``signature_status``) — so that pair is the entry's before/after values;
    ``upload_signed`` (P6-1) moves it again when the client-signed copy's
    upload stores the WORM document and lands the draft on
    ``signature_received``.
    """
    current = DraftStatus(row.status)
    target = DRAFT_TRANSITIONS[current][action]
    before = row.status
    now = datetime.now(UTC)
    if merged_metadata is not None:
        row.filing_metadata = dict(merged_metadata)
    if action is DraftAction.SEND_FOR_SIGNATURE:
        before_values = {"signature_status": row.signature_status}
        row.signature_status = SignatureStatus.SENT_FOR_SIGNATURE.value
        after_values = {"signature_status": row.signature_status}
    if action is DraftAction.UPLOAD_SIGNED:
        before_values = {"signature_status": row.signature_status}
        row.signature_status = SignatureStatus.SIGNATURE_RECEIVED.value
        after_values = {"signature_status": row.signature_status}
    if target in REVIEW_STAMP_STATUSES:
        if action is DraftAction.MARK_REVIEWED:
            row.reviewed_by = actor_user_id
            row.reviewed_at = now
    else:
        row.reviewed_by = None
        row.reviewed_at = None
    if target in APPROVAL_STAMP_STATUSES:
        if action is DraftAction.APPROVE:
            row.approved_by = actor_user_id
            row.approved_at = now
    else:
        row.approved_by = None
        row.approved_at = None
    if target in STATEMENT_CARRIED_STATUSES:
        if action is DraftAction.APPROVE and statement_confirmed:
            row.statement_confirmed_by = actor_user_id
            row.statement_confirmed_at = now
    else:
        row.statement_confirmed_by = None
        row.statement_confirmed_at = None
    row.status = target.value
    await session.flush()
    # ``updated_at`` is the database's own (``onupdate=func.now()``), so the
    # UPDATE expires it and this reloads the moment the database wrote.
    await session.refresh(row, attribute_names=["updated_at"])
    details: dict[str, object] = {
        "action": action.value,
        "before": before,
        "after": row.status,
        "version_no": row.version_no,
    }
    if extra_details:
        details.update(extra_details)
    if before_values is not None and after_values is not None:
        details["before_values"] = before_values
        details["after_values"] = after_values
    if rationale is not None:
        details["rationale"] = rationale
    await audit.record_event(
        session,
        tenant_id=row.tenant_id,
        action=AuditAction.DRAFT_TRANSITION_RECORDED,
        actor_user_id=actor_user_id,
        company_id=row.company_id,
        entity_type="draft",
        entity_id=row.id,
        details=details,
    )


# --- §4.1's invalidation leg ------------------------------------------------


async def invalidate_for_event(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    event_id: UUID,
    reason: str,
    actor_user_id: UUID | None = None,
) -> list[UUID]:
    """Invalidate the drafts one change event is linked to (§4.1, §2.8).

    Called by the change-events executor when an applied event is reverted,
    edited, deleted or withdrawn. For every draft lineage whose **current
    version** carries the event in ``applied_event_ids`` — the §4.1 "draft
    linkage recorded", which is what makes this a lookup rather than a scan of
    every snapshot — the version moves to ``invalidated`` with the moment and
    the reason (``change event <id> <action>``, the event named in the row), and
    its own audit entry is appended on the same session.

    Two statuses are left alone, each for a stated reason: an **exported** draft
    is a filing already made — undoing it silently would erase the record that
    something was filed, and the event's own entry is what a reader follows
    instead — and an **invalidated** one is idempotence (a second act on the
    same event changes nothing). Earlier versions of a lineage are history and
    keep the status they stood at, exactly as a change event's revision rows do.

    Returns the ids of the drafts it invalidated (the caller's audit narrative).
    """
    latest = (
        select(
            Draft.id.label("id"),
            func.max(Draft.version_no).label("version_no"),
        )
        .where(
            Draft.tenant_id == tenant_id,
            Draft.applied_event_ids.contains([str(event_id)]),
        )
        .group_by(Draft.id)
        .subquery()
    )
    rows = list(
        (
            await session.scalars(
                select(Draft)
                .join(
                    latest,
                    and_(
                        Draft.id == latest.c.id,
                        Draft.version_no == latest.c.version_no,
                    ),
                )
                .where(
                    Draft.tenant_id == tenant_id,
                    Draft.status.not_in(_INVALIDATION_SKIPPED),
                )
            )
        ).all()
    )
    now = datetime.now(UTC)
    invalidated: list[UUID] = []
    for row in rows:
        before = row.status
        row.status = DraftStatus.INVALIDATED.value
        row.invalidated_at = now
        row.invalidation_reason = f"change event {event_id} {reason}"[:300]
        # The stamp convention's one rule (``_move_row``): ``invalidated``
        # carries no stamp, so an approved-then-invalidated draft's review,
        # approval and statement pairs go with the move — the row must not
        # keep telling a story the status has overruled.
        row.reviewed_by = None
        row.reviewed_at = None
        row.approved_by = None
        row.approved_at = None
        row.statement_confirmed_by = None
        row.statement_confirmed_at = None
        await session.flush()
        await audit.record_event(
            session,
            tenant_id=tenant_id,
            action=AuditAction.DRAFT_INVALIDATED,
            actor_user_id=actor_user_id,
            company_id=row.company_id,
            entity_type="draft",
            entity_id=row.id,
            details={
                "draft_id": str(row.id),
                "version_no": row.version_no,
                "event_id": str(event_id),
                "action": reason,
                "before": before,
                "after": row.status,
            },
        )
        invalidated.append(row.id)
    await session.flush()
    return invalidated


__all__ = [
    "AutomationDeniedError",
    "DraftActionNotWiredError",
    "DraftConflictError",
    "DraftExportRefusedError",
    "DraftForbiddenError",
    "DraftInvalidError",
    "DraftNotFoundError",
    "DraftRationaleRequiredError",
    "DraftTransitionError",
    "DraftTransitions",
    "DraftView",
    "FilingPackageView",
    "SignaturePackage",
    "TransitionResult",
    "export_draft",
    "generate_draft",
    "get_draft",
    "invalidate_for_event",
    "list_drafts",
    "record_signed_upload",
    "regenerate_draft",
    "transition",
    "transitions_for",
]
