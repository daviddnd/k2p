"""The service-renewal record's service half (scope §2.15; data-model §2.9;
contract §5.1.9).

One row per company per anniversary year records whether the client is
continuing the engagement for that period and whether the fee has arrived
(``app.adapters.persistence.service_renewals``). This module is the only path
that writes one, and the only place the release gate in
``app.domain.service_renewals`` is consulted:

- ``get_or_create`` — the period's row, created ``pending`` if it does not
  exist. Idempotent by construction: the caller asks for "the row for this
  year", and a concurrent pair of requests leaves one row (the unique index
  decides; the loser re-reads and returns the winner's row) because two answers
  about one period would be two answers to the same question.
- ``decide`` — records the client's answer: ``confirmed`` or ``declined``, with
  a note the API makes mandatory for a decline (a refusal that stops a period
  needs a reason). **Nothing here touches the service assignment** (ruled
  2026-09-24): a decline means no renewed coverage, which the §5.1 gate already
  reads as ``out_of_coverage`` once the period ends and the dashboard already
  paints as "service not covered — no automated action". Ending the assignment
  would be the TCSP's own commercial decision, made through the routes that
  already exist for it.
- ``correct`` — fixes a decision that was recorded wrongly, which is the one
  thing the decision route deliberately cannot do (a second answer is
  ``already_closed``). It takes the new decision and a reason the API requires,
  and it clears the stamps the new status does not carry — a period corrected
  away from ``payment_received`` has no payment day any more (a bounced cheque
  is the case this exists for), and the database CHECK would otherwise refuse
  the write. Correcting is audited like every other write, before and after.
- ``record_payment`` — records that the fee was received: a timestamp, the
  staff account, and an optional note. **Nothing more**: no amount, no
  currency, no line item, no receipt, no payment request, no provider call
  (scope §2.15 "tracking only — no invoicing or billing in v1.0";
  ``ServicePackage.fee`` stays exactly what it is and this path never reads
  it).
- ``_record`` is **the one executor** ADR-0008 asks for: it loads the row under
  ``FOR UPDATE`` (the ``application/quarantine`` precedent), asks the guard,
  writes the state, and appends ``service_renewal_recorded`` on the same
  session — so the change and its audit entry commit or roll back together.
  The lock is the whole concurrency story: two overlapping decisions would
  otherwise both read ``pending`` and both close the period, appending a second
  entry to an append-only chain for one answer.
- ``ensure_release_permitted`` is **the gate**, and the only one the record
  closes since the founder's revision of 2026-09-24: a document that goes to a
  client, filled or not, waits until the company's assigned package covers its
  form (and the coverage period holds) and the period is recorded as continuing
  and paid — and the refusal names which of the two is missing, because
  "record the client's answer" and "record the fee" are different errands. It is
  where ``document_release_refused`` is written — **only on denial**: a
  permitted download changes nothing, and ADR-0009's chain records changes (the
  sensitive-class download entries are their own rule, in
  ``app.application.documents``).
- ``pending_actions`` is the §5.1.9 panel read: per company per anniversary
  year, every milestone from the tenant's configuration with its date, its days
  remaining, its state and — whenever the state is ``blocked`` — the leg that
  refused. It is composed from the same things the gate reads — the renewal
  row, the company's coverage and whether the record that completes a milestone
  exists — so a milestone reads ``blocked`` exactly when the gate the panel can
  judge would refuse, and the panel cannot contradict the API for a case both
  can see.

Reads write no audit entry and no row (the compliance read models' rule): the
panel is derived, and asking what is due is not a change to tenant data.

Services flush but never commit — the API session dependency commits at the
request edge.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.persistence.communications import Communication
from app.adapters.persistence.confirmation import ConfirmationRequest
from app.adapters.persistence.documents import Document
from app.adapters.persistence.service_renewals import ServiceRenewal
from app.application import audit, company_master, compliance, service_packages, tenant_config
from app.domain.audit import AuditAction
from app.domain.communications import CommunicationKind, Direction
from app.domain.milestones import MilestoneKind, Milestones, milestone_dates
from app.domain.rbac import ActorContext
from app.domain.reminders import ReminderState, reminder_state
from app.domain.service_packages import covered_form
from app.domain.service_renewals import (
    RENEWAL_TRANSITIONS,
    MilestoneBlockReason,
    MilestoneState,
    ReleaseBlockReason,
    RenewalAction,
    RenewalStatus,
    RenewalTransitionError,
    can_transition,
    milestone_state,
    period_release_block_reason,
    release_block_reason,
)

# The panel's default forward horizon, and its cap. The cap is a year because
# the panel is a working view of the periods still open, not a report; the
# lookback past the horizon is the dashboard's own (30 days,
# ``app.application.compliance.OVERDUE_LOOKBACK_DAYS``), so a deadline that has
# left the operational window falls off the panel exactly as it falls off the
# dashboard.
DEFAULT_DUE_WITHIN_DAYS = 90
MAX_DUE_WITHIN_DAYS = 365

# The two answers a client can give, and the action each records. A closed
# mapping rather than a branch, so a caller that passes a status the client
# cannot have given (``payment_received`` is staff's record, not an answer) is
# refused instead of being reinterpreted as a decline.
_DECISION_ACTIONS: dict[RenewalStatus, RenewalAction] = {
    RenewalStatus.CONFIRMED: RenewalAction.CONFIRM,
    RenewalStatus.DECLINED: RenewalAction.DECLINE,
}

# The same two answers, as the correction records them. A second mapping
# because the *actions* differ: the decision route records an answer, the
# correction route overwrites one, and the transition table is what keeps the
# two apart — a correction can never be read as a first answer, and a
# first answer can never overwrite a decision.
_CORRECTION_ACTIONS: dict[RenewalStatus, RenewalAction] = {
    RenewalStatus.CONFIRMED: RenewalAction.CORRECT_TO_CONFIRMED,
    RenewalStatus.DECLINED: RenewalAction.CORRECT_TO_DECLINED,
}


def _action_for(
    decision: RenewalStatus, *, table: Mapping[RenewalStatus, RenewalAction]
) -> RenewalAction:
    """The guard action a decision records in one of the two mappings."""
    action = table.get(decision)
    if action is None:
        raise ValueError(
            f"{decision!r} is not a decision a client can give "
            f"(one of: {', '.join(sorted(state.value for state in table))})"
        )
    return action


def _decision_action(decision: RenewalStatus) -> RenewalAction:
    """The guard action a client's answer records, or a refusal."""
    return _action_for(decision, table=_DECISION_ACTIONS)


