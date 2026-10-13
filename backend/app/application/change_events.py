"""The change-event lifecycle's service half (scope §2.7-§2.8; data-model §2.6;
contract §4.1, §5.1).

This module is the only path that writes a change event, and the only place
the §4.1 table in ``app.domain.change_events`` is consulted:

- ``create`` is scope §2.7's manual-entry fallback: staff type in a change a
  client reported by phone or in person (or one there was no time to wait for
  a reply on), and the event enters ``pending``, version 1,
  ``source_type=manual``. Ungated by the §5.1 automation gate — the manual
  path is ungated in every tier (ADR-0012) — and grant-gated per company with
  no permission flag, the same rule as a master-data fact append. The event
  it writes is the same gate-checked shape the reply parser will write into
  (M5b), which is why the payload grammar and the linkage rules live here
  rather than in the route.
- ``list_events`` is the company's read: one entry per event — its current
  version, flat, plus the versions before it — newest first, and each event's
  computed 15-day window (§2.6's CE2 note). Every event is
  listed, ``deleted`` included: nothing here is hard-deleted, and this is the
  only read a soft-deleted event appears in (there is no per-event GET in the
  §5.1 row), so hiding one would make it unfindable rather than tidy.
- ``queue`` is the cross-company review read behind DoD §9 item 10's queue:
  every event whose current version awaits staff review, across the companies
  the actor may see, the soonest statutory deadline first, each item carrying
  its company and its computed window. It carries no action list — the
  transitions read is the sole allowed-actions authority.
- ``transitions_for`` is the §5.1 read behind the action buttons: which wired
  actions the actor may perform now, from the table plus the actor's two
  event legs. An unwired action and a system row are never in it.
- ``transition`` is **the one executor** ADR-0008 asks for: it locks the
  event, asks the guard, writes the state (or the new version), and appends
  its audit entry on the same session — so the change and its entry commit or
  roll back together. Nothing else may move an event's status.
- ``list_assessments`` is the §2.7 diagnostic read linked to the event
  (``subject_type=change_event``): truthfully empty until the reply parser
  exists, and deliberately without a writer — no assessment is invented for a
  path that produces none.

**Wired, and refused by name.** ``override`` waits on nothing M5b owes —
``mark_under_review`` covers its need (``reconfirmation_not_wired`` stays its
named refusal); ``supersede`` on the new-event linkage §4.1 describes. Both
are in the table (ADR-0008 wants the spec whole) and both are refused here
with the reason their side effects wait on, before the guard is asked: the
refusal is a property of the build, not of the event's status, and answering
"the status does not admit it" would send a caller to review an event whose
next step does not exist yet. P6-4 retired the third: ``submit`` is wired as
the system row the draft's export performs.

**The reconfirmation exchange (M5b).** ``send_reconfirmation`` is wired as the
review queue's re-ask: a reviewer's row at ``pending`` whose side effect is
the reconfirmer's compose-store-release
(``app.application.reconfirmation.enqueue_event_letter`` — the letter
composes from the event itself, on the thread the event's provenance names),
executed **before** the status move so a letter that cannot compose refuses
with the reconfirmer's own bounded reason and the event never moves
half-performed. ``client_reply`` is wired as §4.1's **system** row: the reply
parser's landing performs it (``client_reply_from_parse``) for every event of
the company standing ``client_reconfirming`` when a reply joins the
reconfirmation's request thread — the event returns to ``pending`` and
reappears in the review queue, with the reply named as the move's source in
the chain. No staff account may press either system row: the transitions
read never offers one, and a staff POST answers the executor's system-row
refusal (``transition_not_allowed``, the ``apply`` precedent).

**The system's own leg.** ``apply`` is wired, as §4.1's system row for draft
generation: ``locked_approved_events`` is generation's read *and* its lock —
every writer of an event locks the event's version-1 row first, so locking the
company's version-1 rows holds them all still for the transaction — and
``apply_to_draft`` moves one of the returned events ``confirmed →
applied_to_draft`` through the same guard, the same write tail and the same
audit entry a staff action uses. A staff POST of ``apply`` still cannot
succeed: the row's leg is ``None`` (the system's), so the executor answers its
system-row refusal, and the transitions read never offers it. P6-4 added the
mirror image: ``submit_applied_events`` is the system row ``export``
performs — each event the exported draft applied moves
``applied_to_draft → submitted`` through the same tail, on the export's own
session, so the filing and its events' filing commit or roll back together.
A staff POST of ``submit`` answers the same system-row refusal, and the
transitions read never offers it either.

**§4.1's invalidation legs.** A transition **out of** ``applied_to_draft`` —
``revert``, ``edit``, ``delete`` or ``client_withdraw`` — invalidates the
drafts the event is linked to, in the same transaction, through the drafts
module's own path (``app.application.drafts.invalidate_for_event``: it finds
the versions whose ``applied_event_ids`` contain the event, sets the reason and
appends the draft's audit entry). The rule it enforces is §4.1's ("invalidates
affected drafts"), not this module's; this module only decides when it applies.
The dependency between the two services runs one way at import time — this
module imports ``app.application.drafts`` — and the drafts module reaches back
for the generation path's reads and the system ``apply`` with a function-level
import, so there is no import cycle.

**The version-row mechanics.** A status change updates the *current* version
in place; an edit appends a new row with the same ``id`` and
``version_no + 1``, carrying the status and the unchanged fields forward (the
audit chain records who revised it and which version the revision became).
Every writer first locks **all** of the event's version rows and then reads
the current one again with a fresh statement — that pair is the whole
concurrency story:

- the lock serializes writers per event (every writer locks version 1, which
  can never be deleted, so two writers cannot be inside at once), which is
  what makes "the highest version is the current one" safe to read and then
  write;
- the second read is not a nicety: the locking statement's snapshot can
  predate a committed revision (a writer that waited on the lock sees the row
  it waited for, not the row inserted while it waited), and acting on that
  stale version would either revise the wrong content or collide on the next
  version number. The fresh statement is the current one, every time.

A collision is still refused rather than raised: the primary key refuses a
second row for one ``(id, version_no)``, and the executor answers
``change_event_conflict`` for it (the ``draft_conflict`` precedent — the
caller re-reads and retries), because a lost race is not a 500.

**Status and stamps tell one story.** Reaching a status clears the stamps it
does not carry: a reverted event cannot keep claiming the review or approval
that was undone, and a rejected or withdrawn one carries neither. The
rationale travels with the write — §4.1 requires it for a rejection and for
the staff-recorded client withdrawal (scope §2.7's phone/in-person case,
where the staff member records what the client said and how they said it),
and an optional rationale on any other action is kept too: it is why the move
was made, and the chain is where a later write cannot erase it.

Reads write no audit entry and no row (the read models' rule).

Services flush but never commit — the API session dependency commits at the
request edge.
"""

