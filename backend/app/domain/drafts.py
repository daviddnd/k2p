"""The draft lifecycle and the resolution engine (scope §2.9, §2.11; data-model
§2.8; contract §4.2, §5.1, appendix A).

A draft is what the auto-drafter produces: the company's particulars as the
form would state them on a target date, resolved from master data plus the
approved change events whose effective dates have arrived, with per-registry-
item provenance and the prior-filing cross-check's flags. This module owns the
part of that which is pure — the words, the table, and the resolution rules.
The application layer (``app.application.drafts``) reads the rows, calls
``resolve_draft``, and writes the version; nothing here touches a database.

**The §4.2 table, as data.** ``DRAFT_TRANSITIONS`` is the contract's table
transcribed — every From/Action/To row, the filler's and Phase 6's included,
because ADR-0008 requires the machine to encode the spec whole and be tested
against it. The guard over it is the *generic* ``can_transition``
(``app.domain.service_renewals``), reused rather than copied: this is the third
lifecycle behind one guard. Two rows need a word of explanation:

- **"any pre-``exported``" + ``invalidate``** is spelled out over the seven
  statuses §4.2's wording covers (``generated`` … ``rejected``), each moving to
  ``invalidated``. It is the **system leg**: §4.1's withdrawal/deletion of an
  applied event is its trigger, and no staff account performs it — the
  transitions read never offers it.
- **``rejected`` + ``regenerate``** targets ``generated``. The "new version" in
  §4.2's Side-effects column is the generation path's own behaviour
  (``app.application.drafts`` appends the next version), not a second status.

**The actor column, as a second table.** ``ACTION_PERMISSIONS`` is keyed by
(status, action) for the same reason the change events' is: the same action
carries different legs at different statuses (``reject`` is a reviewer's from
``signature_received`` and an approver's from ``reviewed``). Both draft
permission flags are here, plus the one leg §4.2 gives without a flag —
``upload_signed`` is "staff (any General)", which every master-data write in
this build already expresses as the company access grant
(``app.domain.rbac.can_access_company``, founder decision 2026-08-24). That leg
is ``DraftLeg.COMPANY_GRANT``, pinned here so the executor asks the same single
decision the master-data routes do. A **system row** has ``None``: the
``invalidate`` rows admit no staff actor at all, and "Administrator may perform
any action" is not a row here — it is ``app.domain.rbac.is_allowed``'s own rule.

**Wired, and refused by name.** ``edit``, ``mark_under_review``, the system
``invalidate`` and — since F4b — ``send_for_signature``, whose side effect is
the signature-ready package (``app.adapters.pdf.filler`` through
``app.application.drafts``); P6-1 added the client's half of that story —
``upload_signed``, whose side effect is the stored client-signed copy (a WORM
``signed_draft`` document through the records registry's write path), and
``recall``, the plain walk-back needing no side effect at all; P6-2 wired the
two-layer review/approval gate (scope §2.11) — ``mark_reviewed`` (the review
stamp), ``reject`` at both of its legs (rationale required, the §2.11 record
of the decision) and ``approve`` (the approval stamp, and the item-16
statement confirmation via the body's ``confirm_statement``); P6-3 wired
``regenerate`` — generation with a lineage: the rejected version stays as the
lineage's history and the seam appends the next version, resolved fresh from
current master data and the still-approved events; and P6-4 wired the last
row — ``export``, whose side effect is the submission package (the
``filing_packages`` row, the flattened or JSON artifact stored as a WORM
``export_package`` document, the applied events' ``submit``, and the move to
``exported``). Every row is wired now, and ``UNWIRED_REASONS`` is empty —
never accepted, never half-performed, and never offered by the
allowed-actions read. Entry states the build cannot reach are still in the
table, exactly as the change events' are.

**The dashboard's set.** ``AWAITING_REVIEW_STATUSES`` names the statuses that
count as "awaiting staff review" for the dashboard's pending-draft-review
widget — ``generated`` and ``under_review`` — the mirror of the change events'
own constant: ``invalidated`` and everything past review are not staff-review
work. The constant is the one statement of that decision.

**The resolution rules (scope §2.9 steps 1-6).** ``resolve_draft`` takes plain
values — the company statics and fact groups as of the target date, F1's
in-office appointments and shareholdings, and the company's approved change
events — and returns the snapshot, the path-indexed value map the provenance is
derived from, the applied events and the flags §2.9 steps 3-5 raise. The rules
it implements, pinned:

- **Relevance and effective dates (§3.1).** An event applies iff its status is
  ``confirmed`` or ``applied_to_draft``, its ``effective_date`` is on or before
  the target date, and the form takes its type: **NAR1** takes officer
  appointments and cessations and ``registered_office_change``; **ND2A** takes
  the officer events only (a registered-office move is NR1's change, v1.1). An
  applied event's value overrides a master value only when its effective date
  is **newer** than the master value's — a fact's ``effective_date``, an
  officer's or member's ``valid_from``, the registered office's fact date. The
  company statics are not effective-dated and are never overridden.
- **The officer overlay (§3.2).** A cessation closes the in-office record it
  names — matched on the partial HKID when the payload carries one (exact,
  trimmed), else on the name pair, **and on the role the event's type carries**
  (``TYPE_ROLES``: a ``director_*`` event names a directorship or a reserve
  directorship, a ``secretary_*`` event the secretaryship) — and only when the
  cessation date is newer than that record's start. The role leg is what keeps
  the common sole-director-and-secretary shape resolvable: identity alone would
  match both offices and make every such cessation ambiguous. An appointment
  adds its officer as a new entry unless the same person already holds the
  **same office** (the same role — §2.4 lets one person hold several offices).
  A match that is absent, ambiguous or a duplicate leaves the event
  **unapplied** with a named flag rather than guessing which office was meant.
- **The registered-office overlay (§3.1).** A ``registered_office_change``
  replaces the resolved office when its effective date is newer than the fact
  it would replace (or when there is no office fact at all).
- **Shareholders and share capital (§3.3).** Members are F1's holdings as of
  the target date — **no event overlay**, because v1.0 has no shareholding
  event type — and the totals are summed over the classes and marked derived.

**Provenance (§3.5) is mechanical.** Every value the resolution places is
registered under the appendix-A path the registry targets name, with the source
it came from (the fact/appointment/shareholding row, the change event, or — for
a fact an import batch wrote — the batch's document). One entry is produced per
registry item by splitting its target on `` + `` and looking up the parts; a
part event-sourced wins (it is the explanation), then a backfill document, then
the record itself.

**The QC vocabulary (§3.6)** is three structured flag kinds — ``missing_source``
(§2.9 step 5's critical fields with no value), ``unapplied_event`` (an event the
overlay could not apply) and ``unexpected_change`` (a path the prior filing and
this draft both name, differing without an applied event explaining it). The
comparison's canonicalisation reuses the prior-filing extraction's own
normalisers (``app.domain.prior_filing``), so "the same value" means what it
means on the extraction path.

Pure and framework-free per ADR-0006: the standard library and the pure domain
modules it builds on (``app.domain.change_events`` for the event vocabulary,
``app.domain.officers`` for the service window, ``app.domain.prior_filing`` for
the target paths and normalisers, ``app.domain.compliance`` for the anniversary
rule and its refusal, ``app.domain.rbac`` for the leg vocabulary).
"""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Any, Protocol
from uuid import UUID

from app.domain.change_events import TYPE_ROLES, ChangeEventStatus, ChangeEventType
from app.domain.compliance import anniversary_in_year, nar1_due_date
from app.domain.officers import in_office
from app.domain.prior_filing import (
    FormCode,
    InvalidFieldValueError,
    individual_paths,
    is_supported_target,
    normalize_value,
)
from app.domain.rbac import Permission
from app.domain.service_renewals import can_transition


class DraftStatus(StrEnum):
    """Where a draft stands (§4.2). Nine values, every one a row's target."""

    GENERATED = "generated"
    UNDER_REVIEW = "under_review"
    SENT_FOR_SIGNATURE = "sent_for_signature"
    SIGNATURE_RECEIVED = "signature_received"
    REVIEWED = "reviewed"
    APPROVED = "approved"
    EXPORTED = "exported"
    REJECTED = "rejected"
    INVALIDATED = "invalidated"


# The statuses the dashboard's pending-draft-review widget counts: work waiting
# on staff — generated and not yet looked at, or under review. The mirror of
# ``app.domain.change_events.AWAITING_REVIEW_STATUSES``, for the same reasons:
# ``invalidated`` is a decision already made (the §4.1 legs), everything past
# review is the signature/approval gates' work rather than this widget's, and
# ``exported`` is filed. The constant is the one statement of that decision, so
# a later read of the same set cannot drift from it.
AWAITING_REVIEW_STATUSES: frozenset[DraftStatus] = frozenset(
    {DraftStatus.GENERATED, DraftStatus.UNDER_REVIEW}
)