def _correction_action(decision: RenewalStatus) -> RenewalAction:
    """The guard action a correction to that decision records, or a refusal."""
    return _action_for(decision, table=_CORRECTION_ACTIONS)


class RenewalNotFoundError(Exception):
    """No renewal row for that company and year (cross-tenant → unknown)."""


# The gate's two period-wide reasons answer ``<reason>: <year>`` — for either of
# them the period is the coordinate staff need — while every scope reason answers
# ``service_not_in_scope: <reason>``, where the reason itself names the remedy.
# The pairing lives here so the wire shape cannot drift from the vocabulary: a
# reason added to the domain without a decision about its shape is a visible
# omission, not a silent fall-through.
_PERIOD_REASONS = frozenset(
    {ReleaseBlockReason.CONTINUATION_NOT_RECORDED, ReleaseBlockReason.PAYMENT_NOT_RECEIVED}
)


def gate_detail(reason: ReleaseBlockReason, anniversary_year: int) -> str:
    """One refusal as the wire names it — composed once, for every caller.

    The download route answers it, the sender records it on the outbox row and
    reports it, and the two cannot describe one refusal in two vocabularies:
    ``service_not_in_scope: <reason>`` for the scope legs,
    ``continuation_not_recorded: <year>`` and ``payment_not_received: <year>``
    for the two period legs (contract §5.1.3).
    """
    return (
        f"{reason.value}: {anniversary_year}"
        if reason in _PERIOD_REASONS
        else f"service_not_in_scope: {reason.value}"
    )


@dataclass(frozen=True)
class ReleaseRefusal:
    """One refused release: the leg, the period, and the work it was judged on.

    The pure predicate's answer, resolved — what ``release_block_reason``
    decided plus the coordinates the record and the wire need. ``form`` is
    ``None`` for a document whose work nothing can name (the gate's
    ``form_not_covered`` leg, fail-safe by design).

    ``release_refusal`` builds it with no side effect, ``record_release_refusal``
    writes the one entry for it, and ``ensure_release_permitted`` does both —
    which is what lets the send path refuse a blocked release **once per
    distinct reason** instead of writing the same entry every cycle.
    """

    reason: ReleaseBlockReason
    anniversary_year: int
    form: str | None

    @property
    def detail(self) -> str:
        """The wire string this refusal answers with (``gate_detail``)."""
        return gate_detail(self.reason, self.anniversary_year)


class RenewalGateRefusedError(Exception):
    """A client-bound release was refused by the renewal gate.

    ``reason`` is the closed vocabulary (``app.domain.service_renewals``),
    ``anniversary_year`` the period the caller is refused for, and ``detail``
    the wire string both routes answer (contract §5.1.3). Composed by
    ``gate_detail``, so callers cannot drift.
    """

    def __init__(self, *, reason: ReleaseBlockReason, anniversary_year: int) -> None:
        self.reason = reason
        self.anniversary_year = anniversary_year
        self.detail = gate_detail(reason, anniversary_year)
        super().__init__(f"the release is refused: {self.detail}")


@dataclass(frozen=True)
class _ReplyChain:
    """One period's reply chain as the panel reads it: whether a reply has
    joined the latest request's thread (the milestone's done signal), and the
    block reason while the chain is spent and unanswered.

    Both facts derive from the same reads the §5.1.12 sweep uses — the reply's
    presence and the send instant against the tenant's cadence — so the panel
    and the sweep cannot drift apart on what "answered" means.
    """

    replied: bool
    block: MilestoneBlockReason | None