from collections.abc import Collection, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import and_, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.persistence.change_events import ChangeEvent
from app.adapters.persistence.communications import Communication
from app.adapters.persistence.confirmation import ConfirmationRequest
from app.adapters.persistence.prior_filing import ExtractionAssessment
from app.application import audit, calendar_sync, company_master, compliance, drafts, reconfirmation
from app.application.documents import DocumentStore
from app.application.scanning import Scanner
from app.domain.audit import AuditAction
from app.domain.change_events import (
    AWAITING_REVIEW_STATUSES,
    CHANGE_EVENT_TRANSITIONS,
    PROTECTED_CLASS_UNAVAILABLE,
    AllowedAction,
    ChangeEventAction,
    ChangeEventRelation,
    ChangeEventSourceType,
    ChangeEventStatus,
    ChangeEventType,
    admits,
    allowed_actions,
    event_violations,
    is_reserved,
    is_wired,
    permission_for,
    protected_class_fields,
    rationale_required,
    unwired_reason,
)
from app.domain.compliance import (
    ComplianceEventKind,
    RiskLevel,
    change_window_countdown,
    risk_band,
)
from app.domain.drafts import APPROVED_EVENT_STATUSES
from app.domain.prior_filing import AssessmentSubject
from app.domain.rbac import ActorContext, Permission, can_access_company, is_allowed

# The statuses that carry a review stamp, and those that carry an approval
# stamp: the review exists while the event stands at `reviewed` or beyond, the
# approval while it stands at `confirmed` or beyond. Everything else carries
# neither, and reaching it clears what it does not carry.
_REVIEW_STAMPED: frozenset[ChangeEventStatus] = frozenset(
    {
        ChangeEventStatus.REVIEWED,
        ChangeEventStatus.CONFIRMED,
        ChangeEventStatus.APPLIED_TO_DRAFT,
        ChangeEventStatus.SUBMITTED,
    }
)
_APPROVAL_STAMPED: frozenset[ChangeEventStatus] = frozenset(
    {
        ChangeEventStatus.CONFIRMED,
        ChangeEventStatus.APPLIED_TO_DRAFT,
        ChangeEventStatus.SUBMITTED,
    }
)


class ChangeEventNotFoundError(Exception):
    """No change event with that id in the actor's tenant (cross-tenant → unknown)."""


class ChangeEventForbiddenError(Exception):
    """The actor has no per-company access grant for the event's company."""


class ChangeEventReservedTypeError(Exception):
    """The type is reserved for a later wave (§2.6): ``company_name_change``."""

    def __init__(self, event_type: ChangeEventType) -> None:
        self.event_type = event_type
        super().__init__(f"{event_type.value} is reserved for Wave 2/NNC2")


class ChangeEventInvalidError(Exception):
    """The entry or edit leaves the domain's rules; ``errors`` names every one.

    The list is the domain's own violation strings — field paths and rules,
    never client data — and the API puts it on the wire as
    ``change_event_invalid: <violations>`` (contract §5.1.3), the way the
    tenant-configuration block's violations travel.
    """

    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        super().__init__("; ".join(errors))


class ChangeEventRationaleRequiredError(Exception):
    """The action is one §4.1 marks "rationale required", and none arrived."""


class ChangeEventTransitionError(Exception):
    """The event's current status does not admit the action; the guard refused.

    Carries both operands so the API can name the refusal without re-reading
    the table (``app.domain.change_events`` owns the legality; this is only
    the report of it).
    """

    def __init__(self, *, status: ChangeEventStatus, action: ChangeEventAction) -> None:
        self.status = status
        self.action = action
        super().__init__(f"a {status.value} change event does not admit {action.value}")


class ChangeEventActionNotWiredError(Exception):
    """The action is in §4.1's table and not in this build's executor.

    ``reason`` is the §5.1.3 name the API answers with (today
    ``reconfirmation_not_wired`` / ``supersede_not_wired`` — ``draft_not_wired``
    retired with P6-4, which wired ``submit`` as the system row the draft's
    export performs) — never a silent acceptance and never a half-performed
    transition.
    """

    def __init__(self, *, action: ChangeEventAction, reason: str) -> None:
        self.action = action
        self.reason = reason
        super().__init__(f"{action.value} is not wired: {reason}")


class ChangeEventConflictError(Exception):
    """Another revision of this event committed while this one was deciding.

    The primary key refused the version insert: the caller lost a race it
    cannot see, re-reads the event and retries (the ``draft_conflict``
    precedent, contract §5.1.3).
    """


class ChangeEventProtectedClassError(Exception):
    """The payload carries a §1 protected-class field, which this build refuses.

    ``protected_class_unavailable`` (contract §5.1.3): the class is stored
    encrypted with per-tenant keys and no field-level encryption exists yet
    (§8 scope, Phase 7), so the four fields are refused rather than becoming
    the first plaintext of the class in the schema (the ruling of 2026-09-25).
    ``fields`` names every one that appeared, as payload paths, and ``detail``
    is the wire string — composed here so the entry path and the edit path
    cannot describe one refusal in two vocabularies (the renewal gate's own
    pattern). The partial identifiers are unaffected: they are what the public
    ND2A pages carry.
    """

    def __init__(self, fields: list[str]) -> None:
        self.fields = fields
        self.detail = f"{PROTECTED_CLASS_UNAVAILABLE}: {', '.join(fields)}"
        super().__init__(self.detail)


@dataclass(frozen=True)
class ChangeWindowView:
    """One event's 15-day window as the reads carry it.

    The domain's ``ChangeWindow`` (``change_window_countdown``: the statutory
    deadline, the days left and whether the deadline has passed) plus the
    ``RiskBand`` the same countdown produces under the calendar's own 30/15/7
    bands — so "approaching" (amber inside 15 days, red inside 7) and "past"
    (``is_late``, red) are the product's existing colour vocabulary rather
    than a second threshold invented for this read. ``risk`` and ``alert`` are
    ``risk_band``'s values, computed from ``days_remaining``.
    """

    deadline: date
    days_remaining: int
    is_late: bool
    risk: RiskLevel
    alert: ComplianceEventKind | None


@dataclass(frozen=True)
class ChangeEventView:
    """One event as the reads return it: its current version, and its past.

    ``history`` holds the versions *before* the current one, newest first, so
    every fact in the response has one home: the current version is the flat
    fields, and the lineage is the list. The brief's alternative — a version
    count plus a versions endpoint — was rejected because the §5.1 row has no
    such route, and a count without the rows would be a read that answers
    "how many" and never "what".

    ``window`` is the current version's 15-day countdown, computed as of the
    read's day (``as_of``) from ``effective_date`` — the day the statutory
    clock starts, never ``informed_date``. It belongs to the event rather than
    to a version row, which is why the ``history`` entries carry none.
    """

    current: ChangeEvent
    history: list[ChangeEvent]
    window: ChangeWindowView

    @property
    def id(self) -> UUID:
        return self.current.id