class SignatureStatus(StrEnum):
    """``signature_status`` (§2.8): the client-signature lifecycle.

    It rides beside the draft's own status because the two are different
    questions — what the TCSP is doing with the draft, and where the client's
    signature stands — and §4.2's signature rows are the only actions that move
    it. All three values are in the vocabulary; this build writes ``not_sent``
    (the column's default) at generation, ``sent_for_signature`` when the
    release moves the draft (F4b's ``send_for_signature``), and
    ``signature_received`` when the client-signed copy's upload moves it
    (P6-1's ``upload_signed``, scope §2.10).
    """

    NOT_SENT = "not_sent"
    SENT_FOR_SIGNATURE = "sent_for_signature"
    SIGNATURE_RECEIVED = "signature_received"


class DraftAction(StrEnum):
    """Every action the §4.2 table names (the request body's vocabulary).

    The full vocabulary, unwired members included: the API must *accept* the
    name to refuse it with its named 409 — an action it could not parse would be
    a 422 about a value the product's own contract prints.
    """

    EDIT = "edit"
    MARK_UNDER_REVIEW = "mark_under_review"
    SEND_FOR_SIGNATURE = "send_for_signature"
    UPLOAD_SIGNED = "upload_signed"
    RECALL = "recall"
    MARK_REVIEWED = "mark_reviewed"
    REJECT = "reject"
    APPROVE = "approve"
    EXPORT = "export"
    REGENERATE = "regenerate"
    INVALIDATE = "invalidate"


class DraftLeg(StrEnum):
    """The §4.2 "Actor permission" column's staff legs.

    The two draft permission flags are the flags themselves; the third value is
    the one leg §4.2 gives without a flag — ``upload_signed`` is "staff (any
    General)", which this build expresses as the company access grant, the same
    decision every master-data route asks
    (``app.domain.rbac.can_access_company``; the founder decision of
    2026-08-24). A system row has no entry of its own: see ``ACTION_PERMISSIONS``.
    """

    REVIEW_DRAFTS = Permission.REVIEW_DRAFTS.value
    APPROVE_DRAFTS = Permission.APPROVE_DRAFTS.value
    COMPANY_GRANT = "company_grant"


# §4.2 as data: for every status, the actions it admits and the status each
# moves to. The rows are the contract's, in its order.
DRAFT_TRANSITIONS: Mapping[DraftStatus, Mapping[DraftAction, DraftStatus]] = {
    DraftStatus.GENERATED: {
        DraftAction.EDIT: DraftStatus.UNDER_REVIEW,
        DraftAction.MARK_UNDER_REVIEW: DraftStatus.UNDER_REVIEW,
        DraftAction.INVALIDATE: DraftStatus.INVALIDATED,
    },
    DraftStatus.UNDER_REVIEW: {
        DraftAction.SEND_FOR_SIGNATURE: DraftStatus.SENT_FOR_SIGNATURE,
        DraftAction.INVALIDATE: DraftStatus.INVALIDATED,
    },
    DraftStatus.SENT_FOR_SIGNATURE: {
        DraftAction.UPLOAD_SIGNED: DraftStatus.SIGNATURE_RECEIVED,
        DraftAction.RECALL: DraftStatus.UNDER_REVIEW,
        DraftAction.INVALIDATE: DraftStatus.INVALIDATED,
    },
    DraftStatus.SIGNATURE_RECEIVED: {
        DraftAction.MARK_REVIEWED: DraftStatus.REVIEWED,
        DraftAction.REJECT: DraftStatus.REJECTED,
        DraftAction.INVALIDATE: DraftStatus.INVALIDATED,
    },
    DraftStatus.REVIEWED: {
        DraftAction.APPROVE: DraftStatus.APPROVED,
        DraftAction.REJECT: DraftStatus.REJECTED,
        DraftAction.INVALIDATE: DraftStatus.INVALIDATED,
    },
    DraftStatus.APPROVED: {
        DraftAction.EXPORT: DraftStatus.EXPORTED,
        DraftAction.INVALIDATE: DraftStatus.INVALIDATED,
    },
    DraftStatus.REJECTED: {
        DraftAction.REGENERATE: DraftStatus.GENERATED,
        DraftAction.INVALIDATE: DraftStatus.INVALIDATED,
    },
    DraftStatus.EXPORTED: {},
    DraftStatus.INVALIDATED: {},
}

# §4.2's "Actor permission" column, keyed by (status, action). `None` is the
# system's row — no staff actor may perform it at all, Administrator included
# (the Administrator rule is `is_allowed`'s and would otherwise have to be
# restated here).
ACTION_PERMISSIONS: Mapping[tuple[DraftStatus, DraftAction], DraftLeg | None] = {
    (DraftStatus.GENERATED, DraftAction.EDIT): DraftLeg.REVIEW_DRAFTS,
    (DraftStatus.GENERATED, DraftAction.MARK_UNDER_REVIEW): DraftLeg.REVIEW_DRAFTS,
    (DraftStatus.UNDER_REVIEW, DraftAction.SEND_FOR_SIGNATURE): DraftLeg.REVIEW_DRAFTS,
    (DraftStatus.SENT_FOR_SIGNATURE, DraftAction.UPLOAD_SIGNED): DraftLeg.COMPANY_GRANT,
    (DraftStatus.SENT_FOR_SIGNATURE, DraftAction.RECALL): DraftLeg.REVIEW_DRAFTS,
    (DraftStatus.SIGNATURE_RECEIVED, DraftAction.MARK_REVIEWED): DraftLeg.REVIEW_DRAFTS,
    (DraftStatus.SIGNATURE_RECEIVED, DraftAction.REJECT): DraftLeg.REVIEW_DRAFTS,
    (DraftStatus.REVIEWED, DraftAction.APPROVE): DraftLeg.APPROVE_DRAFTS,
    (DraftStatus.REVIEWED, DraftAction.REJECT): DraftLeg.APPROVE_DRAFTS,
    (DraftStatus.APPROVED, DraftAction.EXPORT): DraftLeg.APPROVE_DRAFTS,
    (DraftStatus.REJECTED, DraftAction.REGENERATE): DraftLeg.REVIEW_DRAFTS,
    (DraftStatus.GENERATED, DraftAction.INVALIDATE): None,
    (DraftStatus.UNDER_REVIEW, DraftAction.INVALIDATE): None,
    (DraftStatus.SENT_FOR_SIGNATURE, DraftAction.INVALIDATE): None,
    (DraftStatus.SIGNATURE_RECEIVED, DraftAction.INVALIDATE): None,
    (DraftStatus.REVIEWED, DraftAction.INVALIDATE): None,
    (DraftStatus.APPROVED, DraftAction.INVALIDATE): None,
    (DraftStatus.REJECTED, DraftAction.INVALIDATE): None,
}

# The actions this slice implements; everything else is refused with the reason
# its side effects wait on (never accepted, never half-performed). Since P6-4
# the §4.2 table is fully wired: ``export`` joined the set — its side effect is
# the FilingPackage (``app.application.drafts``' ``export_draft``: the filled
# package flattened and stored, the ``filing_packages`` row, the applied
# events' ``submit``, and the move to ``exported``) — so the vocabulary's
# unwired half is empty and ``unwired_reason`` answers every action with
# ``KeyError`` (the partition is the vocabulary's whole; the executor asks
# only after ``is_wired`` said no, and the API tests import the function for
# that contract).
WIRED_ACTIONS: frozenset[DraftAction] = frozenset(
    {
        DraftAction.EDIT,
        DraftAction.MARK_UNDER_REVIEW,
        DraftAction.INVALIDATE,
        DraftAction.SEND_FOR_SIGNATURE,
        DraftAction.UPLOAD_SIGNED,
        DraftAction.RECALL,
        DraftAction.MARK_REVIEWED,
        DraftAction.REJECT,
        DraftAction.APPROVE,
        DraftAction.REGENERATE,
        DraftAction.EXPORT,
    }
)

# The named refusal for each unwired action (contract §5.1.3). Empty since
# P6-4: ``export_not_wired`` retired when the export landed — every §4.2 row
# is wired now. The map stays (the API tests import it and assert the empty
# partition), exactly as ``unwired_reason`` stays for the wired-side
# ``KeyError`` contract. History, for the reader: ``send_for_signature``
# wired in F4b, ``upload_signed``/``recall`` in P6-1, ``mark_reviewed``/
# ``reject``/``approve`` in P6-2, ``regenerate`` in P6-3, and ``export``
# last — each retiring its ``*_not_wired`` reason with it, so
# ``signature_not_wired``, ``approval_not_wired`` and
# ``regenerate_not_wired`` are long deleted from the vocabulary.
UNWIRED_REASONS: Mapping[DraftAction, str] = {}