@dataclass(frozen=True)
class ClientActionMilestone:
    """One milestone of one period, as of the panel read's day.

    ``date`` is the configured date (the anniversary minus its offset) and
    ``days_remaining`` its distance from ``as_of`` — negative once the day has
    passed, the compliance calendar's own convention. ``state`` is
    ``app.domain.service_renewals.milestone_state``'s derivation, and
    ``blocked_reason`` says **which leg refused** whenever the state is
    ``blocked`` — the release gate's own vocabulary, or the reply's
    precondition — and is ``None`` whenever the milestone is not blocked. A
    bare ``blocked`` makes staff guess at the remedy; the reason is the
    coordinate that names it, and ``state`` is derived from it rather than from
    a second boolean that could disagree.
    """

    kind: MilestoneKind
    date: date
    days_remaining: int
    state: MilestoneState
    blocked_reason: MilestoneBlockReason | None


@dataclass(frozen=True)
class ClientActionPeriod:
    """One company's one anniversary period, and everything due for it.

    ``renewal_status`` is ``None`` when no row was recorded for the period yet
    — one staff member has not recorded the client's answer, which changes
    nothing about what may be drafted (the founder's revision of 2026-09-24)
    and holds only the release.
    """

    company_id: UUID
    br_number: str
    company_name: str | None
    anniversary_year: int
    anniversary_date: date
    renewal_status: str | None
    milestones: list[ClientActionMilestone]


@dataclass(frozen=True)
class ClientActions:
    """The panel: the periods inside the read's window, oldest period first."""

    as_of: date
    due_within: int
    window_from: date
    window_to: date
    periods: list[ClientActionPeriod]


@dataclass(frozen=True)
class _ClientBoundArtifact:
    """A document the product produced *for* a client: its work and its period.

    ``form`` is the registry form code of the work the document is
    (``app.domain.service_packages.covered_form``), which is what the assigned
    package's ``covered_forms`` is compared against; it is ``None`` when the
    record that points at the document names no form the registry ships, and
    the gate reads that as not covered.
    """

    form: str | None
    anniversary_year: int


async def _get_row(
    session: AsyncSession, *, tenant_id: UUID, company_id: UUID, anniversary_year: int
) -> ServiceRenewal | None:
    row: ServiceRenewal | None = await session.scalar(
        select(ServiceRenewal).where(
            ServiceRenewal.tenant_id == tenant_id,
            ServiceRenewal.company_id == company_id,
            ServiceRenewal.anniversary_year == anniversary_year,
        )
    )
    return row


async def _locked_row(
    session: AsyncSession, *, tenant_id: UUID, company_id: UUID, anniversary_year: int
) -> ServiceRenewal:
    """The period's row, locked for the transition that is about to happen.

    Taken before the guard is asked, so two overlapping requests cannot both
    read the same status and both write: the loser waits, then reads what the
    winner left and is refused by the guard exactly as it would be a moment
    later (the quarantine surface's precedent).
    """
    row: ServiceRenewal | None = await session.scalar(
        select(ServiceRenewal)
        .where(
            ServiceRenewal.tenant_id == tenant_id,
            ServiceRenewal.company_id == company_id,
            ServiceRenewal.anniversary_year == anniversary_year,
        )
        .with_for_update()
    )
    if row is None:
        raise RenewalNotFoundError
    return row


async def _audit(
    session: AsyncSession,
    *,
    row: ServiceRenewal,
    actor_user_id: UUID | None,
    before: str | None,
    note: str | None,
) -> None:
    """One audit entry per write, on the caller's session (ADR-0009).

    ``before`` is the status the row stood in — ``None`` when the row itself is
    what this write created, the tenant-config precedent for "there was
    nothing". The note travels with it: a decline's reason is staff's own
    record of why a period stopped, and the chain is where a later correction
    of the row's single ``note`` column cannot erase it.
    """
    details: dict[str, object] = {
        "anniversary_year": row.anniversary_year,
        "before": before,
        "after": row.status,
    }
    if note is not None:
        details["note"] = note
    await audit.record_event(
        session,
        tenant_id=row.tenant_id,
        action=AuditAction.SERVICE_RENEWAL_RECORDED,
        actor_user_id=actor_user_id,
        company_id=row.company_id,
        entity_type="service_renewal",
        entity_id=row.id,
        details=details,
    )


async def get_or_create(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    company_id: UUID,
    anniversary_year: int,
    actor_user_id: UUID | None = None,
) -> ServiceRenewal:
    """The period's renewal row, created ``pending`` if it does not exist.

    Creating a row is a change to tenant data, so it is audited like any other
    write (``before: null``, ``after: pending``). A concurrent pair of creates
    is the same request twice: the unique index lets one insert through, the
    loser meets it on a savepoint and re-reads the winner's row — which is what
    the caller asked for — without a second audit entry, because the winner
    already wrote one.
    """
    company = await company_master.get_company(session, tenant_id=tenant_id, company_id=company_id)
    existing = await _get_row(
        session, tenant_id=tenant_id, company_id=company.id, anniversary_year=anniversary_year
    )
    if existing is not None:
        return existing
    row = ServiceRenewal(
        tenant_id=tenant_id,
        company_id=company.id,
        anniversary_year=anniversary_year,
        status=RenewalStatus.PENDING.value,
    )
    try:
        async with session.begin_nested():
            session.add(row)
            await session.flush()
    except IntegrityError:
        concurrent = await _get_row(
            session, tenant_id=tenant_id, company_id=company.id, anniversary_year=anniversary_year
        )
        assert concurrent is not None  # the index refused this insert because of it
        return concurrent
    await _audit(session, row=row, actor_user_id=actor_user_id, before=None, note=None)
    await session.flush()
    return row


