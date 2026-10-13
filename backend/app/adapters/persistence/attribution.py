"""Attribution-case persistence (ADR-0019 §1; migration ``0033``).

The adapter half of the registration/confirmation flow: the queries and
writes the case lifecycle makes against ``attribution_cases`` — the row the
ingestion cycle opens, the suppression read, the hourly cap's count, the
status moves, and the confirming exchange the act pins to the case (ruling
R5). The model itself lives in ``app.adapters.persistence.models`` with the
rest of the schema; this module is the one place the table's queries are
written, so the slice has a single seam to read (and stays imported in
``alembic/env.py`` with the other persistence modules, the ``282a1e8`` wiring
discipline).

Two rules every caller can rely on:

- **Every read is tenant-scoped.** Another tenant's case id is simply not
  found — the quarantine surface's own answer, so a deployment cannot tell
  "not mine" from "not there".
- **Anchors match by equality, never by NULL.** ``thread_id`` and
  ``original_rfc_message_id`` are the two anchors a thread is matched by (the
  provider's thread id, and the original message's RFC 5322 ``Message-ID``,
  which a reply's ``In-Reply-To``/``References`` carries). The id leg takes a
  **set** of tokens — the caller's ancestor ids — so a case is found from any
  message on the chain, which is the only evidence the IMAP leg has (its
  provider thread id is always ``None``). A case with both anchors NULL
  matches nothing, and a caller that passes neither anchor, or only blank
  values, gets no rows rather than all of them.

**Open** means one of the three awaits (ruling R3, and the clarification
loop's D1); ``resolved``, ``closed`` and ``dismissed`` are terminal. The
suppression read and the processing read look at open cases only: an ask
already sent for a case that has been worked is no reason to suppress the
next one. The anchor reads come in two scopes — ``open_cases_for_thread``
and ``latest_ask_for_thread`` are company-scoped (①'s threaded cases), and
``open_cases_for_anchor`` / ``latest_ask_for_anchor`` are tenant-scoped (the
clarification loop's company-less cases, ruling D2), which no company filter
can see.

Callers flush; the transaction is theirs (the application layer's and, at a
request edge, the API session's).
"""

from collections.abc import Sequence
from datetime import datetime
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from app.adapters.persistence.mail_outbox import MailOutbox
from app.adapters.persistence.models import AttributionCase
from app.domain.attribution import AttributionAskKind, AttributionCaseStatus
from app.domain.mail_send import OutboxKind, OutboxStatus

# The three awaiting statuses — ①'s two and the clarification loop's
# ``awaiting_clarification``. Everything else is terminal, and the open reads
# below select exactly these.
OPEN_STATUSES: tuple[AttributionCaseStatus, ...] = (
    AttributionCaseStatus.AWAITING_REGISTERED_CHANNEL,
    AttributionCaseStatus.AWAITING_CONFIRMATION,
    AttributionCaseStatus.AWAITING_CLARIFICATION,
)

_OPEN_VALUES = [member.value for member in OPEN_STATUSES]


def _anchor_condition(
    *, thread_id: str | None, rfc_message_ids: Sequence[str]
) -> ColumnElement[bool] | None:
    """The condition matching either thread anchor, or ``None`` when neither
    is given (which matches nothing, not everything).

    The two anchors are the provider ``thread_id`` (equality) and the original
    message's RFC 5322 ``Message-ID`` (membership in ``rfc_message_ids`` —
    the ids a reply's ``In-Reply-To``/``References`` carry back, so an
    ancestor's case is found from any descendant on the chain, which is the
    only evidence the IMAP leg has: its ``thread_id`` is always ``None``). A
    blank or ``None`` entry matches no column (the equality rule, unchanged):
    a caller whose message carries no usable token gets no rows rather than
    all of them. Duplicates in the sequence are harmless and folded here.
    """
    ids = [item for item in dict.fromkeys(rfc_message_ids) if item]
    conditions: list[ColumnElement[bool]] = []
    if thread_id is not None:
        conditions.append(AttributionCase.thread_id == thread_id)
    if ids:
        conditions.append(AttributionCase.original_rfc_message_id.in_(ids))
    if not conditions:
        return None
    return or_(*conditions)