def _window_for(effective_date: date, *, as_of: date) -> ChangeWindowView:
    """The composed window the reads carry: the domain countdown plus its band."""
    window = change_window_countdown(effective_date, as_of=as_of)
    band = risk_band(window.days_remaining)
    return ChangeWindowView(
        deadline=window.deadline,
        days_remaining=window.days_remaining,
        is_late=window.is_late,
        risk=band.level,
        alert=band.alert,
    )


@dataclass(frozen=True)
class ChangeEventTransitions:
    """The §5.1 transitions read: the event as it stands, and the actor's actions."""

    row: ChangeEvent
    actions: list[AllowedAction]


@dataclass(frozen=True)
class ChangeEventQueueItem:
    """One awaiting-review event as the cross-company queue returns it.

    The current version (the queue lists events, not lineages — the company
    read carries the version history), the company that owns it, and the
    computed window. It carries no action list on purpose:
    ``GET /change-events/{id}/transitions`` is the sole allowed-actions
    authority.
    """

    current: ChangeEvent
    br_number: str
    company_name: str | None
    window: ChangeWindowView


async def _locked_event(
    session: AsyncSession, *, tenant_id: UUID, event_id: UUID
) -> ChangeEvent:
    """The event's current version, with every writer serialized behind us.

    Two statements, and the module docstring says why: the lock (over the
    event's version rows, in version order, so two writers cannot deadlock
    over them) is the mutex, and the plain re-read in between is what makes
    the row we act on the *current* one when a revision committed while we
    waited.

    ``ChangeEventNotFoundError`` for an unknown or another tenant's id — one
    answer for both, like every other tenant-scoped read.
    """
    locked = list(
        (
            await session.scalars(
                select(ChangeEvent)
                .where(ChangeEvent.id == event_id, ChangeEvent.tenant_id == tenant_id)
                .order_by(ChangeEvent.version_no)
                .with_for_update()
            )
        ).all()
    )
    if not locked:
        raise ChangeEventNotFoundError
    current = await _current_version(session, tenant_id=tenant_id, event_id=event_id)
    assert current is not None  # the lock read saw the rows, and nothing deletes them
    return current


async def _current_version(
    session: AsyncSession, *, tenant_id: UUID, event_id: UUID
) -> ChangeEvent | None:
    """The event's current version — the highest ``version_no`` — or ``None``."""
    current: ChangeEvent | None = await session.scalar(
        select(ChangeEvent)
        .where(ChangeEvent.id == event_id, ChangeEvent.tenant_id == tenant_id)
        .order_by(ChangeEvent.version_no.desc())
        .limit(1)
    )
    return current


async def locked_approved_events(
    session: AsyncSession, *, tenant_id: UUID, company_id: UUID
) -> list[ChangeEvent]:
    """The company's approved events' current versions, locked for a generation.

    Draft generation's read **and** its lock, in the module's two-statement
    pattern: the lock is taken over the company's version-1 rows (the rows every
    writer locks first, and the ones that can never be deleted), so while this
    transaction holds them no other writer can move or revise any of the
    company's events — and the second statement is a *fresh* read, so a
    revision or status change a committed writer made while this transaction
    waited is what the returned rows carry.

    Only the statuses §3.1 lets a draft apply are returned (``confirmed`` and
    ``applied_to_draft``), ordered by ``(effective_date, id)``: the order the
    resolution applies them in, and a stable order for the locks. A read that
    writes nothing: no audit entry, no row.
    """
    locked_ids = list(
        (
            await session.scalars(
                select(ChangeEvent.id)
                .where(
                    ChangeEvent.tenant_id == tenant_id,
                    ChangeEvent.company_id == company_id,
                    ChangeEvent.version_no == 1,
                )
                .order_by(ChangeEvent.id)
                .with_for_update()
            )
        ).all()
    )
    if not locked_ids:
        return []
    latest = (
        select(
            ChangeEvent.id.label("id"),
            func.max(ChangeEvent.version_no).label("version_no"),
        )
        .where(ChangeEvent.tenant_id == tenant_id, ChangeEvent.id.in_(locked_ids))
        .group_by(ChangeEvent.id)
        .subquery()
    )
    rows = await session.scalars(
        select(ChangeEvent)
        .join(
            latest,
            and_(
                ChangeEvent.id == latest.c.id,
                ChangeEvent.version_no == latest.c.version_no,
            ),
        )
        .where(
            ChangeEvent.status.in_(
                sorted(status.value for status in APPROVED_EVENT_STATUSES)
            )
        )
        .order_by(ChangeEvent.effective_date, ChangeEvent.id)
    )
    return list(rows.all())


def _require_company_access(context: ActorContext, company_id: UUID) -> None:
    """The grant-only gate for the id-scoped routes (no ``company_id`` in path).

    The same single decision ``app.api.deps.require_company_access`` applies to
    company-scoped routes; these routes learn their company from the row, so
    the check happens here instead — one mechanism, one place it is consulted.
    """
    if not can_access_company(context, company_id):
        raise ChangeEventForbiddenError


async def _relation_violations(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    company_id: UUID,
    related_event_id: UUID | None,
    relation: ChangeEventRelation | None,
) -> list[str]:
    """The linkage rules §2.6 states, checked against the target event.

    A reference and a relation travel together (the database says the same),
    the target must be an event of *this* company — an unknown id, another
    company's event and another tenant's are one answer, deliberately — and a
    ``submitted`` event **can only be superseded**: §2.6's rule, and the one
    that keeps a filed event out of reach of a correction.
    """
    if related_event_id is None:
        if relation is not None:
            return ["relation: requires related_event_id"]
        return []
    if relation is None:
        return ["related_event_id: requires a relation"]
    target_status = await session.scalar(
        select(ChangeEvent.status)
        .where(
            ChangeEvent.id == related_event_id,
            ChangeEvent.tenant_id == tenant_id,
            ChangeEvent.company_id == company_id,
        )
        .order_by(ChangeEvent.version_no.desc())
        .limit(1)
    )
    if target_status is None:
        return ["related_event_id: no such change event for this company"]
    if (
        target_status == ChangeEventStatus.SUBMITTED.value
        and relation is not ChangeEventRelation.SUPERSEDES
    ):
        return [
            "related_event_id: a submitted event can only be superseded "
            "(relation: supersedes)"
        ]
    return []


def _entry_audit_details(row: ChangeEvent) -> dict[str, object]:
    """The entry's audit details: what it is, not what it says.

    The payload's home is the version row; the chain carries the coordinates a
    reader needs to find it (ADR-0009: the chain records changes, and the
    payload is one).
    """
    return {
        "type": row.type,
        "source_type": row.source_type,
        "version_no": row.version_no,
        "before": None,
        "after": row.status,
    }