async def decide(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    company_id: UUID,
    anniversary_year: int,
    decision: RenewalStatus,
    actor_user_id: UUID | None = None,
    note: str | None = None,
) -> ServiceRenewal:
    """Record the client's answer for the period: ``confirmed`` or ``declined``.

    ``decision`` is one of those two statuses — the API rejects anything else
    before this is called — and it is mapped to the guard's action here, so the
    transition table stays the single statement of what is legal. Raises
    ``RenewalNotFoundError`` for a period with no row (the caller creates it
    first) and ``RenewalTransitionError`` when the period is already decided:
    a decision stands, and a second one would be a different answer to the same
    question.
    """
    company = await company_master.get_company(session, tenant_id=tenant_id, company_id=company_id)
    action = _decision_action(decision)
    row = await _locked_row(
        session, tenant_id=tenant_id, company_id=company.id, anniversary_year=anniversary_year
    )
    return await _record(session, row=row, action=action, actor_user_id=actor_user_id, note=note)


async def record_payment(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    company_id: UUID,
    anniversary_year: int,
    actor_user_id: UUID | None = None,
    note: str | None = None,
) -> ServiceRenewal:
    """Record that the period's fee was received — a timestamp, an actor, a note.

    Only a ``confirmed`` period admits it (the guard), so payment before
    confirmation is refused rather than stored: the API answers
    ``service_not_confirmed`` for a period the client has not confirmed, and
    ``already_closed`` for one that is already paid.
    """
    company = await company_master.get_company(session, tenant_id=tenant_id, company_id=company_id)
    row = await _locked_row(
        session, tenant_id=tenant_id, company_id=company.id, anniversary_year=anniversary_year
    )
    return await _record(
        session,
        row=row,
        action=RenewalAction.RECORD_PAYMENT,
        actor_user_id=actor_user_id,
        note=note,
    )


async def correct(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    company_id: UUID,
    anniversary_year: int,
    decision: RenewalStatus,
    actor_user_id: UUID | None = None,
    note: str | None = None,
) -> ServiceRenewal:
    """Correct a recorded decision: the answer becomes ``decision``, with a reason.

    The one thing the decision route cannot do, and the reason this path
    exists: a mis-clicked ``declined`` would otherwise stop a client's whole
    year, and a wrongly-recorded payment would leave the release gate wrong
    for ever. ``note`` is the correction's reason and the API requires it — a
    change to a regulated client record needs one — and the stamps the new
    status does not carry are cleared (a correction away from
    ``payment_received`` takes the payment back, which is the bounced-cheque
    case; the database CHECK requires the stamps to agree with the status).

    Legal only where the transition table says so: from ``confirmed``,
    ``declined`` or ``payment_received``, and only when the decision actually
    *changes* — re-recording the status a row already holds is refused
    (``RenewalTransitionError``) rather than written as a correction that
    corrected nothing. ``RenewalNotFoundError`` for a period with no row.
    """
    company = await company_master.get_company(session, tenant_id=tenant_id, company_id=company_id)
    action = _correction_action(decision)
    row = await _locked_row(
        session, tenant_id=tenant_id, company_id=company.id, anniversary_year=anniversary_year
    )
    return await _record(session, row=row, action=action, actor_user_id=actor_user_id, note=note)


async def _record(
    session: AsyncSession,
    *,
    row: ServiceRenewal,
    action: RenewalAction,
    actor_user_id: UUID | None,
    note: str | None,
) -> ServiceRenewal:
    """**The one executor** (ADR-0008): guard → state → audit, one transaction.

    The guard is asked over the transition table, so this function carries no
    legality of its own; what it owns is the *side effect* of reaching a status
    — which stamp and which account the target state writes, and which stamps
    it clears — and the audit entry that says what changed, from what, on whose
    authority.

    Reaching a decision (by recording it or by correcting one) writes the
    deciding account and day and leaves the row with no payment: the status a
    decision carries has no payment stamp, and its CHECK would refuse the write
    if the old one were left behind. A correction to a decision thus *is* the
    bounce-recovery path — correct the period back to ``confirmed`` and record
    the fee again when it really arrives.

    A note replaces the row's own when the request carries one and is left
    alone when it does not: erasing a recorded reason by omission would be the
    wrong default, and each version stays in the chain either way.
    """
    status = RenewalStatus(row.status)
    if not can_transition(RENEWAL_TRANSITIONS, status=status, action=action):
        raise RenewalTransitionError(status=status, action=action)
    target = RENEWAL_TRANSITIONS[status][action]
    before = row.status
    row.status = target.value
    if target is RenewalStatus.PAYMENT_RECEIVED:
        row.payment_received_at = datetime.now(UTC)
        row.payment_by = actor_user_id
    else:
        row.client_decided_at = datetime.now(UTC)
        row.decided_by = actor_user_id
        row.payment_received_at = None
        row.payment_by = None
    if note is not None:
        row.note = note
    await _audit(session, row=row, actor_user_id=actor_user_id, before=before, note=note)
    await session.flush()
    return row