async def create_case(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    company_id: UUID | None,
    mailbox_message_id: str,
    sender_address: str,
    status: AttributionCaseStatus,
    thread_id: str | None = None,
    original_rfc_message_id: str | None = None,
    ask_kind: AttributionAskKind | None = None,
) -> AttributionCase:
    """Open one case for one original message and return it, flushed.

    The caller supplies the facts the ingestion cycle resolved: the provider's
    message id (the idempotency key — the unique index refuses a second case
    for it, ever), the sender the decision read, the awaiting ``status`` the
    decision reached, the two thread anchors (either may be absent), and the
    ``ask_kind`` the decision chose (``None`` when the case carries none; a
    suppressed case still carries the kind it was suppressed on). ``company_id``
    is ``None`` for the clarification loop's company-less cases (migration
    ``0034``, ruling D2): the composite foreign key is not checked for a NULL,
    and the company arrives at resolution or closure. ``ask_sent_at``
    stays NULL: the row is written before any ask leaves, and the enqueue
    stamps it through ``set_ask_sent`` in the same transaction (ruling
    R3/R4).
    """
    case = AttributionCase(
        tenant_id=tenant_id,
        company_id=company_id,
        mailbox_message_id=mailbox_message_id,
        sender_address=sender_address,
        status=status.value,
        thread_id=thread_id,
        original_rfc_message_id=original_rfc_message_id,
        ask_kind=None if ask_kind is None else ask_kind.value,
    )
    session.add(case)
    await session.flush()
    return case


async def get_case(
    session: AsyncSession, *, tenant_id: UUID, case_id: UUID
) -> AttributionCase | None:
    """One case by id, in its tenant — or ``None`` for an unknown id and
    another tenant's alike."""
    found: AttributionCase | None = await session.scalar(
        select(AttributionCase).where(
            AttributionCase.id == case_id,
            AttributionCase.tenant_id == tenant_id,
        )
    )
    return found


async def list_cases(
    session: AsyncSession, *, tenant_id: UUID, status: AttributionCaseStatus | None = None
) -> list[AttributionCase]:
    """The tenant's cases, newest first, optionally one status.

    ``status`` filters to one of the row's own statuses; ``None`` is every
    row (the surface's ``all``). Ordering is ``created_at`` descending with
    the id as the tie-break, so a list read is deterministic even for rows
    written inside one transaction.
    """
    statement = (
        select(AttributionCase)
        .where(AttributionCase.tenant_id == tenant_id)
        .order_by(AttributionCase.created_at.desc(), AttributionCase.id)
    )
    if status is not None:
        statement = statement.where(AttributionCase.status == status.value)
    return list((await session.scalars(statement)).all())


async def open_cases_for_thread(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    company_id: UUID,
    thread_id: str | None,
    rfc_message_ids: Sequence[str],
) -> list[AttributionCase]:
    """The company's open cases on a thread anchor — either anchor, oldest
    first.

    Both anchors are tried (ruling R1's two signals: the provider thread id
    and the RFC 5322 ``Message-ID`` a reply's ``In-Reply-To``/``References``
    carries), so a message that carries only one of them still finds the
    case. The id leg is a **set**: every token the caller's message carries
    is a candidate, so a case opened for an ancestor on the chain is found
    from any descendant — the IMAP leg's only chain evidence, because its
    ``thread_id`` is always ``None``. Only the two awaits are returned; the
    R5 confirmation authorises exactly the set this read reports.
    """
    anchor = _anchor_condition(thread_id=thread_id, rfc_message_ids=rfc_message_ids)
    if anchor is None:
        return []
    statement = (
        select(AttributionCase)
        .where(
            AttributionCase.tenant_id == tenant_id,
            AttributionCase.company_id == company_id,
            AttributionCase.status.in_(_OPEN_VALUES),
            anchor,
        )
        .order_by(AttributionCase.created_at, AttributionCase.id)
    )
    return list((await session.scalars(statement)).all())


async def latest_ask_for_thread(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    company_id: UUID,
    thread_id: str | None,
    rfc_message_ids: Sequence[str],
) -> AttributionCase | None:
    """The newest open case on the anchor whose ask has left — the R3
    suppression read.

    ``None`` means no open case on this anchor has had an ask, so an ask may
    go out; a row means one already has, and the caller suppresses this one
    (a suppressed case still opens with ``ask_sent_at`` NULL and stays visible
    to staff). The id leg is the caller's **ancestor set** — the tokens its
    message carries — so a second message on the chain meets the first case's
    ask even when no provider thread id exists to match on (the IMAP leg).
    Terminal cases are not asked about: a resolved, closed or dismissed case's
    ask is spent, and the next message on the thread is a new case with its
    own authorisation.
    """
    anchor = _anchor_condition(thread_id=thread_id, rfc_message_ids=rfc_message_ids)
    if anchor is None:
        return None
    statement = (
        select(AttributionCase)
        .where(
            AttributionCase.tenant_id == tenant_id,
            AttributionCase.company_id == company_id,
            AttributionCase.status.in_(_OPEN_VALUES),
            AttributionCase.ask_sent_at.isnot(None),
            anchor,
        )
        .order_by(AttributionCase.ask_sent_at.desc(), AttributionCase.id)
        .limit(1)
    )
    latest: AttributionCase | None = await session.scalar(statement)
    return latest