async def create(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    company_id: UUID,
    event_type: ChangeEventType,
    effective_date: date,
    payload: object,
    as_of: date,
    effective_date_text: str | None = None,
    informed_date: date | None = None,
    related_event_id: UUID | None = None,
    relation: ChangeEventRelation | None = None,
    source_type: ChangeEventSourceType = ChangeEventSourceType.MANUAL,
    source_id: UUID | None = None,
    actor_user_id: UUID | None = None,
) -> ChangeEventView:
    """Enter one change event — scope §2.7's manual fallback, and the shape
    the reply parser's proposals land in (M5b).

    ``source_type=manual`` and no ``source_id``: there is no communication or
    document behind staff's own words, and inventing a reference to one would
    make the provenance chain lie. The parser passes
    ``source_type=communication`` with the reply's id — its proposals enter
    the same queue with a provenance someone can point at. The event starts
    ``pending`` at version 1 either way.

    ``CompanyNotFoundError`` for an unknown or cross-tenant company (the API
    answers 404), ``ChangeEventReservedTypeError`` for
    ``company_name_change`` (§2.6 reserves it), and ``ChangeEventInvalidError``
    naming every violation of the payload grammar, the date sanity rule and
    the linkage rules. ``as_of`` is the read's day: the new event's window
    countdown the response carries is counted from it.
    """
    company = await company_master.get_company(session, tenant_id=tenant_id, company_id=company_id)
    if is_reserved(event_type):
        raise ChangeEventReservedTypeError(event_type)
    protected = protected_class_fields(payload)
    if protected:
        raise ChangeEventProtectedClassError(protected)
    violations = event_violations(
        event_type=event_type,
        effective_date=effective_date,
        informed_date=informed_date,
        payload=payload,
    )
    violations.extend(
        await _relation_violations(
            session,
            tenant_id=tenant_id,
            company_id=company.id,
            related_event_id=related_event_id,
            relation=relation,
        )
    )
    if violations:
        raise ChangeEventInvalidError(violations)
    row = ChangeEvent(
        tenant_id=tenant_id,
        company_id=company.id,
        version_no=1,
        type=event_type.value,
        status=ChangeEventStatus.PENDING.value,
        effective_date=effective_date,
        effective_date_text=effective_date_text,
        informed_date=informed_date,
        payload=dict(payload) if isinstance(payload, Mapping) else {},
        source_type=source_type.value,
        source_id=source_id,
        related_event_id=related_event_id,
        relation=None if relation is None else relation.value,
        created_by=actor_user_id,
    )
    session.add(row)
    await session.flush()
    await audit.record_event(
        session,
        tenant_id=tenant_id,
        action=AuditAction.CHANGE_EVENT_RECORDED,
        actor_user_id=actor_user_id,
        company_id=company.id,
        entity_type="change_event",
        entity_id=row.id,
        details=_entry_audit_details(row),
    )
    await session.flush()
    return ChangeEventView(
        current=row, history=[], window=_window_for(row.effective_date, as_of=as_of)
    )


async def list_events(
    session: AsyncSession, *, tenant_id: UUID, company_id: UUID, as_of: date
) -> list[ChangeEventView]:
    """The company's change events, one entry per event, newest first.

    Every version row of the company in one read, grouped by event id: the
    current version is the flat fields and the earlier ones are ``history``,
    each event with its 15-day window counted as of ``as_of``.
    ``CompanyNotFoundError`` for an unknown or cross-tenant company, like
    every other company-scoped read.
    """
    company = await company_master.get_company(session, tenant_id=tenant_id, company_id=company_id)
    rows = list(
        (
            await session.scalars(
                select(ChangeEvent)
                .where(ChangeEvent.tenant_id == tenant_id, ChangeEvent.company_id == company.id)
                .order_by(ChangeEvent.id, ChangeEvent.version_no)
            )
        ).all()
    )
    views = _group(rows, as_of=as_of)
    views.sort(key=lambda view: (view.current.created_at, view.current.id), reverse=True)
    return views


def _group(rows: list[ChangeEvent], *, as_of: date) -> list[ChangeEventView]:
    """Group version rows into events, newest version first in each history."""
    by_id: dict[UUID, list[ChangeEvent]] = {}
    for row in rows:
        by_id.setdefault(row.id, []).append(row)
    views: list[ChangeEventView] = []
    for versions in by_id.values():
        versions.sort(key=lambda row: row.version_no, reverse=True)
        views.append(
            ChangeEventView(
                current=versions[0],
                history=versions[1:],
                window=_window_for(versions[0].effective_date, as_of=as_of),
            )
        )
    return views


async def queue(
    session: AsyncSession, *, tenant_id: UUID, context: ActorContext, as_of: date
) -> list[ChangeEventQueueItem]:
    """The cross-company review queue (DoD §9 item 10): what awaits staff.

    Every event whose **current version** stands at an awaiting-review status
    (``AWAITING_REVIEW_STATUSES``: ``pending`` and ``under_review`` — the
    constant's own note states why ``client_reconfirming`` and ``reviewed``
    are out) across the companies the actor may see, the soonest statutory
    deadline first — the queue is ordered by the clock the product is judged
    by. Scoping is the dashboard's one rule
    (``app.application.compliance.company_scope``): an Administrator sees the
    tenant, a General account exactly its access grants, an inactive account
    nothing.

    The current version is selected in SQL — the highest ``version_no`` per
    event — not by filtering rows: a revision leaves the earlier row's status
    where it stood, so a row-level filter would list a decided event off its
    own history (the DB scenarios pin that trap). Each item carries the
    event's computed window and its company, and no action list: the
    transitions read is the sole allowed-actions authority.

    A read: no writes, no audit entries.
    """
    scope = compliance.company_scope(context)
    latest = (
        select(
            ChangeEvent.id.label("id"),
            func.max(ChangeEvent.version_no).label("version_no"),
        )
        .where(ChangeEvent.tenant_id == tenant_id)
        .group_by(ChangeEvent.id)
        .subquery()
    )
    query = (
        select(ChangeEvent)
        .join(
            latest,
            and_(
                ChangeEvent.id == latest.c.id,
                ChangeEvent.version_no == latest.c.version_no,
            ),
        )
        .where(
            ChangeEvent.tenant_id == tenant_id,
            ChangeEvent.status.in_(
                sorted(status.value for status in AWAITING_REVIEW_STATUSES)
            ),
        )
        .order_by(ChangeEvent.effective_date, ChangeEvent.created_at, ChangeEvent.id)
    )
    if scope is not None:
        query = query.where(ChangeEvent.company_id.in_(scope))
    rows = list((await session.scalars(query)).all())
    if not rows:
        return []
    companies = {
        company.id: company
        for company in await compliance.visible_companies(
            session, tenant_id=tenant_id, scope=scope
        )
    }
    names = await compliance.display_names(
        session, sorted({row.company_id for row in rows}), as_of=as_of
    )
    return [
        ChangeEventQueueItem(
            current=row,
            br_number=companies[row.company_id].br_number,
            company_name=names.get(row.company_id),
            window=_window_for(row.effective_date, as_of=as_of),
        )
        for row in rows
    ]