async def list_for_company(
    session: AsyncSession, *, tenant_id: UUID, company_id: UUID
) -> list[ServiceRenewal]:
    """The company's renewal rows, newest period first.

    ``CompanyNotFoundError`` for an unknown or cross-tenant company, like every
    other company-scoped read.
    """
    company = await company_master.get_company(session, tenant_id=tenant_id, company_id=company_id)
    rows = await session.scalars(
        select(ServiceRenewal)
        .where(ServiceRenewal.company_id == company.id)
        .order_by(ServiceRenewal.anniversary_year.desc())
    )
    return list(rows.all())


async def _client_bound_artifact(
    session: AsyncSession, *, tenant_id: UUID, document: Document
) -> _ClientBoundArtifact | None:
    """The work and period of the request this document is the letter of, or ``None``.

    What the release gate covers is what goes **to the client**: a
    form-bearing artifact the product generated. In this build the only writer
    of one is the confirmation generator, so the evidence that a document is
    client-bound is the request row that stores it — which carries the work
    (its ``form_type``, resolved to the registry's form code) and the period
    (``target_date``'s year, the same anniversary year the renewal row is keyed
    by, because the made-up date *is* the anniversary).

    A ``generated_draft`` a staff member uploaded, a prior filing, a signed
    upload: none has a request row, and none is gated. That is the ruling of
    2026-09-24 — the TCSP's own commercial precondition applies to what it
    sends out, never to its staff's working copies or its own reads — and it is
    also what keeps this gate clear of ADR-0012's line that manual fill →
    sign → export is never blocked. When the Phase 5 draft lifecycle lands, its
    rows join this resolution (the predicate is unchanged; the table that says
    "produced for a client" grows), and the M2 send path must consult the same
    predicate before a message leaves the deployment.

    **One row, deterministically, and never a raise.** Two request rows pointing
    at one document is not a state this build can produce (each row is written
    with its own document), but a hand-edited database must not turn a download
    into a ``MultipleResultsFound`` 500: a crash is neither a permit nor a
    refusal, and the gate would have been skipped on exactly the document it
    exists for. The read therefore takes the oldest such row — ordered by
    creation and id, so the same document classifies the same way every time —
    and the document is client-bound either way.
    """
    found = (
        (
            await session.execute(
                select(ConfirmationRequest.form_type, ConfirmationRequest.target_date)
                .where(
                    ConfirmationRequest.tenant_id == tenant_id,
                    ConfirmationRequest.document_id == document.id,
                )
                .order_by(ConfirmationRequest.created_at, ConfirmationRequest.id)
                .limit(1)
            )
        )
        .tuples()
        .first()
    )
    if found is None:
        return None
    form_type, target_date = found
    form = covered_form(form_type)
    return _ClientBoundArtifact(
        form=None if form is None else form.value, anniversary_year=target_date.year
    )


async def release_refusal(
    session: AsyncSession, *, tenant_id: UUID, document: Document, as_of: date
) -> ReleaseRefusal | None:
    """Consult the release gate: why this document's release is refused, or ``None``.

    **No side effect**, deliberately: the download route refuses once per
    request, but the send path retries a blocked row every cycle, and a
    predicate that wrote an entry every time would fill an append-only chain
    with one unchanged fact. The caller that must record the refusal calls
    ``record_release_refusal``; ``ensure_release_permitted`` does both.

    A document that is not client-bound — anything but the letter of a
    confirmation request, in this build — has no refusal at all: the gate has
    nothing to say about it, and no read of the assignment or the renewal table
    happens (the ruling of 2026-09-24: the TCSP's own working copies, internal
    uploads and reads are not releases).
    """
    artifact = await _client_bound_artifact(session, tenant_id=tenant_id, document=document)
    if artifact is None:
        return None
    # A client-bound letter is its request's company's document — the request
    # the artifact came from is company-bound, and the only company-less
    # documents (migration 0035, the clarification loop's letters) have no
    # request row to be found by.
    assert document.company_id is not None
    coverage = await service_packages.service_coverage(
        session, tenant_id=tenant_id, company_id=document.company_id
    )
    row = await _get_row(
        session,
        tenant_id=tenant_id,
        company_id=document.company_id,
        anniversary_year=artifact.anniversary_year,
    )
    reason = release_block_reason(
        form=artifact.form,
        covered_forms=coverage.covered_forms,
        assignment_status=coverage.assignment_status,
        coverage_start=coverage.coverage_start,
        coverage_end=coverage.coverage_end,
        renewal_status=None if row is None else RenewalStatus(row.status),
        as_of=as_of,
    )
    if reason is None:
        return None
    return ReleaseRefusal(
        reason=reason, anniversary_year=artifact.anniversary_year, form=artifact.form
    )


async def record_release_refusal(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    document: Document,
    refusal: ReleaseRefusal,
    actor_user_id: UUID | None = None,
    outbox_id: UUID | None = None,
) -> None:
    """Write the one ``document_release_refused`` entry a refusal is worth.

    The entry carries the reason, the period, the work and the document's kind
    — never any document content (contract §5.1.9). ``outbox_id`` is the send
    path's addition: when the refusal stopped a queued message, the row it
    withheld is the coordinate a reader needs to find the queue entry, and the
    download route passes nothing because no queue is involved. **Committed
    before the caller answers** (the route) or with the cycle's per-row commit
    (the worker): a refusal rolled back would leave no trace at all.
    """
    details: dict[str, object] = {
        "reason": refusal.reason.value,
        "anniversary_year": refusal.anniversary_year,
        "form": refusal.form,
        "kind": document.kind,
    }
    if outbox_id is not None:
        details["outbox_id"] = str(outbox_id)
    await audit.record_event(
        session,
        tenant_id=tenant_id,
        action=AuditAction.DOCUMENT_RELEASE_REFUSED,
        actor_user_id=actor_user_id,
        company_id=document.company_id,
        entity_type="document",
        entity_id=document.id,
        details=details,
    )