# The review/approval stamp convention (§2.11; the change-events rule
# transcribed): **reaching a status clears the stamps it does not carry**.
# ``reviewed_by/at`` exist while the draft stands ``reviewed`` or beyond —
# the review happened and nothing has undone it; ``approved_by/at`` and the
# statement pair exist from ``approved`` onward. A landing outside a set
# clears its pair (a rejection, a recall, a regeneration, an invalidation),
# so the row always tells one story about who said what. The sets are the
# domain rule; the write itself is the application layer's ``_move_row``.
REVIEW_STAMP_STATUSES: frozenset[DraftStatus] = frozenset(
    {DraftStatus.REVIEWED, DraftStatus.APPROVED, DraftStatus.EXPORTED}
)
APPROVAL_STAMP_STATUSES: frozenset[DraftStatus] = frozenset(
    {DraftStatus.APPROVED, DraftStatus.EXPORTED}
)
STATEMENT_CARRIED_STATUSES: frozenset[DraftStatus] = frozenset(
    {DraftStatus.APPROVED, DraftStatus.EXPORTED}
)


class _ExportGateRow(Protocol):
    """The row face ``ensure_exportable`` reads (the persistence ``Draft``
    satisfies it): the three columns the export gate's rule consults."""

    form_type: str
    private_company_statement: bool | None
    statement_confirmed_by: UUID | None


def ensure_exportable(row: _ExportGateRow) -> str | None:
    """The statement half of §2.11's export gate, as one rule.

    An NAR1 draft whose ``private_company_statement`` is true — item 16's
    declaration, pre-filled draft-only (§3.3) — is refused export until the
    statement is human-confirmed (``statement_confirmed_by/at``, recorded by
    ``approve``'s ``confirm_statement``): the answer is the §5.1.3 refusal
    code, ``statement_not_confirmed``. Everything else — the flag false or
    absent, another form — has nothing to confirm and answers ``None``. The
    approved hard gate is the §4.2 guard's own job (``admits(approved,
    export)``); this rule is the additional statement leg, domain-owned so
    the export route and the executor share it.
    """
    if (
        row.form_type == FormCode.NAR1.value
        and row.private_company_statement is True
        and row.statement_confirmed_by is None
    ):
        return "statement_not_confirmed"
    return None

# §4.2's "rationale required" rows: both rejections. The requirement is why the
# row exists — a draft refused by a reviewer or an approver is a decision a
# reader must be able to understand — and it is §2.11's record of the approver's
# reasoning.
_RATIONALE_ACTIONS: frozenset[DraftAction] = frozenset({DraftAction.REJECT})

# §2.8's member-particulars delivery vocabulary (§3.3's disposition).
DELIVERY_CHOICES: tuple[str, ...] = ("schedule_1", "schedule_2", "cd_rom")


@dataclass(frozen=True)
class AllowedAction:
    """One action the actor may perform on a draft right now.

    What the §5.1 transitions read returns and the frontend renders buttons
    from — the action, where it moves the draft, and whether a rationale comes
    with it.
    """

    action: DraftAction
    to_status: DraftStatus
    rationale_required: bool


def admits(status: DraftStatus, action: DraftAction) -> bool:
    """Whether the §4.2 table admits ``action`` from ``status``.

    The shared guard (``app.domain.service_renewals.can_transition``) asked of
    this lifecycle's table — legality only; *who* may act is the API layer's
    single RBAC decision and the actor's leg below.
    """
    return can_transition(DRAFT_TRANSITIONS, status=status, action=action)


def leg_for(status: DraftStatus, action: DraftAction) -> DraftLeg | None:
    """The leg §4.2 gives this row, or ``None`` for the system's.

    ``None`` covers two cases callers must not confuse: a system row (no staff
    actor at all) and a row the table does not admit (no leg because there is no
    row) — both are non-pressable, which is what the allowed-actions read asks.
    """
    return ACTION_PERMISSIONS.get((status, action))


def is_wired(action: DraftAction) -> bool:
    """Whether this build implements the action (the executor's split)."""
    return action in WIRED_ACTIONS


def unwired_reason(action: DraftAction) -> str:
    """The named §5.1.3 refusal for an unwired action.

    Raises ``KeyError`` for a wired action: the partition is the vocabulary's
    whole, so a caller asking for a wired action's waiting reason has mixed
    the two halves up (the executor asks only after ``is_wired`` said no).
    """
    return UNWIRED_REASONS[action]


def rationale_required(action: DraftAction) -> bool:
    """Whether §4.2 marks this action "rationale required"."""
    return action in _RATIONALE_ACTIONS


def allowed_actions(
    *, status: DraftStatus, may_review: bool, may_approve: bool, has_grant: bool
) -> list[AllowedAction]:
    """The wired, leg-admitted rows from ``status`` — the §5.1 read's model.

    ``may_review``/``may_approve``/``has_grant`` are the actor's legs as the API
    layer resolved them (``is_allowed`` for the two flags,
    ``can_access_company`` for the no-flag leg, each against the draft's
    company — so an Administrator arrives with all three true). An unwired row
    is never offered (the executor would refuse it, and a button that always
    409s is not an allowed action) and a system row (``invalidate``) is offered
    to nobody. The order is §4.2's own row order, because the table's insertion
    order is its text.
    """
    offered: list[AllowedAction] = []
    for action, target in DRAFT_TRANSITIONS.get(status, {}).items():
        if not is_wired(action):
            continue
        leg = leg_for(status, action)
        if leg is DraftLeg.REVIEW_DRAFTS and not may_review:
            continue
        if leg is DraftLeg.APPROVE_DRAFTS and not may_approve:
            continue
        if leg is DraftLeg.COMPANY_GRANT and not has_grant:
            continue
        if leg is None:
            continue
        offered.append(
            AllowedAction(
                action=action,
                to_status=target,
                rationale_required=rationale_required(action),
            )
        )
    return offered


# --- The resolution's inputs ------------------------------------------------

# The provenance vocabulary §2.8 names, plus the empty answer §3.5 gives a part
# nothing resolved: `none` with a null id.
SOURCE_MASTER_RECORD = "master_record_id"
SOURCE_CHANGE_EVENT = "change_event_id"
SOURCE_BACKFILL = "backfill_document_id"
SOURCE_NONE = "none"

# The statuses an event may carry and still apply (§3.1): approved, and not yet
# filed. `submit` — Phase 6 — is what takes an event out of this set.
APPROVED_EVENT_STATUSES: frozenset[ChangeEventStatus] = frozenset(
    {ChangeEventStatus.CONFIRMED, ChangeEventStatus.APPLIED_TO_DRAFT}
)

_APPOINTMENT_TYPES: frozenset[ChangeEventType] = frozenset(
    {ChangeEventType.DIRECTOR_APPOINTMENT, ChangeEventType.SECRETARY_APPOINTMENT}
)
_CESSATION_TYPES: frozenset[ChangeEventType] = frozenset(
    {ChangeEventType.DIRECTOR_CESSATION, ChangeEventType.SECRETARY_CESSATION}
)

# The form → event-type relevance table (§3.1). NAR1's officer and
# registered-office boxes are the two things its target date resolves; ND2A is
# a *notice of change* — its blocks are the events themselves, and a
# registered-office move belongs to NR1 (v1.1), not to this notice.
_FORM_EVENT_TYPES: Mapping[FormCode, frozenset[ChangeEventType]] = {
    FormCode.NAR1: _APPOINTMENT_TYPES
    | _CESSATION_TYPES
    | {ChangeEventType.REGISTERED_OFFICE_CHANGE},
    FormCode.ND2A: _APPOINTMENT_TYPES | _CESSATION_TYPES,
}

# The payload key each officer event type carries (the grammar's own split).
_EVENT_BODY_KEY: Mapping[ChangeEventType, str] = {
    ChangeEventType.DIRECTOR_APPOINTMENT: "appointment",
    ChangeEventType.SECRETARY_APPOINTMENT: "appointment",
    ChangeEventType.DIRECTOR_CESSATION: "cessation",
    ChangeEventType.SECRETARY_CESSATION: "cessation",
}

# §2.9 step 5's critical set: the fields a draft cannot be filled without, per
# form. Each inner tuple is an "at least one of" group; the flag names the
# group's first path when none of its alternatives has a value.
MADE_UP_DATE_PATH = "filing_metadata.return.made_up_date"
_CRITICAL_PATHS: Mapping[FormCode, tuple[tuple[str, ...], ...]] = {
    FormCode.NAR1: (
        ("company.name_en", "company.name_zh"),
        ("company.br_number",),
        ("company.type",),
        ("company.registered_office.flat",),
        ("company.registered_office.building",),
        ("company.registered_office.street",),
        ("company.registered_office.district",),
        (MADE_UP_DATE_PATH,),
    ),
    FormCode.ND2A: (
        ("company.name_en", "company.name_zh"),
        ("company.br_number",),
        ("company.type",),
        ("company.registered_office.flat",),
        ("company.registered_office.building",),
        ("company.registered_office.street",),
        ("company.registered_office.district",),
    ),
}


@dataclass(frozen=True)
class SourceRef:
    """Where one resolved value came from (§2.8's vocabulary).

    ``source_type`` is one of ``master_record_id``, ``change_event_id`` or
    ``backfill_document_id``; ``source_id`` is the row, the event or the
    document. The empty answer (``none``/null) is not a reference — a part
    nothing resolved simply has no entry.
    """

    source_type: str
    source_id: UUID