async def open_cases_for_anchor(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    thread_id: str | None,
    rfc_message_ids: Sequence[str],
) -> list[AttributionCase]:
    """The tenant's open cases on a thread anchor, whatever company they carry
    — the clarification loop's company-less read (ruling D2).

    ①'s ``open_cases_for_thread`` is company-scoped: the threaded cases belong
    to the chain's company. The clarification loop's cases open with no
    company at all, and both the answer read (a reply naming one of the
    sender's companies, ruling D6) and the B-class closure read (a registered
    channel's message on the anchor, ruling D8) must find them by the anchor
    alone. The id leg is the caller's ancestor set, as in the company-scoped
    read; only the three awaits are returned, oldest first.
    """
    anchor = _anchor_condition(thread_id=thread_id, rfc_message_ids=rfc_message_ids)
    if anchor is None:
        return []
    statement = (
        select(AttributionCase)
        .where(
            AttributionCase.tenant_id == tenant_id,
            AttributionCase.status.in_(_OPEN_VALUES),
            anchor,
        )
        .order_by(AttributionCase.created_at, AttributionCase.id)
    )
    return list((await session.scalars(statement)).all())


async def latest_ask_for_anchor(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    thread_id: str | None,
    rfc_message_ids: Sequence[str],
    exclude_case_id: UUID,
) -> AttributionCase | None:
    """The newest **other** open case on the anchor whose ask has left — the
    clarification loop's suppression read (ruling D7: one ask per (tenant,
    sender, anchor)).

    ``None`` means no other open case owns the anchor's ask, so this case may
    ask; a row means the anchor already has an open asked case and this one
    stays quiet. The caller's own case is excluded — its re-asks are bounded by
    ``count_asks_for_case`` (two per case), not by the one-ask-per-anchor rule,
    which governs cases, not messages. Company-less cases only in practice:
    this read exists for the clarification loop, and its company-bound cousins
    use ``latest_ask_for_thread``.
    """
    anchor = _anchor_condition(thread_id=thread_id, rfc_message_ids=rfc_message_ids)
    if anchor is None:
        return None
    statement = (
        select(AttributionCase)
        .where(
            AttributionCase.tenant_id == tenant_id,
            AttributionCase.status.in_(_OPEN_VALUES),
            AttributionCase.ask_sent_at.isnot(None),
            AttributionCase.id != exclude_case_id,
            anchor,
        )
        .order_by(AttributionCase.ask_sent_at.desc(), AttributionCase.id)
        .limit(1)
    )
    latest: AttributionCase | None = await session.scalar(statement)
    return latest


async def count_asks_for_case(
    session: AsyncSession, *, tenant_id: UUID, case_id: UUID
) -> int:
    """How many asks one case has issued — the two-ask bound's read (ruling
    D7: one ask, one re-ask, then never again).

    Counted as the case's ask outbox rows that were not cancelled: a row that
    reached ``sent`` or ``failed`` is an ask that left (or tried to), and a row
    still ``pending`` is an ask on its way — either way the bound must count
    it, while a ``cancelled`` row never carried anything out. The row's own
    anchor pins it to its tenant.
    """
    count = await session.scalar(
        select(func.count())
        .select_from(MailOutbox)
        .where(
            MailOutbox.tenant_id == tenant_id,
            MailOutbox.attribution_case_id == case_id,
            MailOutbox.kind == OutboxKind.ATTRIBUTION_ASK.value,
            MailOutbox.status != OutboxStatus.CANCELLED.value,
        )
    )
    return int(count or 0)