async def ensure_release_permitted(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    document: Document,
    as_of: date,
    actor_user_id: UUID | None = None,
) -> None:
    """**The release gate**: a client-bound artifact needs scope and payment.

    Consulted by the download route before any URL is issued
    (``app.api.documents``) — the moment a document goes to a client, which is
    where the founder's revision of 2026-09-24 put the gate — and by the send
    path before a message leaves (``app.application.mail_send``), which consults
    the same predicate through ``release_refusal`` so the two callers cannot
    disagree about what a release needs.

    The legs are the pure ``release_block_reason``'s, in its ruled order: the
    company's relevant assignment (the active one, else the latest — the same
    resolution ``check_automation`` uses, read through
    ``service_packages.service_coverage``, so the two gates cannot disagree
    about which package covers the company), its coverage period, the work's
    form against the package's ``covered_forms``, and the period's payment.

    On denial one audit entry (``document_release_refused``) is written on the
    caller's session and ``RenewalGateRefusedError`` is raised; on a permitted
    download **nothing is written**, because nothing changed (ADR-0009 records
    changes, and the sensitive-class download entries are their own rule).
    """
    refusal = await release_refusal(session, tenant_id=tenant_id, document=document, as_of=as_of)
    if refusal is None:
        return
    await record_release_refusal(
        session,
        tenant_id=tenant_id,
        document=document,
        refusal=refusal,
        actor_user_id=actor_user_id,
    )
    raise RenewalGateRefusedError(reason=refusal.reason, anniversary_year=refusal.anniversary_year)


async def pending_actions(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    context: ActorContext,
    as_of: date,
    due_within: int = DEFAULT_DUE_WITHIN_DAYS,
) -> ClientActions:
    """The panel: every milestone due for the companies the actor may see.

    A period is listed when at least one of its four milestone dates falls in
    ``[as_of - 30 days, as_of + due_within]`` — the dashboard's own overdue
    lookback, so a period that has left the operational window falls off this
    panel exactly as it falls off the dashboard. The candidate years are
    ``as_of.year - 1`` to ``as_of.year + 2``: a period's milestones lie between
    its anniversary minus the largest configured offset (a year at most) and
    the anniversary itself, so no period outside those four can have a date in
    the window. The bound is stated rather than searched for.

    Milestones whose record does not exist yet read by their date — ``pending``,
    ``due``, ``overdue`` — and the signature posting reads ``blocked`` exactly
    when the release gate would refuse it for that period, judged on the read's
    day. That judgement is the gate's own legs minus the one a period cannot
    answer: a milestone names no form, so the panel judges the assignment, the
    coverage and the period's record, and the form leg is judged where a
    document is — at the download
    (``app.domain.service_renewals.period_release_block_reason`` states the
    split, and ``_blocked`` below repeats it in one place).

    What *completes* a milestone is only ever something the product recorded: a
    decided renewal row for the continuation ask, a generated request for the
    data confirmation, and — since the reply parser (M5b) — a reply on the
    request's thread for the client's reply (the §5.1.12 join: an inbound
    ``Communication`` carrying the request's id). Nothing in this build
    records that the documents were posted, so that milestone has no ``done``
    signal and is read by its date — stated here because a reader must not
    take the panel for a tracker it is not.
    """
    window_from = as_of - timedelta(days=compliance.OVERDUE_LOOKBACK_DAYS)
    window_to = as_of + timedelta(days=due_within)
    companies = await compliance.visible_companies(
        session, tenant_id=tenant_id, scope=compliance.company_scope(context)
    )
    if not companies:
        return ClientActions(
            as_of=as_of,
            due_within=due_within,
            window_from=window_from,
            window_to=window_to,
            periods=[],
        )
    company_ids = [company.id for company in companies]
    names = await compliance.display_names(session, company_ids, as_of=as_of)
    milestones = await tenant_config.get_milestones(session, tenant_id=tenant_id)
    renewals = await _renewals_by_period(session, tenant_id=tenant_id, company_ids=company_ids)
    generated = await _generated_periods(session, tenant_id=tenant_id, company_ids=company_ids)
    reply_chains = await _reply_chains(
        session,
        tenant_id=tenant_id,
        company_ids=company_ids,
        milestones=milestones,
        as_of=as_of,
    )
    coverage = await service_packages.service_coverage_for_companies(
        session, company_ids=company_ids
    )
    periods: list[ClientActionPeriod] = []
    for company in companies:
        for year in range(as_of.year - 1, as_of.year + 3):
            dates = milestone_dates(company.anniversary_date, year, milestones=milestones)
            scheduled = dates.ordered()
            if not any(window_from <= day <= window_to for _, day in scheduled):
                continue
            row = renewals.get((company.id, year))
            status = None if row is None else RenewalStatus(row.status)
            periods.append(
                ClientActionPeriod(
                    company_id=company.id,
                    br_number=company.br_number,
                    company_name=names.get(company.id),
                    anniversary_year=year,
                    anniversary_date=dates.anniversary,
                    renewal_status=None if status is None else status.value,
                    milestones=_milestone_rows(
                        scheduled,
                        as_of=as_of,
                        status=status,
                        generated=(company.id, year) in generated,
                        # None = the period has no request yet (the caller maps
                        # that to the reply's own precondition block); the map
                        # covers every period that has one.
                        reply_chain=reply_chains.get((company.id, year)),
                        coverage=coverage[company.id],
                    ),
                )
            )
    periods.sort(key=lambda period: (period.br_number, period.anniversary_year))
    return ClientActions(
        as_of=as_of,
        due_within=due_within,
        window_from=window_from,
        window_to=window_to,
        periods=periods,
    )