@dataclass(frozen=True)
class MasterFact:
    """One resolved effective-dated fact group: its values and where it came from.

    ``effective_date`` is the fact row's own date — the registered-office
    overlay compares against it (§3.1). ``source`` is *this* fact row's when the
    fact was written by hand or by a change event; a fact an import batch wrote
    is handed over already resolved as its batch's document
    (``backfill_document_id``), because finding the batch's document is a read
    the application layer makes.
    """

    values: Mapping[str, object]
    effective_date: date
    source: SourceRef


@dataclass(frozen=True)
class CompanyInput:
    """The company statics, status and fact groups as of the target date.

    ``statics`` carries ``br_number``, ``cr_number``, ``type``,
    ``date_of_incorporation`` and ``anniversary_date`` — the anchor's own
    columns, which are not effective-dated and never overridden. ``facts`` is
    keyed by group: ``name``, ``registered_office``, ``business_nature``,
    ``contact``, ``filing``, ``records_kept``, ``share_capital``; a group with
    nothing in force is simply absent.
    """

    company_id: UUID
    statics: Mapping[str, object]
    status: str | None
    facts: Mapping[str, MasterFact]


@dataclass(frozen=True)
class OfficerInput:
    """One in-office appointment with its person snapshot (F1's read).

    ``person`` carries the §2.4 identity groups the registry's officer boxes
    print (see ``app.application.drafts`` for the snapshot's keys).
    """

    appointment_id: UUID
    person: Mapping[str, object]
    role: str
    capacity: Sequence[str]
    alternate_to: str | None
    correspondence_address: Mapping[str, object] | None
    registered_office_address: Mapping[str, object] | None
    valid_from: date
    valid_to: date | None


@dataclass(frozen=True)
class ShareholdingInput:
    """One in-office member position with its person snapshot (F1's read)."""

    shareholding_id: UUID
    person: Mapping[str, object]
    class_name: str
    shares_held: int
    jointly_held: str | None
    remarks: str | None
    percentage_held: str | None
    allotment_date: date | None
    amount_paid: str | None
    amount_unpaid: str | None
    address: Mapping[str, object] | None
    valid_from: date
    valid_to: date | None


@dataclass(frozen=True)
class EventInput:
    """One approved change event, as the resolution reads it."""

    event_id: UUID
    type: ChangeEventType
    status: ChangeEventStatus
    effective_date: date
    payload: Mapping[str, object]


@dataclass(frozen=True)
class Resolution:
    """What one generation resolved: the snapshot, its provenance, the flags.

    ``values`` and ``path_sources`` are the machinery §3.5 derives item
    provenance from — one entry per appendix-A path the registry targets name,
    the source being the fact/appointment/shareholding row, the change event, or
    a batch's document. ``applied_events`` is the events that entered the
    snapshot, in effective-date order: the ids generation links on the draft
    (``applied_event_ids``) and moves ``confirmed → applied_to_draft``.
    ``qc_flags`` carries the flags the resolution itself raises (steps 3-5); the
    prior-filing cross-check's ``unexpected_change`` flags are appended by
    ``prior_filing_comparison``.
    """

    resolved_state: dict[str, Any]
    values: dict[str, Any]
    path_sources: dict[str, list[SourceRef]]
    applied_events: list[EventInput]
    qc_flags: list[dict[str, Any]] = field(default_factory=list)

    @property
    def applied_event_ids(self) -> list[str]:
        """The applied events' ids, as the draft's JSONB list carries them."""
        return [str(event.event_id) for event in self.applied_events]


# --- The pure rules ---------------------------------------------------------


def is_relevant(form_type: FormCode, event_type: ChangeEventType) -> bool:
    """Whether the form takes this event type at all (§3.1)."""
    return event_type in _FORM_EVENT_TYPES[form_type]


def is_approved(status: ChangeEventStatus) -> bool:
    """Whether the status is approved-and-not-yet-submitted (§3.1)."""
    return status in APPROVED_EVENT_STATUSES


def applies_to_draft(
    *,
    form_type: FormCode,
    event_type: ChangeEventType,
    status: ChangeEventStatus,
    effective_date: date,
    target_date: date,
) -> bool:
    """§3.1's three legs: approved, relevant, and effective as of the target date."""
    return (
        is_approved(status) and is_relevant(form_type, event_type) and effective_date <= target_date
    )


def nar1_made_up_date(*, company_type: str, anniversary_date: date, target_date: date) -> date:
    """The NAR1 made-up date for a draft targeting ``target_date``.

    The latest anniversary of incorporation on or before the target date: the
    anniversary's month/day in the target date's year, stepping back one year
    when that date is later than the target (the NAR1 is made up to the
    anniversary — the filing's own date, CR PAM 8A's arithmetic, and the day the
    return's period runs from). Raises ``DueDateRuleNotEstablishedError`` for a
    company type with no established anniversary rule — asked of the NAR1 rule
    itself (``app.domain.compliance.nar1_due_date``) rather than restated, so the
    two cannot disagree about which types this product dates by anniversary.
    """
    nar1_due_date(anniversary_date, target_date.year, company_type=company_type)
    candidate = anniversary_in_year(anniversary_date, target_date.year)
    if candidate <= target_date:
        return candidate
    return anniversary_in_year(anniversary_date, target_date.year - 1)


def same_officer(*, record: Mapping[str, object], officer: Mapping[str, object]) -> bool:
    """§3.2's identity match: the partial HKID first, else the name pair.

    When the payload carries an ``hkid_partial`` the match is that value (exact,
    trimmed) — an identification that is not the same person's is not a match,
    whatever the names say. Otherwise the two sides' **name pairs** must be
    equal: the Chinese name (trimmed), and the English name — the ``name_en``
    when it is stated, else the ``surname_en``/``other_names_en`` parts joined,
    on both sides, so a record stored as one English name and a payload written
    as two parts still match. Two nameless sides never match: a match on nothing
    is not a match.
    """
    partial = _text(officer.get("hkid_partial"))
    if partial is not None:
        return _text(record.get("hkid_partial")) == partial
    record_names = _name_pair(record)
    officer_names = _name_pair(officer)
    if record_names == (None, None) or officer_names == (None, None):
        return False
    return record_names == officer_names


def _text(value: object) -> str | None:
    """A trimmed non-blank text, or ``None`` (the payload/row convention)."""
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _name_pair(values: Mapping[str, object]) -> tuple[str | None, str | None]:
    """A person or payload's (Chinese name, English name) as §3.2 matches them."""
    chinese = _text(values.get("name_zh"))
    english = _text(values.get("name_en"))
    if english is None:
        parts = [
            part
            for part in (_text(values.get("surname_en")), _text(values.get("other_names_en")))
            if part is not None
        ]
        english = " ".join(parts) or None
    return (chinese, english)


def _as_list(value: object) -> list[Any]:
    """A stored JSON list as a list (a value that is not one reads as empty)."""
    return list(value) if isinstance(value, list) else []


def _json_value(value: object) -> Any:
    """The value as JSON holds it: ISO dates, string decimals, plain structures.

    Applied on registration, so the resolution's ``values`` map is exactly what
    a JSONB column can store — and so a date or a ``Decimal`` never reaches the
    prior-filing comparison in a shape the normalisers cannot read.
    """
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value


class _Registration:
    """The values/sources maps as one object, so registration is one call.

    ``register`` places a value where nothing has been placed; ``override``
    replaces one an applied event has superseded (the registered-office
    overlay). Both walk the value's structure, which is what makes §3.5's
    mechanical lookup possible: an officer entry registered at ``officers[]``
    answers ``officers[].name_zh``, ``officers[].correspondence_address.flat``
    and ``officers[].previous_names[].name_en`` without a per-field list.
    """

    def __init__(self) -> None:
        self.values: dict[str, Any] = {}
        self.path_sources: dict[str, list[SourceRef]] = {}

    def register(self, path: str, value: object, ref: SourceRef) -> None:
        """Place a value, keeping whatever is already there."""
        self._place(path, value, ref, replace=False)

    def override(self, path: str, value: object, ref: SourceRef) -> None:
        """Place a value, replacing what is already there (an applied event's)."""
        self._place(path, value, ref, replace=True)

    def _place(self, path: str, value: object, ref: SourceRef, *, replace: bool) -> None:
        value = _json_value(value)
        if replace:
            self.values[path] = value
        else:
            self.values.setdefault(path, value)
        self.path_sources.setdefault(path, []).append(ref)
        if isinstance(value, Mapping):
            for key, item in value.items():
                self._place(f"{path}.{key}", item, ref, replace=replace)
        elif isinstance(value, list):
            # A list already addressed with the registry's ``[]`` keeps that
            # path for its elements (``share_capital.classes[]`` holds the
            # classes); a bare list gains one (``previous_names`` holds the
            # pairs at ``previous_names[]``).
            element_path = path if path.endswith("[]") else f"{path}[]"
            for item in value:
                self._place(element_path, item, ref, replace=replace)