async def count_asks_since(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    since: datetime,
    company_less: bool | None = None,
) -> int:
    """How many asks left this tenant at or after ``since`` — the hourly caps'
    read (rulings R4/D7).

    Every case whose ``ask_sent_at`` falls in the window counts, whatever its
    status now is: the cap bounds the sending, and a case that closed since is
    still an ask that went out. ``company_less`` splits the two pools the two
    caps govern — ``False`` is ①'s authority asks (the company-bound cases),
    ``True`` is the clarification loop's convenience asks (the company-less
    ones), and ``None`` is both, the count's unpartitioned shape. ``since`` is
    a timezone-aware instant (the column is).
    """
    conditions: list[ColumnElement[bool]] = [
        AttributionCase.tenant_id == tenant_id,
        AttributionCase.ask_sent_at >= since,
    ]
    if company_less is True:
        conditions.append(AttributionCase.company_id.is_(None))
    elif company_less is False:
        conditions.append(AttributionCase.company_id.isnot(None))
    count = await session.scalar(
        select(func.count()).select_from(AttributionCase).where(*conditions)
    )
    return int(count or 0)


async def set_ask_sent(
    session: AsyncSession, *, tenant_id: UUID, case_id: UUID, at: datetime
) -> AttributionCase | None:
    """Stamp the instant the case's ask left, and return the row.

    Written in the same transaction as the outbox row the ask rides (ADR-0011
    §1), so the suppression read sees the ask the moment the enqueue exists.
    ``at`` is the caller's instant — the same one the ask's own rows are
    written with. ``None`` when the case is not in this tenant.
    """
    case = await get_case(session, tenant_id=tenant_id, case_id=case_id)
    if case is None:
        return None
    case.ask_sent_at = at
    await session.flush()
    return case


async def set_confirmation(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    case_id: UUID,
    communication_id: UUID | None,
) -> AttributionCase | None:
    """Pin the confirming exchange to the case, or clear the pin.

    Ruling R5's record: the registered channel's confirming reply is the
    authorising act, and this is the case's pointer at it — written while the
    case is still awaiting, because the confirmation authorises processing
    rather than being it (the resolve is ``set_status``'s). ``communication_id``
    is ``None`` when the §5.1 gate kept the confirming reply from being stored:
    the act is still recorded, but there is no exchange to name yet.
    ``None`` when the case is not in this tenant.
    """
    case = await get_case(session, tenant_id=tenant_id, case_id=case_id)
    if case is None:
        return None
    case.confirmation_communication_id = communication_id
    await session.flush()
    return case


async def set_status(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    case_id: UUID,
    status: AttributionCaseStatus,
    at: datetime | None = None,
    resolved_communication_id: UUID | None = None,
) -> AttributionCase | None:
    """Move one case to a new status, writing the stamps that status carries.

    ``closed`` and ``dismissed`` require ``at`` — the instant the case closed
    or staff decided it — because the CHECK pairs each with its own stamp
    (``closed_at``, ``dismissed_at``); a missing instant is refused here with
    a ``ValueError`` rather than left for the database to reject. Every status
    that is not one of the two closures clears both stamps, so the pairing
    cannot be violated by a move back. ``resolved_communication_id`` is
    written when the caller supplies it (R6's resolved case names the exchange
    the original became). ``None`` when the case is not in this tenant.
    """
    if status in (AttributionCaseStatus.CLOSED, AttributionCaseStatus.DISMISSED) and at is None:
        raise ValueError(f"{status.value} requires the instant it happened (at)")
    case = await get_case(session, tenant_id=tenant_id, case_id=case_id)
    if case is None:
        return None
    case.status = status.value
    case.closed_at = at if status is AttributionCaseStatus.CLOSED else None
    case.dismissed_at = at if status is AttributionCaseStatus.DISMISSED else None
    if resolved_communication_id is not None:
        case.resolved_communication_id = resolved_communication_id
    await session.flush()
    return case


async def set_case_company(
    session: AsyncSession, *, tenant_id: UUID, case_id: UUID, company_id: UUID
) -> AttributionCase | None:
    """Stamp the company an A-class answer named on its case (ruling D6).

    The clarification loop's cases open company-less and take their company at
    resolution: the answer grammar matched exactly one of the sender's
    companies, and this writes it — before the processing re-runs the
    ingestion tail, so the tail and everything it writes carry the company the
    answer chose. The composite foreign key pins the pair to this tenant the
    moment the value is non-null. ``None`` when the case is not in this tenant.
    """
    case = await get_case(session, tenant_id=tenant_id, case_id=case_id)
    if case is None:
        return None
    case.company_id = company_id
    await session.flush()
    return case