def _panel_block_reason(reason: ReleaseBlockReason | None) -> MilestoneBlockReason | None:
    """The gate's leg in the panel's own vocabulary.

    The two vocabularies share the value strings for every leg the panel can
    judge, so this conversion is where the one it cannot — ``form_not_covered``,
    which needs a document's work to compare — fails **loudly** instead of
    reaching the wire: a leg the panel was never supposed to name is a
    deployment reading a panel that has no business answering it, and a
    ``ValueError`` here is better than a sentence about a form the panel cannot
    see. The domain's own test pins which value that is.
    """
    return None if reason is None else MilestoneBlockReason(reason.value)


def _milestone_rows(
    scheduled: Sequence[tuple[MilestoneKind, date]],
    *,
    as_of: date,
    status: RenewalStatus | None,
    generated: bool,
    reply_chain: _ReplyChain | None,
    coverage: service_packages.ServiceCoverage,
) -> list[ClientActionMilestone]:
    """The four milestones' states, as the panel shows them.

    ``done`` is what the record says completed the milestone, ``blocked`` is
    the gate the panel can judge refusing — and *which* leg refused, because a
    state without its remedy leaves staff guessing — and the date decides the
    rest. The blocked reason is the one input: the state is derived from it, so
    a milestone cannot read ``blocked`` with nothing to name.

    Two entries are deliberately never blocked. The **continuation ask**:
    nothing in the product makes it — the sending slice (M2) is what would — so
    today staff make it by hand and only the client's answer is recorded here.
    The **data confirmation**: since the founder's revision of 2026-09-24,
    generating and preparing a document are not gated, so nothing refuses that
    milestone and it reads by its date (the §5.1 automation gate still governs
    the trigger, and the panel has never read it — it is a different seam with
    a different input, the tenant's entitlement). The **reply** is blocked
    while no letter exists to reply to, which is a precondition rather than a
    gate and is named ``no_confirmation_request``; once a letter exists the
    milestone reads by its date until the request's reminder chain runs out —
    then it is ``reminders_exhausted`` (§5.1.12: the final cadence point
    passed with no reply, no automated reminder will follow, and staff take
    over by hand). And once a reply has joined the request's thread — an
    inbound ``Communication`` carrying the request's id, the §5.1.12 join —
    the milestone reads ``done``: the client's answer arrived, and ``done``
    outranks the date and the chain alike. The **signature posting** is
    the release gate's, at the granularity a period has: the assignment, the
    coverage and the period's record (``period_release_block_reason``) — not
    the form leg, which needs a document to judge and is therefore judged at
    the download.
    """
    blocked = {
        MilestoneKind.CONTINUATION_REQUEST: None,
        MilestoneKind.DATA_CONFIRMATION: None,
        MilestoneKind.CLIENT_REPLY: (
            MilestoneBlockReason.NO_CONFIRMATION_REQUEST
            if reply_chain is None
            else reply_chain.block
        ),
        MilestoneKind.SIGNATURE_POST: _panel_block_reason(
            period_release_block_reason(
                as_of=as_of,
                assignment_status=coverage.assignment_status,
                coverage_start=coverage.coverage_start,
                coverage_end=coverage.coverage_end,
                renewal_status=status,
            )
        ),
    }
    done = {
        MilestoneKind.CONTINUATION_REQUEST: status is not None
        and status is not RenewalStatus.PENDING,
        MilestoneKind.DATA_CONFIRMATION: generated,
        MilestoneKind.CLIENT_REPLY: False if reply_chain is None else reply_chain.replied,
        MilestoneKind.SIGNATURE_POST: False,
    }
    return [
        ClientActionMilestone(
            kind=kind,
            date=day,
            days_remaining=(day - as_of).days,
            state=milestone_state(
                day=day, as_of=as_of, done=done[kind], blocked=blocked[kind] is not None
            ),
            blocked_reason=blocked[kind],
        )
        for kind, day in scheduled
    ]


async def _renewals_by_period(
    session: AsyncSession, *, tenant_id: UUID, company_ids: Sequence[UUID]
) -> dict[tuple[UUID, int], ServiceRenewal]:
    """Every renewal row of the visible companies, keyed by (company, year)."""
    rows = await session.scalars(
        select(ServiceRenewal).where(
            ServiceRenewal.tenant_id == tenant_id,
            ServiceRenewal.company_id.in_(company_ids),
        )
    )
    return {(row.company_id, row.anniversary_year): row for row in rows}