async def _event_view(
    session: AsyncSession, *, tenant_id: UUID, event_id: UUID, as_of: date
) -> ChangeEventView:
    """One event's full lineage, as a read after the write returns it."""
    rows = list(
        (
            await session.scalars(
                select(ChangeEvent)
                .where(ChangeEvent.id == event_id, ChangeEvent.tenant_id == tenant_id)
                .order_by(ChangeEvent.version_no)
            )
        ).all()
    )
    if not rows:
        raise ChangeEventNotFoundError
    return _group(rows, as_of=as_of)[0]


async def transitions_for(
    session: AsyncSession, *, tenant_id: UUID, context: ActorContext, event_id: UUID
) -> ChangeEventTransitions:
    """The actor's allowed actions on one event (§5.1).

    Read-only: the event's current version, then the wired, leg-admitted rows
    from the §4.1 table. ``Permission.REVIEW_EVENTS`` and
    ``Permission.APPROVE_EVENTS`` are resolved **for the event's company**
    through the single RBAC decision, so a General account's grants and an
    Administrator's standing are one rule; an actor without the company's
    access grant is refused before any action is computed
    (``ChangeEventForbiddenError``).
    """
    row = await _current_version(session, tenant_id=tenant_id, event_id=event_id)
    if row is None:
        raise ChangeEventNotFoundError
    _require_company_access(context, row.company_id)
    actions = allowed_actions(
        status=ChangeEventStatus(row.status),
        may_review=is_allowed(context, Permission.REVIEW_EVENTS, row.company_id),
        may_approve=is_allowed(context, Permission.APPROVE_EVENTS, row.company_id),
    )
    return ChangeEventTransitions(row=row, actions=actions)


async def list_assessments(
    session: AsyncSession, *, tenant_id: UUID, context: ActorContext, event_id: UUID
) -> list[ExtractionAssessment]:
    """The §2.7 assessments linked to this event, newest first.

    Truthfully empty for parser-proposed events: the parser exists (M5b), but
    its diagnostic row hangs off the reply's ``Communication``
    (``subject_type=communication``, §2.7), while this read filters the
    contract's ``subject_type=change_event`` — the parse behind a proposed
    event surfaces through the communications parse read, and an event
    entered by hand has no parse behind it at all. This read does not invent
    a row.
    """
    row = await _current_version(session, tenant_id=tenant_id, event_id=event_id)
    if row is None:
        raise ChangeEventNotFoundError
    _require_company_access(context, row.company_id)
    return list(
        (
            await session.scalars(
                select(ExtractionAssessment)
                .where(
                    ExtractionAssessment.tenant_id == tenant_id,
                    ExtractionAssessment.subject_type == AssessmentSubject.CHANGE_EVENT.value,
                    ExtractionAssessment.subject_id == event_id,
                )
                .order_by(ExtractionAssessment.created_at.desc(), ExtractionAssessment.id)
            )
        ).all()
    )


def _edit_changes(
    row: ChangeEvent, changes: Mapping[str, Any]
) -> tuple[dict[str, Any], list[str]]:
    """The fields an edit changes, checked, or the violations that refuse it.

    ``changes`` carries only the fields the request actually stated, so an
    explicit ``null`` clears ``effective_date_text``/``informed_date`` and an
    absent field keeps what the version has (the partial-update rule the
    person routes state). ``type``, ``effective_date`` and ``payload`` are the
    substance of the event and cannot be cleared: an event with no type is not
    an event.

    The §1 protected class is refused **before** the rest of the grammar runs,
    and for the *effective* payload — what the version would carry after the
    edit — so a revision cannot introduce the class any more than an entry can,
    and the caller is answered ``protected_class_unavailable`` naming the field
    rather than a generic violation list.
    """
    if not changes:
        return {}, ["edit: nothing to change"]
    resolved: dict[str, Any] = {}
    violations: list[str] = []
    for field, value in changes.items():
        if value is None and field not in ("effective_date_text", "informed_date"):
            violations.append(f"{field}: cannot be cleared")
            continue
        resolved[field] = value
    if violations:
        return {}, violations
    event_type = ChangeEventType(resolved.get("type", row.type))
    effective_date = resolved.get("effective_date", row.effective_date)
    informed_date = resolved.get("informed_date", row.informed_date)
    payload = resolved.get("payload", row.payload)
    protected = protected_class_fields(payload)
    if protected:
        raise ChangeEventProtectedClassError(protected)
    violations.extend(
        event_violations(
            event_type=event_type,
            effective_date=effective_date,
            informed_date=informed_date,
            payload=payload,
        )
    )
    return resolved, violations


async def transition(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    context: ActorContext,
    event_id: UUID,
    action: ChangeEventAction,
    as_of: date,
    actor_user_id: UUID | None = None,
    rationale: str | None = None,
    changes: Mapping[str, Any] | None = None,
    store: DocumentStore | None = None,
    scanner: Scanner | None = None,
) -> ChangeEventView:
    """**The one executor** (ADR-0008): lock → guard → write → audit.

    The refusals, in the order a caller meets them, and why that order:

    1. unknown or another tenant's id → ``ChangeEventNotFoundError``;
    2. no per-company access grant → ``ChangeEventForbiddenError`` (deny by
       default, before anything about the event is used);
    3. an unwired action → ``ChangeEventActionNotWiredError`` with the reason
       its side effects wait on. A property of the build, so it is answered
       before the guard: "the status does not admit it" would send a caller to
       review an event whose next step does not exist yet;
    4. an action the current status does not admit →
       ``ChangeEventTransitionError`` (never a no-op);
    5. a missing leg — the §4.1 row's ``review_events``/``approve_events``,
       resolved for the event's company → ``ChangeEventForbiddenError``;
    6. a required rationale that did not arrive →
       ``ChangeEventRationaleRequiredError`` (422 ``rationale_required``);
    7. a malformed edit (nothing to change, a field cleared that cannot be, a
       payload the grammar refuses) → ``ChangeEventInvalidError``;
    8. a lost race on the version insert → ``ChangeEventConflictError``.

    Steps 3 and 4 are answered for any actor who has the company's grant —
    they can already read the event and its status, so the refusals leak
    nothing; the actor's *own* authority is settled at 5, deliberately after
    the two the whole build shares. An edit's validation runs after the leg
    because an actor without the leg should not learn what a valid payload
    would have been.

    ``send_reconfirmation`` resolves the request thread from the event's
    provenance and runs the reconfirmer's compose-store-release **before**
    the move: a letter that cannot compose raises
    ``reconfirmation.ReconfirmationError`` and the event stays where it stood
    — never half-performed. The caller passes the ``store`` and ``scanner``
    seams (the route takes them from its dependencies, the way the
    confirmation trigger does); every other action ignores them.

    ``as_of`` is the read's day: the returned view's window countdown is
    counted from it, exactly as the read that follows would.
    """
    row = await _locked_event(session, tenant_id=tenant_id, event_id=event_id)
    _require_company_access(context, row.company_id)
    if not is_wired(action):
        reason = unwired_reason(action)
        assert reason is not None  # every unwired action has one (domain test)
        raise ChangeEventActionNotWiredError(action=action, reason=reason)
    status = ChangeEventStatus(row.status)
    leg = permission_for(status, action)
    # A row the status does not admit, and a system row (no staff leg at all),
    # are the same answer here: this actor cannot perform it from here.
    if not admits(status, action) or leg is None:
        raise ChangeEventTransitionError(status=status, action=action)
    if not is_allowed(context, leg, row.company_id):
        raise ChangeEventForbiddenError
    if rationale_required(action) and not (rationale or "").strip():
        raise ChangeEventRationaleRequiredError
    if action is ChangeEventAction.EDIT:
        # An edit with no fields stated is a revision that would say exactly
        # what the last version says: refused, not appended.
        resolved, violations = _edit_changes(row, changes or {})
        if violations:
            raise ChangeEventInvalidError(violations)
        view = await _revise(
            session,
            row=row,
            changes=resolved,
            actor_user_id=actor_user_id,
            rationale=rationale,
            as_of=as_of,
        )
    elif changes:
        # The edit fields are the body's shape for `edit` alone: on any other
        # action they are a request the caller misunderstood, not keys to
        # ignore (which is how a "confirm" would silently rewrite the payload).
        raise ChangeEventInvalidError([f"{action.value}: carries edit fields"])
    elif action is ChangeEventAction.SEND_RECONFIRMATION:
        view = await _send_reconfirmation(
            session,
            row=row,
            store=store,
            scanner=scanner,
            actor_user_id=actor_user_id,
            as_of=as_of,
        )
    else:
        view = await _move(
            session,
            row=row,
            action=action,
            actor_user_id=actor_user_id,
            rationale=rationale,
            as_of=as_of,
        )
    if status is ChangeEventStatus.APPLIED_TO_DRAFT and action in _INVALIDATING_ACTIONS:
        # §4.1: leaving `applied_to_draft` by revert/edit/delete/client_withdraw
        # invalidates the drafts this event is linked to. The status was read
        # before the write (the row's is already the target's), and the
        # invalidation runs on the same session: the move and what it invalidated
        # commit or roll back together.
        await _invalidate_linked_drafts(
            session, row=row, action=action, actor_user_id=actor_user_id
        )
    return view