def _source_for_path(
    path: str, path_sources: Mapping[str, Sequence[SourceRef]]
) -> SourceRef | None:
    """The one source a path is explained by — the §3.5 priority order.

    An applied change event first (it is the *explanation* a reviewer needs),
    then the batch's document a backfilled fact came from, then the first record
    in resolution order.
    """
    refs = path_sources.get(path)
    if not refs:
        return None
    for ref in refs:
        if ref.source_type == SOURCE_CHANGE_EVENT:
            return ref
    for ref in refs:
        if ref.source_type == SOURCE_BACKFILL:
            return ref
    return refs[0]


# --- The resolution engine --------------------------------------------------


def resolve_draft(
    *,
    form_type: FormCode,
    target_date: date,
    company: CompanyInput,
    officers: Sequence[OfficerInput],
    shareholdings: Sequence[ShareholdingInput],
    events: Sequence[EventInput],
    made_up_date: date | None,
) -> Resolution:
    """Resolve one draft (§2.9 steps 1-5): the snapshot, provenance and flags.

    Pure: every input is a plain value the application layer read. The returned
    ``qc_flags`` are this resolution's own (unapplied events, then the missing
    critical fields); the prior-filing cross-check's flags are
    ``prior_filing_comparison``'s, appended by the caller in that order.
    """
    registration = _Registration()
    flags: list[dict[str, Any]] = []
    state = _company_state(registration, company=company)
    _register_company_event_paths(registration, company)
    entries, applied_officers = _resolve_officers(
        registration,
        officers=officers,
        events=events,
        form_type=form_type,
        target_date=target_date,
        flags=flags,
    )
    state["officers"] = [_serialize_entry(entry) for entry in entries]
    state["shareholders"] = _resolve_shareholdings(registration, shareholdings=shareholdings)
    applied = _apply_events(
        registration,
        state=state,
        company=company,
        form_type=form_type,
        target_date=target_date,
        events=events,
        applied_officers=applied_officers,
        flags=flags,
    )
    state["changes"] = [
        {
            "event_id": str(event.event_id),
            "type": event.type.value,
            "effective_date": event.effective_date.isoformat(),
            "payload": _json_value(event.payload),
        }
        for event in applied
    ]
    flags.extend(
        {"kind": "missing_source", "path": path}
        for path in critical_missing_paths(
            form_type=form_type, values=registration.values, made_up_date=made_up_date
        )
    )
    return Resolution(
        resolved_state=state,
        values=registration.values,
        path_sources=registration.path_sources,
        applied_events=applied,
        qc_flags=flags,
    )


def _company_state(registration: _Registration, *, company: CompanyInput) -> dict[str, Any]:
    """The ``company`` block of the snapshot, and its paths (§3.4).

    Every fact group is registered whether or not it is present, and the values
    inside a present record are registered even when null: a nullable column the
    record does not state is still a fact about the record, and the cross-check
    needs it to see a value the prior filing had and this draft does not. A
    group with nothing in force is absent from the statics' side, so §3.5's "no
    parts resolvable" answer names its items.
    """
    statics = company.statics
    statics_ref = SourceRef(SOURCE_MASTER_RECORD, company.company_id)
    for path, key in (
        ("company.br_number", "br_number"),
        ("company.cr_number", "cr_number"),
        ("company.type", "type"),
        ("company.date_of_incorporation", "date_of_incorporation"),
        ("company.anniversary_date", "anniversary_date"),
    ):
        registration.register(path, statics.get(key), statics_ref)

    name = company.facts.get("name")
    if name is not None:
        for path, key in (
            ("company.name_en", "name_en"),
            ("company.name_zh", "name_zh"),
            ("company.business_name", "business_name"),
        ):
            registration.register(path, name.values.get(key), name.source)
    business_nature = company.facts.get("business_nature")
    if business_nature is not None:
        for path, key in (
            ("company.business_nature_code", "business_nature_code"),
            ("company.business_nature_description", "business_nature_description"),
        ):
            registration.register(path, business_nature.values.get(key), business_nature.source)
    contact = company.facts.get("contact")
    if contact is not None:
        for path, key in (
            ("company.email", "email"),
            ("company.contact_phone_hk", "contact_phone_hk"),
        ):
            registration.register(path, contact.values.get(key), contact.source)
    filing = company.facts.get("filing")
    if filing is not None:
        for path, key in (
            ("company.mortgages_and_charges_total", "mortgages_and_charges_total"),
            ("company.member_count", "member_count"),
        ):
            registration.register(path, filing.values.get(key), filing.source)
    records_kept = company.facts.get("records_kept")
    if records_kept is not None:
        registration.register(
            "company.records_kept[]",
            _as_list(records_kept.values.get("records_kept")),
            records_kept.source,
        )
    office = company.facts.get("registered_office")
    if office is not None:
        for part in ("flat", "building", "street", "district", "country"):
            registration.register(
                f"company.registered_office.{part}", office.values.get(part), office.source
            )
    share_capital = company.facts.get("share_capital")
    classes: list[dict[str, Any]] = []
    if share_capital is not None:
        classes = [
            dict(entry)
            for entry in _as_list(share_capital.values.get("classes"))
            if isinstance(entry, Mapping)
        ]
        registration.register("share_capital.classes[]", classes, share_capital.source)
    return {
        "company": {
            "br_number": statics.get("br_number"),
            "cr_number": statics.get("cr_number"),
            "type": statics.get("type"),
            "date_of_incorporation": _json_value(statics.get("date_of_incorporation")),
            "anniversary_date": _json_value(statics.get("anniversary_date")),
            "name": (
                None
                if name is None
                else {
                    "name_en": name.values.get("name_en"),
                    "name_zh": name.values.get("name_zh"),
                    "business_name": name.values.get("business_name"),
                }
            ),
            "registered_office": (
                None
                if office is None
                else _json_value(
                    {
                        part: office.values.get(part)
                        for part in ("flat", "building", "street", "district", "country")
                        if office.values.get(part) is not None
                    }
                )
            ),
            "business_nature": (
                None
                if business_nature is None
                else {
                    "business_nature_code": business_nature.values.get("business_nature_code"),
                    "business_nature_description": business_nature.values.get(
                        "business_nature_description"
                    ),
                }
            ),
            "contact": (
                None
                if contact is None
                else {
                    "email": contact.values.get("email"),
                    "contact_phone_hk": contact.values.get("contact_phone_hk"),
                }
            ),
            "filing": (
                None
                if filing is None
                else {
                    "mortgages_and_charges_total": _json_value(
                        filing.values.get("mortgages_and_charges_total")
                    ),
                    "member_count": filing.values.get("member_count"),
                }
            ),
            "records_kept": (
                []
                if records_kept is None
                else _json_value(_as_list(records_kept.values.get("records_kept")))
            ),
            "share_capital": {
                "classes": _json_value(classes),
                "total": _capital_total(classes),
            },
            "status": company.status,
        }
    }