async def _generated_periods(
    session: AsyncSession, *, tenant_id: UUID, company_ids: Sequence[UUID]
) -> set[tuple[UUID, int]]:
    """Which (company, year) periods already have a confirmation request.

    The request's ``target_date`` *is* the made-up date, so its year is the
    anniversary year — the same key the renewal rows carry.
    """
    rows = await session.execute(
        select(ConfirmationRequest.company_id, ConfirmationRequest.target_date).where(
            ConfirmationRequest.tenant_id == tenant_id,
            ConfirmationRequest.company_id.in_(company_ids),
        )
    )
    return {(company_id, target_date.year) for company_id, target_date in rows.all()}


async def _reply_chains(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    company_ids: Sequence[UUID],
    milestones: Milestones,
    as_of: date,
) -> dict[tuple[UUID, int], _ReplyChain]:
    """The reply milestone's two facts per (company, year), derived at read time.

    ``replied`` — whether a reply has joined the period's latest request (the
    milestone's done signal, the §5.1.12 join an inbound ``Communication``
    carrying the request's id makes) — and ``block``: ``reminders_exhausted``
    when the period's latest request has left, no reply has joined it, and the
    chain's final cadence point has passed (§5.1.12 — the domain rule reads the
    same derived facts the sweep does: the send instant from the outbound
    ``Communication``, the reply's presence, the tenant's configured cadence);
    ``None`` whenever the chain is still live, which is the milestone reading
    purely by its date. Periods with no request at all are the caller's
    ``no_confirmation_request`` precondition — this map covers only the periods
    that have one. The caller has already read the milestone block — the panel
    fails closed on an unreadable one before this derivation runs.

    The exhaustion judgement is deliberately **time-bound, not send-bound**:
    the read names a spent chain at ``T + c[n-1]`` whether or not the final
    reminder has delivered — that is what "items still unanswered after the
    final reminder are routed to staff" means on the panel.
    """
    cadence = milestones.reminder_cadence_days
    rows = await session.scalars(
        select(ConfirmationRequest).where(
            ConfirmationRequest.tenant_id == tenant_id,
            ConfirmationRequest.company_id.in_(company_ids),
        )
    )
    latest: dict[tuple[UUID, int], ConfirmationRequest] = {}
    for request in rows.all():
        key = (request.company_id, request.target_date.year)
        current = latest.get(key)
        if current is None or (request.version_no, request.created_at, request.id) > (
            current.version_no,
            current.created_at,
            current.id,
        ):
            latest[key] = request
    if not latest:
        return {}
    ids = [request.id for request in latest.values()]
    sent_at = await _request_sent_at(session, ids)
    replied = await _replied_requests(session, ids)
    now = datetime(as_of.year, as_of.month, as_of.day, tzinfo=UTC)
    chains: dict[tuple[UUID, int], _ReplyChain] = {}
    for key, request in latest.items():
        anchor = sent_at.get(request.id)
        if anchor is None:
            # The letter has not left: the chain has not started — nothing is
            # exhausted, and nothing has answered it either.
            chains[key] = _ReplyChain(replied=False, block=None)
            continue
        state = reminder_state(
            sent_at=anchor,
            cadence_days=cadence,
            has_reply=request.id in replied,
            now=now,
        )
        chains[key] = _ReplyChain(
            replied=request.id in replied,
            block=(
                MilestoneBlockReason.REMINDERS_EXHAUSTED
                if state is ReminderState.EXHAUSTED
                else None
            ),
        )
    return chains


async def _request_sent_at(
    session: AsyncSession, request_ids: Sequence[UUID]
) -> dict[UUID, datetime]:
    """Each request's send instant: its outbound exchange's acceptance moment."""
    rows = await session.execute(
        select(Communication.confirmation_request_id, func.min(Communication.received_at))
        .where(
            Communication.confirmation_request_id.in_(request_ids),
            Communication.direction == Direction.OUTBOUND.value,
            Communication.kind == CommunicationKind.CONFIRMATION_REQUEST.value,
        )
        .group_by(Communication.confirmation_request_id)
    )
    return {request_id: received_at for request_id, received_at in rows.all()}


async def _replied_requests(session: AsyncSession, request_ids: Sequence[UUID]) -> set[UUID]:
    """The requests a reply has already joined — the chain is over."""
    rows = await session.scalars(
        select(Communication.confirmation_request_id)
        .where(
            Communication.confirmation_request_id.in_(request_ids),
            Communication.direction == Direction.INBOUND.value,
        )
        .distinct()
    )
    return {request_id for request_id in rows.all() if request_id is not None}


__all__ = [
    "DEFAULT_DUE_WITHIN_DAYS",
    "MAX_DUE_WITHIN_DAYS",
    "ClientActionMilestone",
    "ClientActionPeriod",
    "ClientActions",
    "ReleaseRefusal",
    "RenewalGateRefusedError",
    "RenewalNotFoundError",
    "correct",
    "decide",
    "ensure_release_permitted",
    "gate_detail",
    "get_or_create",
    "list_for_company",
    "pending_actions",
    "record_payment",
    "record_release_refusal",
    "release_refusal",
]