# §4.1's "invalidates affected drafts" rows: the four transitions out of
# `applied_to_draft` that are not the filing itself. `submit` is not among them
# — it is the one move that *keeps* the drafts (the event has been filed).
_INVALIDATING_ACTIONS: frozenset[ChangeEventAction] = frozenset(
    {
        ChangeEventAction.REVERT,
        ChangeEventAction.EDIT,
        ChangeEventAction.DELETE,
        ChangeEventAction.CLIENT_WITHDRAW,
    }
)


async def _invalidate_linked_drafts(
    session: AsyncSession,
    *,
    row: ChangeEvent,
    action: ChangeEventAction,
    actor_user_id: UUID | None,
) -> None:
    """Give §4.1's "invalidates affected drafts" its side effect.

    What the invalidation *is* — which drafts the event is linked to, what the
    reason says, the audit entry — is the drafts module's own path
    (``invalidate_for_event``); this call site only decides when §4.1 requires
    it.
    """
    await drafts.invalidate_for_event(
        session,
        tenant_id=row.tenant_id,
        event_id=row.id,
        reason=action.value,
        actor_user_id=actor_user_id,
    )


async def _reconfirmation_request(
    session: AsyncSession, *, tenant_id: UUID, row: ChangeEvent
) -> ConfirmationRequest:
    """The request thread a staff-pressed re-ask answers on — from the event's
    own provenance, never a guess.

    The chain is the event's source reply: ``source_type=communication`` with
    the reply's id, the reply carrying the request its thread belongs to (the
    §5.1.12 deterministic join the ingestion cycle wrote). Anything else — a
    hand-entered event, a reply that joined no request — refuses with the
    reconfirmer's own ``no_confirmation_request``: the letter needs the
    request's recipients and thread anchor, and the outbox's request reference
    is NOT NULL, so there is no letter to compose and the event must not move.
    """
    if row.source_type != ChangeEventSourceType.COMMUNICATION.value or row.source_id is None:
        raise reconfirmation.ReconfirmationError("no_confirmation_request")
    communication = await session.scalar(
        select(Communication).where(
            Communication.id == row.source_id, Communication.tenant_id == tenant_id
        )
    )
    if communication is None or communication.confirmation_request_id is None:
        raise reconfirmation.ReconfirmationError("no_confirmation_request")
    request = await session.scalar(
        select(ConfirmationRequest).where(
            ConfirmationRequest.id == communication.confirmation_request_id,
            ConfirmationRequest.tenant_id == tenant_id,
        )
    )
    if request is None:
        raise reconfirmation.ReconfirmationError("no_confirmation_request")
    return request


async def _send_reconfirmation(
    session: AsyncSession,
    *,
    row: ChangeEvent,
    store: DocumentStore | None,
    scanner: Scanner | None,
    actor_user_id: UUID | None,
    as_of: date,
) -> ChangeEventView:
    """The review queue's re-ask: compose-store-release the restatement, then
    move the event to ``client_reconfirming``.

    The letter rides the reconfirmer's event-driven entry
    (``app.application.reconfirmation.enqueue_event_letter``) and runs
    **before** the move: a letter that cannot compose raises
    ``ReconfirmationError`` and nothing is written or moved — never
    half-performed. The move itself is the same tail every status move uses,
    so the chain records the re-ask exactly the way it records any other
    transition, one entry through the single write path.
    """
    if store is None or scanner is None:
        raise RuntimeError(
            "send_reconfirmation requires the store and scanner seams — "
            "the API route passes them, as it does for every letter-writing path"
        )
    request = await _reconfirmation_request(session, tenant_id=row.tenant_id, row=row)
    await reconfirmation.enqueue_event_letter(
        session,
        store,
        scanner,
        tenant_id=row.tenant_id,
        event=row,
        request=request,
        actor_user_id=actor_user_id,
    )
    await _move_row(
        session,
        row=row,
        action=ChangeEventAction.SEND_RECONFIRMATION,
        actor_user_id=actor_user_id,
        rationale=None,
    )
    return await _event_view(session, tenant_id=row.tenant_id, event_id=row.id, as_of=as_of)


def _transition_audit_details(
    *, action: ChangeEventAction, before: str, after: str, version_no: int, rationale: str | None
) -> dict[str, object]:
    """One transition's audit details — the coordinates, never the content.

    The payload is the version row's; what the chain carries is what a reader
    auditing the workflow needs: which action, from which status to which,
    which version resulted, and the reason when one was given.
    """
    details: dict[str, object] = {
        "action": action.value,
        "before": before,
        "after": after,
        "version_no": version_no,
    }
    if rationale is not None:
        details["rationale"] = rationale
    return details