def _capital_total(classes: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """The share-capital total row: summed over the classes, marked derived.

    §2.3: ``share_capital.total.*`` is derived, never stored. The three figures
    are summed over the classes that state them (a class that does not state an
    amount contributes nothing, and a total with nothing to sum is null rather
    than a misleading zero); the currency is named only when every class agrees
    on one, because a mixed list's total is not one currency's amount — the
    >4-class / mixed-currency case is the filler's manual-escalation boundary,
    not a number this product invents.
    """
    if not classes:
        return {
            "derived": True,
            "currency": None,
            "total_shares": None,
            "total_amount": None,
            "total_paid_up": None,
        }
    currencies = {entry.get("currency") for entry in classes}
    shares = [entry.get("total_shares") for entry in classes]
    amounts = [entry.get("total_amount") for entry in classes]
    paid_up = [entry.get("total_paid_up") for entry in classes]
    return {
        "derived": True,
        "currency": currencies.pop() if len(currencies) == 1 else None,
        "total_shares": (
            sum(value for value in shares if value is not None)
            if any(value is not None for value in shares)
            else None
        ),
        "total_amount": _sum_amounts(amounts),
        "total_paid_up": _sum_amounts(paid_up),
    }


def _sum_amounts(values: Sequence[object]) -> str | None:
    """The sum of the stated amounts, as a string, or ``None`` when none is."""
    stated = [value for value in values if value is not None]
    if not stated:
        return None
    return str(sum(Decimal(str(value)) for value in stated))


def _register_company_event_paths(registration: _Registration, company: CompanyInput) -> None:
    """ND2A's ``change_event.company.*`` blocks, sourced from the company itself.

    The form's identification block names the company by BRN and English name;
    the registry writes those paths in the change-event notation, and the *value*
    is the company's own — the master record, never the event (an event carries
    no company name of its own).
    """
    statics_ref = SourceRef(SOURCE_MASTER_RECORD, company.company_id)
    registration.register(
        "change_event.company.br_number", company.statics.get("br_number"), statics_ref
    )
    name_sources = registration.path_sources.get("company.name_en")
    if name_sources:
        registration.register(
            "change_event.company.name_en",
            registration.values.get("company.name_en"),
            name_sources[0],
        )


def _officer_entry_from_input(row: OfficerInput) -> dict[str, Any]:
    """One F1 appointment as a resolved officer entry, with its source."""
    entry = dict(row.person)
    entry.update(
        {
            "role": row.role,
            "capacity": list(row.capacity),
            "alternate_to": row.alternate_to,
            "correspondence_address": _json_value(row.correspondence_address),
            "registered_office_address": _json_value(row.registered_office_address),
            "valid_from": row.valid_from,
            "valid_to": row.valid_to,
            "source": {"type": SOURCE_MASTER_RECORD, "id": str(row.appointment_id)},
        }
    )
    return entry


def _officer_entry_from_payload(event: EventInput, officer: Mapping[str, object]) -> dict[str, Any]:
    """An appointment event's officer as a resolved entry (§3.2).

    The payload's officer object is the form's own vocabulary; every field a
    master record carries that the payload does not state is null — the entry
    says what the event said and nothing more. The source is the event, which is
    what makes the ND2A blocks and any NAR1 highlight trace to the change that
    caused them.
    """
    return {
        "person_id": None,
        "person_type": None,
        "name_zh": officer.get("name_zh"),
        "name_en": officer.get("name_en"),
        "surname_en": officer.get("surname_en"),
        "other_names_en": officer.get("other_names_en"),
        "previous_names": _json_value(officer.get("previous_names") or []),
        "aliases": _json_value(officer.get("aliases") or []),
        "email": officer.get("email"),
        "br_number": officer.get("br_number"),
        "hkid_partial": officer.get("hkid_partial"),
        "passport_partial": officer.get("passport_partial"),
        "passport_issuing_country_region": officer.get("passport_issuing_country_region"),
        "tcsp_licence": _json_value(officer.get("tcsp_licence")),
        "role": officer.get("role"),
        "capacity": _json_value(_as_list(officer.get("capacity"))),
        "alternate_to": officer.get("alternate_to"),
        "correspondence_address": _json_value(officer.get("correspondence_address")),
        "registered_office_address": _json_value(officer.get("registered_office_address")),
        "valid_from": event.effective_date,
        "valid_to": None,
        "source": {"type": SOURCE_CHANGE_EVENT, "id": str(event.event_id)},
    }


def _entry_source(entry: Mapping[str, Any]) -> SourceRef:
    """The source reference an officer entry carries."""
    source = entry.get("source")
    assert isinstance(source, Mapping)  # built above, always this shape
    if source.get("type") == SOURCE_CHANGE_EVENT:
        return SourceRef(SOURCE_CHANGE_EVENT, UUID(str(source["id"])))
    return SourceRef(SOURCE_MASTER_RECORD, UUID(str(source["id"])))


def _entry_in_office(entry: Mapping[str, Any], target_date: date) -> bool:
    """Whether a resolved officer entry holds office as of the target date.

    The half-open service window (``app.domain.officers.in_office``): a record
    closed by a cessation is out from the cessation day, and an entry an earlier
    event added is in from its own effective date.
    """
    valid_from = entry["valid_from"]
    valid_to = entry["valid_to"]
    assert isinstance(valid_from, date)  # entries hold dates until serialization
    assert valid_to is None or isinstance(valid_to, date)
    return in_office(valid_from, valid_to, target_date)


def _resolve_officers(
    registration: _Registration,
    *,
    officers: Sequence[OfficerInput],
    events: Sequence[EventInput],
    form_type: FormCode,
    target_date: date,
    flags: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[EventInput]]:
    """The officer overlay (§3.2): F1's in-office records, then the events.

    The base is every appointment in office as of the target date (the
    application reads them ``as_of``); the events are applied in
    ``effective_date`` order, so a cessation sees the state the appointment
    before it left. Each entry is registered under ``officers[]`` — and thus at
    every path the registry's officer items name — with its own source. An event
    that cannot be applied raises its named flag instead of guessing.
    """
    entries = [_officer_entry_from_input(row) for row in officers]
    applied: list[EventInput] = []
    for event in _ordered_events(events, form_type=form_type, target_date=target_date):
        if event.type not in _APPOINTMENT_TYPES and event.type not in _CESSATION_TYPES:
            continue
        officer = _payload_officer(event)
        if officer is None:
            flags.append(_unapplied(event, "the payload carries no officer object to apply"))
            continue
        if event.type in _CESSATION_TYPES:
            applied_it, detail = _apply_cessation(
                entries, event=event, officer=officer, target_date=target_date
            )
        else:
            applied_it, detail = _apply_appointment(
                entries, event=event, officer=officer, target_date=target_date
            )
        if applied_it:
            applied.append(event)
        else:
            assert detail is not None  # every refusal states one
            flags.append(_unapplied(event, detail))
    entries.sort(key=lambda entry: (str(entry["valid_from"]), str(entry["source"]["id"])))
    for entry in entries:
        registration.register("officers[]", entry, _entry_source(entry))
    return entries, applied


def _apply_cessation(
    entries: list[dict[str, Any]],
    *,
    event: EventInput,
    officer: Mapping[str, object],
    target_date: date,
) -> tuple[bool, str | None]:
    """Close the record a cessation names, or say why it cannot (§3.2).

    The match is the identity rule — the payload's partial HKID when it carries
    one, else the name pair — **plus the role the event's type carries**: a
    ``director_cessation`` closes a directorship or a reserve directorship, a
    ``secretary_cessation`` the secretaryship. Without the role leg the common
    Hong Kong shape — one person who is the company's sole director *and* its
    company secretary — would match both records and leave every cessation
    ambiguous; with it, the office the event names is the office that closes.
    Ambiguity inside one kind (a directorship and a reserve directorship) still
    leaves the event **unapplied** with its named flag rather than a chosen
    office, and the window must stay non-empty: a cessation dated on or before
    the record's start is not a cessation of it.
    """
    implied = _implied_roles(event.type)
    candidates = [
        entry
        for entry in entries
        if _entry_in_office(entry, target_date)
        and entry.get("role") in implied
        and same_officer(record=entry, officer=officer)
    ]
    if not candidates:
        return (False, "no in-office officer record matches the event's identity and role")
    if len(candidates) > 1:
        return (False, "several in-office officer records match the event's identity and role")
    record = candidates[0]
    valid_from = record["valid_from"]
    assert isinstance(valid_from, date)
    if event.effective_date <= valid_from:
        return (False, "the cessation's effective date is not newer than the record's valid_from")
    record["valid_to"] = event.effective_date
    return (True, None)


def _apply_appointment(
    entries: list[dict[str, Any]],
    *,
    event: EventInput,
    officer: Mapping[str, object],
    target_date: date,
) -> tuple[bool, str | None]:
    """Add an appointment's officer, unless the office is already held (§3.2).

    The duplicate test is the same person **in the same office**: the role must
    be one the event's type carries (``TYPE_ROLES`` — the type's own vocabulary,
    the same mapping the grammar validates against) *and* the in-office record
    must hold that same role. The equality is what keeps §2.4's "a person may
    hold several offices" true: appointing a directorship beside a
    secretaryship — or beside a reserve directorship — is a second office, not
    a double entry of one, and the master record's own overlap rule refuses
    only the same role twice.
    """
    role = _text(officer.get("role"))
    if role is None:
        return (False, "the payload names no role to appoint into")
    if role not in _implied_roles(event.type):
        return (False, "the payload names a role this event type does not carry")
    duplicate = any(
        _entry_in_office(entry, target_date)
        and entry.get("role") == role
        and same_officer(record=entry, officer=officer)
        for entry in entries
    )
    if duplicate:
        return (False, "an in-office record of the same person in the same role already exists")
    entries.append(_officer_entry_from_payload(event, officer))
    return (True, None)


def _serialize_entry(entry: Mapping[str, Any]) -> dict[str, Any]:
    """One resolved entry as the snapshot and the JSONB column carry it."""
    return {key: _json_value(value) for key, value in entry.items()}


def _implied_roles(event_type: ChangeEventType) -> frozenset[str]:
    """The role values an event type carries, from the grammar's own mapping.

    Read from ``app.domain.change_events.TYPE_ROLES`` rather than restated, so
    the payload grammar (which refuses a role its type does not carry) and the
    overlay (which matches records by it) cannot disagree about which offices an
    event names.
    """
    return frozenset(role.value for role in TYPE_ROLES[event_type])


def _payload_officer(event: EventInput) -> Mapping[str, object] | None:
    """The officer object of an officer event's payload, or ``None``."""
    key = _EVENT_BODY_KEY.get(event.type)
    if key is None:
        return None
    body = event.payload.get(key)
    if not isinstance(body, Mapping):
        return None
    officer = body.get("officer")
    return officer if isinstance(officer, Mapping) else None


def _ordered_events(
    events: Sequence[EventInput], *, form_type: FormCode, target_date: date
) -> list[EventInput]:
    """The events that apply (§3.1), in effective-date order.

    Ordering is ``(effective_date, event id)``: the day sits first because the
    overlay is a sequence of states, and the id breaks a same-day tie
    deterministically.
    """
    applying = [
        event
        for event in events
        if applies_to_draft(
            form_type=form_type,
            event_type=event.type,
            status=event.status,
            effective_date=event.effective_date,
            target_date=target_date,
        )
    ]
    applying.sort(key=lambda event: (event.effective_date, str(event.event_id)))
    return applying


def _apply_events(
    registration: _Registration,
    *,
    state: dict[str, Any],
    company: CompanyInput,
    form_type: FormCode,
    target_date: date,
    events: Sequence[EventInput],
    applied_officers: Sequence[EventInput],
    flags: list[dict[str, Any]],
) -> list[EventInput]:
    """The registered-office overlay, and the applied events' payload paths.

    The officer events already ran (``_resolve_officers``): this pass takes
    their verdict as the applied set's first part, applies the one non-officer
    overlay NAR1 takes — the registered-office change, when its date is newer
    than the fact it would replace — and registers what each applied event
    contributes to the values map (ND2A's blocks source from
    ``change_event.payload.*``, appendix A). The applied list is returned in
    effective-date order.
    """
    applied_by_id = {str(event.event_id): event for event in applied_officers}
    office = company.facts.get("registered_office")
    office_effective_date = None if office is None else office.effective_date
    for event in _ordered_events(events, form_type=form_type, target_date=target_date):
        if str(event.event_id) in applied_by_id:
            continue
        if event.type is not ChangeEventType.REGISTERED_OFFICE_CHANGE:
            continue
        address = _registered_office_address(event)
        if address is None:
            flags.append(_unapplied(event, "the payload carries no registered office address"))
            continue
        if (
            office_effective_date is not None
            and event.effective_date <= office_effective_date
        ):
            flags.append(
                _unapplied(event, "not newer than the registered-office fact it would replace")
            )
            continue
        ref = SourceRef(SOURCE_CHANGE_EVENT, event.event_id)
        if state["company"]["registered_office"] is None:
            state["company"]["registered_office"] = {}
        for part in ("flat", "building", "street", "district", "country"):
            value = address.get(part)
            registration.override(f"company.registered_office.{part}", value, ref)
            if value is None:
                state["company"]["registered_office"].pop(part, None)
            else:
                state["company"]["registered_office"][part] = value
        applied_by_id[str(event.event_id)] = event
        office_effective_date = event.effective_date
    applied = [
        event
        for event in _ordered_events(events, form_type=form_type, target_date=target_date)
        if str(event.event_id) in applied_by_id
    ]
    for event in applied:
        kind = _EVENT_BODY_KEY.get(event.type)
        if kind is None:
            continue
        body = event.payload.get(kind)
        if isinstance(body, Mapping):
            registration.register(
                f"change_event.payload.{kind}",
                body,
                SourceRef(SOURCE_CHANGE_EVENT, event.event_id),
            )
    return applied


def _registered_office_address(event: EventInput) -> dict[str, Any] | None:
    """The §2.5 address a registered-office change carries."""
    body = event.payload.get("registered_office")
    if not isinstance(body, Mapping):
        return None
    address = body.get("address")
    if not isinstance(address, Mapping):
        return None
    return {str(key): _json_value(value) for key, value in address.items() if value is not None}


def _resolve_shareholdings(
    registration: _Registration, *, shareholdings: Sequence[ShareholdingInput]
) -> list[dict[str, Any]]:
    """The member entries (§3.3) — F1's holdings, with no event overlay.

    v1.0 has no shareholding event type (the change-event vocabulary is officer
    and registered-office changes), so a member position is resolved from the
    master record alone. That is a boundary, not an omission: a share transfer a
    client reports has no event to carry it, and the draft must not invent one.
    """
    entries: list[dict[str, Any]] = []
    for row in sorted(shareholdings, key=lambda item: (item.valid_from, str(item.shareholding_id))):
        entry = dict(row.person)
        entry.update(
            {
                "class_name": row.class_name,
                "shares_held": row.shares_held,
                "jointly_held": row.jointly_held,
                "remarks": row.remarks,
                "percentage_held": row.percentage_held,
                "allotment_date": (
                    None if row.allotment_date is None else row.allotment_date.isoformat()
                ),
                "amount_paid": row.amount_paid,
                "amount_unpaid": row.amount_unpaid,
                "address": _json_value(row.address),
                "valid_from": row.valid_from.isoformat(),
                "valid_to": None if row.valid_to is None else row.valid_to.isoformat(),
                "source": {"type": SOURCE_MASTER_RECORD, "id": str(row.shareholding_id)},
            }
        )
        ref = SourceRef(SOURCE_MASTER_RECORD, row.shareholding_id)
        registration.register("shareholders[]", entry, ref)
        # The registry addresses a member's share facts as `shares[]`; the values
        # are the record's own share columns, registered under that path so the
        # item resolves to the holding it came from.
        registration.register(
            "shareholders[].shares[]",
            {
                "class_name": row.class_name,
                "shares_held": row.shares_held,
                "jointly_held": row.jointly_held,
                "remarks": row.remarks,
                "percentage_held": row.percentage_held,
                "amount_paid": row.amount_paid,
                "amount_unpaid": row.amount_unpaid,
            },
            ref,
        )
        entries.append({key: _json_value(value) for key, value in entry.items()})
    return entries


def _unapplied(event: EventInput, detail: str) -> dict[str, Any]:
    """§3.6's ``unapplied_event`` flag for one event."""
    return {"kind": "unapplied_event", "event_id": str(event.event_id), "detail": detail}


# --- §2.9 step 5's critical fields ------------------------------------------


def _has_value(values: Mapping[str, Any], path: str) -> bool:
    """Whether the resolution placed a non-null, non-blank value at a path."""
    value = values.get(path)
    if value is None:
        return False
    return bool(str(value).strip())


def critical_missing_paths(
    *, form_type: FormCode, values: Mapping[str, Any], made_up_date: date | None
) -> list[str]:
    """§2.9 step 5's critical fields with no value, as the flag paths.

    The set is this module's ``_CRITICAL_PATHS``: the company's name (either
    language), its BRN, its type, the registered office's four required parts,
    and — for NAR1 alone — the return's made-up date. A missing group is flagged
    once, at its first path, rather than once per alternative.
    """
    missing: list[str] = []
    for alternatives in _CRITICAL_PATHS[form_type]:
        present = [
            made_up_date is not None if path == MADE_UP_DATE_PATH else _has_value(values, path)
            for path in alternatives
        ]
        if any(present):
            continue
        missing.append(alternatives[0])
    return missing


# --- §2.9 step 6's prior-filing cross-check ---------------------------------


@dataclass(frozen=True)
class PriorComparison:
    """The cross-check's outcome: which paths moved, and the unexplained ones.

    ``changed_paths`` is per appendix-A path (the registry item's highlight is
    derived from it), and ``flags`` are the ``unexpected_change`` entries — a
    changed path no applied event explains, i.e. drift between what the registry
    was last told and what this draft would say.
    """

    changed_paths: dict[str, bool]
    flags: list[dict[str, Any]]


def prior_filing_comparison(
    *,
    values: Mapping[str, Any],
    path_sources: Mapping[str, Sequence[SourceRef]],
    prior: Mapping[str, str],
) -> PriorComparison:
    """Compare the draft's values against the most recent prior filing (§3.6).

    ``prior`` is the run's read values — the records whose status is not
    ``rejected``, keyed by target: an explicit rejection is staff's decision that
    the filing did not state that value, and a record still awaiting review is
    the filing's own read, which is what the cross-check is about. Only paths
    **both sides name** are compared: the extraction vocabulary is the company
    facts' (officer rows and collections are not proposed), so the comparison is
    naturally about the company block. A difference on a path an applied change
    event explains is recorded as ``changed`` but raises no flag — the event *is*
    the explanation; anything else is the quality signal staff review for.
    """
    changed_paths: dict[str, bool] = {}
    flags: list[dict[str, Any]] = []
    for path, prior_value in prior.items():
        if path not in values:
            continue
        now = values[path]
        before = _canonical(path, prior_value)
        after = _canonical(path, now)
        changed_paths[path] = before != after
        if before == after:
            continue
        source = _source_for_path(path, path_sources)
        if source is None or source.source_type != SOURCE_CHANGE_EVENT:
            flags.append(
                {
                    "kind": "unexpected_change",
                    "target": path,
                    "detail": f"{prior_value} -> {_display(now)}",
                }
            )
    return PriorComparison(changed_paths=changed_paths, flags=flags)


def _canonical(path: str, value: object) -> str | None:
    """A value in its canonical comparable form, by the extraction's own rules.

    ``None``/blank is "no value" — the one thing every path agrees about, and a
    difference against a stated value. A path outside the extraction target
    vocabulary, or a value that does not normalise, compares as its own text:
    the comparison must answer "do these read the same", never raise.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if not is_supported_target(path):
        return text
    try:
        return normalize_value(path, text)
    except InvalidFieldValueError:
        return text


def _display(value: object) -> str:
    """A value as the flag's detail shows it (no value is not an empty string)."""
    text = None if value is None else str(value).strip()
    return text or "(none)"


# --- Provenance -------------------------------------------------------------


def build_field_provenance(
    items: Iterable[tuple[str, str]],
    *,
    path_sources: Mapping[str, Sequence[SourceRef]],
    changed_paths: Mapping[str, bool],
) -> dict[str, dict[str, Any]]:
    """One entry per registry item — ``item → {target, source_type, source_id, changed}``.

    §3.5's mechanical derivation: each part of the item's `` + ``-joined target
    is looked up in the resolution's path sources, the winner being the event
    that explains it, then the document a backfill came from, then the record
    itself; an item no part of which resolves is ``none`` with a null id. The
    item's ``changed`` is true when any of its parts is (the registry item *is*
    the form field — the plan's ruling of 2026-09-28), so a highlight follows the
    box a reviewer sees rather than a sub-string.
    """
    provenance: dict[str, dict[str, Any]] = {}
    for item, target in items:
        parts = individual_paths(target)
        sources = [
            source
            for source in (_source_for_path(part, path_sources) for part in parts)
            if source is not None
        ]
        chosen = next(
            (source for source in sources if source.source_type == SOURCE_CHANGE_EVENT), None
        )
        if chosen is None:
            chosen = next(
                (source for source in sources if source.source_type == SOURCE_BACKFILL), None
            )
        if chosen is None and sources:
            chosen = sources[0]
        provenance[item] = {
            "target": target,
            "source_type": SOURCE_NONE if chosen is None else chosen.source_type,
            "source_id": None if chosen is None else str(chosen.source_id),
            "changed": any(bool(changed_paths.get(part, False)) for part in parts),
        }
    return provenance


# --- The filing-metadata document (§2.8, §3.4) ------------------------------


def filing_metadata_template(*, made_up_date: date | None) -> dict[str, Any]:
    """The empty filing-metadata document, with the return's made-up date filled.

    One home for the shape: generation writes this (with the NAR1 made-up date,
    nothing else), and ``edit`` merges into the version's own document. The
    remaining keys are null until an edit fills them — the return's financial
    period, the delivery channel, the presentor, the signature and the consent
    are all things a human decides, which is why they are not derived from
    master data.
    """
    return {
        "return": {
            "made_up_date": None if made_up_date is None else made_up_date.isoformat(),
            "fs_period_start": None,
            "fs_period_end": None,
        },
        "member_particulars_delivery": None,
        "presentor": None,
        "signature": None,
        "consent": None,
    }


def merge_filing_metadata(
    current: Mapping[str, Any], stated: Mapping[str, Any]
) -> dict[str, Any]:
    """Merge an edit's stated keys into the stored document, key by key.

    ``stated`` carries only what the request said (the API layer drops absent
    keys), so an absent key keeps the stored value and an explicit ``null``
    clears one. The merge runs one level into ``return``, ``presentor``,
    ``signature`` and ``consent`` — the objects whose members are separately
    editable — so a request can correct the financial period's end without
    restating its start.
    """
    merged: dict[str, Any] = {key: value for key, value in current.items()}
    for key, value in stated.items():
        if isinstance(value, Mapping) and isinstance(merged.get(key), Mapping):
            nested = dict(merged[key])
            nested.update(value)
            merged[key] = nested
        else:
            merged[key] = value
    return merged


_METADATA_KEYS = frozenset(
    {"return", "member_particulars_delivery", "presentor", "signature", "consent"}
)
_RETURN_KEYS = ("made_up_date", "fs_period_start", "fs_period_end")
_PRESENTOR_KEYS = ("name", "address", "tel", "fax", "email", "reference")
_SIGNATURE_KEYS = ("name", "date", "capacity")
_CONSENT_KEYS = ("signatory_name", "signatory_capacity")
MAX_METADATA_TEXT_CHARS = 200
MAX_METADATA_ADDRESS_CHARS = 300


def filing_metadata_violations(metadata: Mapping[str, Any]) -> list[str]:
    """Every place the filing-metadata document leaves §2.8's shape.

    Full reason strings, the officers/change-events precedent: the API prints
    them verbatim (``filing_metadata_invalid: <field>``). The rules: no key
    outside §2.8's five (the merge always starts from the stored document, which
    generation wrote the whole of, so none can go missing); ``return``'s three
    dates are ISO dates (or null) with the financial period's start not after
    its end; the delivery channel is §3.3's three-value vocabulary; and the
    presentor, signature and consent objects carry only their own string fields
    — all bounded, because the form's boxes are.
    """
    reasons: list[str] = []
    for key in sorted(set(metadata) - _METADATA_KEYS):
        reasons.append(_invalid(key))
    returned = metadata.get("return")
    if isinstance(returned, Mapping):
        for key in sorted(set(returned) - set(_RETURN_KEYS)):
            reasons.append(_invalid(f"return.{key}"))
        for key in _RETURN_KEYS:
            if key not in returned or returned[key] is None:
                continue
            value = returned[key]
            if not isinstance(value, str) or _iso_date(value) is None:
                reasons.append(_invalid(f"return.{key}: not an ISO date"))
        raw_start, raw_end = returned.get("fs_period_start"), returned.get("fs_period_end")
        start = _iso_date(raw_start) if isinstance(raw_start, str) else None
        end = _iso_date(raw_end) if isinstance(raw_end, str) else None
        if start is not None and end is not None and start > end:
            reasons.append(_invalid("return.fs_period_end: before fs_period_start"))
    elif returned is not None:
        reasons.append(_invalid("return"))
    delivery = metadata.get("member_particulars_delivery")
    if delivery is not None and delivery not in DELIVERY_CHOICES:
        reasons.append(_invalid("member_particulars_delivery"))
    for key, allowed, address in (
        ("presentor", _PRESENTOR_KEYS, True),
        ("signature", _SIGNATURE_KEYS, False),
        ("consent", _CONSENT_KEYS, False),
    ):
        value = metadata.get(key)
        if value is None:
            continue
        if not isinstance(value, Mapping):
            reasons.append(_invalid(key))
            continue
        for field_name in sorted(set(value) - set(allowed)):
            reasons.append(_invalid(f"{key}.{field_name}"))
        for field_name in allowed:
            if field_name not in value:
                continue
            bound = (
                MAX_METADATA_ADDRESS_CHARS
                if address and field_name == "address"
                else MAX_METADATA_TEXT_CHARS
            )
            reasons.extend(
                _text_field_violations(value[field_name], path=f"{key}.{field_name}", bound=bound)
            )
    return reasons


def _invalid(field: str) -> str:
    """One named reason, in the vocabulary contract §5.1.3 prints."""
    return f"filing_metadata_invalid: {field}"


def _iso_date(value: str) -> date | None:
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _text_field_violations(value: object, *, path: str, bound: int) -> list[str]:
    """A stated metadata field: a bounded, non-blank string, or null to clear."""
    if value is None:
        return []
    if not isinstance(value, str):
        return [_invalid(f"{path}: not text")]
    if not value.strip():
        return [_invalid(f"{path}: must not be blank")]
    if len(value) > bound:
        return [_invalid(f"{path}: longer than {bound} characters")]
    return []


__all__ = [
    "ACTION_PERMISSIONS",
    "APPROVAL_STAMP_STATUSES",
    "APPROVED_EVENT_STATUSES",
    "AWAITING_REVIEW_STATUSES",
    "DELIVERY_CHOICES",
    "DRAFT_TRANSITIONS",
    "MADE_UP_DATE_PATH",
    "REVIEW_STAMP_STATUSES",
    "SOURCE_BACKFILL",
    "SOURCE_CHANGE_EVENT",
    "SOURCE_MASTER_RECORD",
    "SOURCE_NONE",
    "STATEMENT_CARRIED_STATUSES",
    "UNWIRED_REASONS",
    "WIRED_ACTIONS",
    "AllowedAction",
    "CompanyInput",
    "DraftAction",
    "DraftLeg",
    "DraftStatus",
    "EventInput",
    "MasterFact",
    "OfficerInput",
    "PriorComparison",
    "Resolution",
    "ShareholdingInput",
    "SignatureStatus",
    "SourceRef",
    "admits",
    "allowed_actions",
    "applies_to_draft",
    "build_field_provenance",
    "critical_missing_paths",
    "ensure_exportable",
    "filing_metadata_template",
    "filing_metadata_violations",
    "is_approved",
    "is_relevant",
    "is_wired",
    "leg_for",
    "merge_filing_metadata",
    "nar1_made_up_date",
    "prior_filing_comparison",
    "rationale_required",
    "resolve_draft",
    "same_officer",
    "unwired_reason",
]