async def _move(
    session: AsyncSession,
    *,
    row: ChangeEvent,
    action: ChangeEventAction,
    actor_user_id: UUID | None,
    rationale: str | None,
    as_of: date,
) -> ChangeEventView:
    """Apply a transition that moves the current version's status in place.

    The write tail is ``_move_row`` — the same one the system's ``apply``
    uses — and the returned view is the event's lineage as the read that
    follows would carry it.
    """
    await _move_row(
        session, row=row, action=action, actor_user_id=actor_user_id, rationale=rationale
    )
    return await _event_view(session, tenant_id=row.tenant_id, event_id=row.id, as_of=as_of)


async def _move_row(
    session: AsyncSession,
    *,
    row: ChangeEvent,
    action: ChangeEventAction,
    actor_user_id: UUID | None,
    rationale: str | None,
    extra_details: Mapping[str, Any] | None = None,
) -> None:
    """Write one status move in place, and append its audit entry.

    The stamp the target status carries is written here and the ones it does not
    carry are cleared — reaching ``reviewed`` records who reviewed it, reverting
    out of ``confirmed`` takes the approval back with it, and a rejection or
    withdrawal carries neither. The rationale replaces the row's when one was
    given, and the entry carries the action, both statuses and the version, so
    the system's ``apply`` and a staff action leave the chain looking the same.
    ``extra_details`` lets a system caller add coordinates of its own (the
    reply parser names the reply that sourced a client withdrawal) without a
    second entry.
    """
    status = ChangeEventStatus(row.status)
    target = CHANGE_EVENT_TRANSITIONS[status][action]
    before = row.status
    now = datetime.now(UTC)
    row.status = target.value
    if target in _REVIEW_STAMPED:
        if action is ChangeEventAction.MARK_REVIEWED:
            row.reviewed_by = actor_user_id
            row.reviewed_at = now
    else:
        row.reviewed_by = None
        row.reviewed_at = None
    if target in _APPROVAL_STAMPED:
        if action is ChangeEventAction.CONFIRM:
            row.approved_by = actor_user_id
            row.approved_at = now
    else:
        row.approved_by = None
        row.approved_at = None
    if rationale is not None:
        row.rationale = rationale
    details = _transition_audit_details(
        action=action, before=before, after=row.status, version_no=row.version_no,
        rationale=rationale,
    )
    if extra_details:
        details.update(extra_details)
    await audit.record_event(
        session,
        tenant_id=row.tenant_id,
        action=AuditAction.CHANGE_EVENT_TRANSITION_RECORDED,
        actor_user_id=actor_user_id,
        company_id=row.company_id,
        entity_type="change_event",
        entity_id=row.id,
        details=details,
    )
    await session.flush()


async def _revise(
    session: AsyncSession,
    *,
    row: ChangeEvent,
    changes: Mapping[str, Any],
    actor_user_id: UUID | None,
    rationale: str | None,
    as_of: date,
) -> ChangeEventView:
    """Append the next version of an event (§2.6: a revision *is* a new version).

    The new row carries the event forward: the status is unchanged (the §4.1
    ``edit`` rows target the status the event stands in), the review and
    approval stamps ride with it, and the linkage and provenance are the
    event's history rather than its content (``EDIT_FIELDS``). ``created_by``
    is the account that revised it, not the one that entered it — the chain
    carries both.
    """
    event_type = ChangeEventType(changes.get("type", row.type))
    payload = changes.get("payload", row.payload)
    version = row.version_no + 1
    new_row = ChangeEvent(
        id=row.id,
        version_no=version,
        tenant_id=row.tenant_id,
        company_id=row.company_id,
        type=event_type.value,
        status=row.status,
        effective_date=changes.get("effective_date", row.effective_date),
        effective_date_text=changes.get("effective_date_text", row.effective_date_text),
        informed_date=changes.get("informed_date", row.informed_date),
        payload=dict(payload) if isinstance(payload, Mapping) else {},
        source_type=row.source_type,
        source_id=row.source_id,
        related_event_id=row.related_event_id,
        relation=row.relation,
        reviewed_by=row.reviewed_by,
        reviewed_at=row.reviewed_at,
        approved_by=row.approved_by,
        approved_at=row.approved_at,
        rationale=row.rationale if rationale is None else rationale,
        created_by=actor_user_id,
    )
    try:
        async with session.begin_nested():
            session.add(new_row)
            await session.flush()
    except IntegrityError:
        # The primary key refused a second row for this (id, version_no): a
        # revision committed between this writer's lock and its insert, which
        # the lock makes impossible except under a lost statement snapshot.
        # The caller re-reads and retries (draft_conflict's remedy).
        raise ChangeEventConflictError from None
    await audit.record_event(
        session,
        tenant_id=row.tenant_id,
        action=AuditAction.CHANGE_EVENT_TRANSITION_RECORDED,
        actor_user_id=actor_user_id,
        company_id=row.company_id,
        entity_type="change_event",
        entity_id=row.id,
        details=_transition_audit_details(
            action=ChangeEventAction.EDIT,
            before=row.status,
            after=row.status,
            version_no=version,
            rationale=rationale,
        ),
    )
    await session.flush()
    return await _event_view(
        session, tenant_id=row.tenant_id, event_id=row.id, as_of=as_of
    )


async def apply_to_draft(
    session: AsyncSession, *, row: ChangeEvent, actor_user_id: UUID | None = None
) -> ChangeEvent:
    """Move one approved event to ``applied_to_draft`` — §4.1's system ``apply``.

    The draft-generation path's leg, and its only caller. The row comes from
    ``locked_approved_events``, so the caller already holds the event's lock;
    this function asks the same guard the executor asks, writes the status
    through the same tail (``_move_row``) and appends the same audit entry, so
    the system's application and a staff action cannot describe one status move
    in two ways.

    An event already ``applied_to_draft`` is left as it stands and returned: a
    later draft links it again — §3.1's rule that approval carries until
    ``submit`` files it — and linking is not a second application, so nothing is
    written and no entry is appended. Anything else raises
    ``ChangeEventTransitionError``: the event left the approved set underfoot,
    and the caller aborts rather than building a draft from a withdrawn event.
    """
    status = ChangeEventStatus(row.status)
    if status is ChangeEventStatus.APPLIED_TO_DRAFT:
        return row
    if not admits(status, ChangeEventAction.APPLY):
        raise ChangeEventTransitionError(status=status, action=ChangeEventAction.APPLY)
    await _move_row(
        session,
        row=row,
        action=ChangeEventAction.APPLY,
        actor_user_id=actor_user_id,
        rationale=None,
    )
    # Ruling R4: applying the event can move a filing deadline, so the push
    # sweep is asked to reconcile — a no-op when no loop runs and never an
    # error inside the request. (``apply`` is a system row: this is the only
    # apply seam, the draft-generation path's own leg.)
    calendar_sync.request_push_sweep(row.tenant_id)
    return row


async def submit_applied_events(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    applied_event_ids: Collection[UUID],
    actor_user_id: UUID | None = None,
) -> list[UUID]:
    """Move the exported draft's applied events to ``submitted`` — §4.1's
    system row, performed by the export.

    The ``apply_to_draft`` mirror, for the filing side of the story: per id,
    the event is locked and re-read (the module's two-statement pattern), and
    the move runs only when the event still stands ``applied_to_draft`` —
    through the same guard's intent, the same ``_move_row`` tail and the same
    ``change_event_transition_recorded`` entry a staff action would leave.
    An event that left the applied set underfoot (a withdrawal racing the
    export — the draft would have been invalidated instead) or is already
    ``submitted`` is **skipped**, not raised: the export idempotence rule
    (Review Focus 4) is that the move is per-event idempotent, so the
    function returns exactly the ids it moved and the caller's package
    records which events this filing carried.

    The export is the only caller, and it rides the export's session: the
    filing package, the draft's move to ``exported`` and the events'
    submission are one transaction — nothing files by halves.
    """
    submitted: list[UUID] = []
    for event_id in applied_event_ids:
        row = await _locked_event(session, tenant_id=tenant_id, event_id=event_id)
        if ChangeEventStatus(row.status) is not ChangeEventStatus.APPLIED_TO_DRAFT:
            continue
        await _move_row(
            session,
            row=row,
            action=ChangeEventAction.SUBMIT,
            actor_user_id=actor_user_id,
            rationale=None,
        )
        submitted.append(row.id)
    return submitted


async def client_withdraw_from_reply(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    event_id: UUID,
    reply_communication_id: UUID,
    as_of: date,
) -> ChangeEventView:
    """§4.1's external-contact leg (M5b): a client reply withdraws an event.

    The reply parser is the only caller. Where the staff-recorded
    ``client_withdraw`` is a person recording what a client said by phone or
    in person (through ``transition``, with the review leg and its required
    rationale), this leg is the record acting on the client's own written
    words: the matched earlier event of the withdrawal parse moves to
    ``withdrawn`` with the reply named as its source in the audit chain. The
    same lock, the same guard, the same write tail and the same draft
    invalidation as every other move — no second path to ``withdrawn``.

    ``ChangeEventNotFoundError`` for an unknown or cross-tenant id, and
    ``ChangeEventTransitionError`` when the event's current status does not
    admit a withdrawal (``submitted`` cannot be withdrawn once filed;
    ``withdrawn``/``deleted`` are terminals) — the caller treats both as "the
    reference cannot land", never as a guess.
    """
    row = await _locked_event(session, tenant_id=tenant_id, event_id=event_id)
    status = ChangeEventStatus(row.status)
    if not admits(status, ChangeEventAction.CLIENT_WITHDRAW):
        raise ChangeEventTransitionError(status=status, action=ChangeEventAction.CLIENT_WITHDRAW)
    await _move_row(
        session,
        row=row,
        action=ChangeEventAction.CLIENT_WITHDRAW,
        actor_user_id=None,
        rationale=None,
        extra_details={"source_communication_id": str(reply_communication_id)},
    )
    if status is ChangeEventStatus.APPLIED_TO_DRAFT:
        await _invalidate_linked_drafts(
            session, row=row, action=ChangeEventAction.CLIENT_WITHDRAW, actor_user_id=None
        )
    return await _event_view(session, tenant_id=tenant_id, event_id=event_id, as_of=as_of)


async def reconfirming_events(
    session: AsyncSession, *, tenant_id: UUID, company_id: UUID
) -> list[ChangeEvent]:
    """The company's events whose current version stands at
    ``client_reconfirming`` — the reconfirmation exchange's read.

    The reply parser's landing asks this after a reply joins a request's
    thread: every event out with the client on that company's threads
    returns to ``pending`` (``client_reply_from_parse``), because a reply
    means the client's answer is in and staff review exists again. The
    current version is selected the queue read's way — the highest
    ``version_no`` per event — never a row-level filter a revision could
    misread. A read: no writes, no audit entries.
    """
    latest = (
        select(
            ChangeEvent.id.label("id"),
            func.max(ChangeEvent.version_no).label("version_no"),
        )
        .where(ChangeEvent.tenant_id == tenant_id, ChangeEvent.company_id == company_id)
        .group_by(ChangeEvent.id)
        .subquery()
    )
    rows = await session.scalars(
        select(ChangeEvent)
        .join(
            latest,
            and_(
                ChangeEvent.id == latest.c.id,
                ChangeEvent.version_no == latest.c.version_no,
            ),
        )
        .where(
            ChangeEvent.tenant_id == tenant_id,
            ChangeEvent.company_id == company_id,
            ChangeEvent.status == ChangeEventStatus.CLIENT_RECONFIRMING.value,
        )
        .order_by(ChangeEvent.effective_date, ChangeEvent.id)
    )
    return list(rows.all())


async def client_reply_from_parse(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    event_id: UUID,
    reply_communication_id: UUID,
    as_of: date,
) -> ChangeEventView:
    """§4.1's system row (M5b): a reply joining the reconfirmation's thread
    returns the event to ``pending``.

    The reply parser is the only caller, and it calls this once per event
    ``reconfirming_events`` names, on the same session as the parse landing —
    so the parse, its proposals, and the event's return commit or roll back
    together. The event reappears in the review queue with the reply as the
    new source of truth to review against; the chain names the reply that
    returned it, the way the withdrawal leg names the reply that withdrew it.
    The same lock, the same guard and the same write tail as every other
    move — no second path to ``pending``.

    ``ChangeEventNotFoundError`` for an unknown or cross-tenant id, and
    ``ChangeEventTransitionError`` when the event's current status no longer
    admits a client reply (staff reached it first — a move the reconfirmation
    statuses allow) — the caller treats both as "the reply no longer has
    anything to return", never as a guess.
    """
    row = await _locked_event(session, tenant_id=tenant_id, event_id=event_id)
    status = ChangeEventStatus(row.status)
    if not admits(status, ChangeEventAction.CLIENT_REPLY):
        raise ChangeEventTransitionError(status=status, action=ChangeEventAction.CLIENT_REPLY)
    await _move_row(
        session,
        row=row,
        action=ChangeEventAction.CLIENT_REPLY,
        actor_user_id=None,
        rationale=None,
        extra_details={"source_communication_id": str(reply_communication_id)},
    )
    return await _event_view(session, tenant_id=tenant_id, event_id=event_id, as_of=as_of)


__all__ = [
    "ChangeEventActionNotWiredError",
    "ChangeEventConflictError",
    "ChangeEventForbiddenError",
    "ChangeEventInvalidError",
    "ChangeEventNotFoundError",
    "ChangeEventProtectedClassError",
    "ChangeEventQueueItem",
    "ChangeEventRationaleRequiredError",
    "ChangeEventTransitions",
    "ChangeEventView",
    "ChangeWindowView",
    "apply_to_draft",
    "client_reply_from_parse",
    "client_withdraw_from_reply",
    "create",
    "list_assessments",
    "list_events",
    "locked_approved_events",
    "queue",
    "reconfirming_events",
    "submit_applied_events",
    "transition",
    "transitions_for",
]
