"""The attribution-case review surface (ADR-0019 §1; contract §5.1.7's
sibling).

The registration/confirmation flow opens a case when a threaded message's
sender is not a registered channel of the chain's company
(``app.application.mail_ingest``), and this is the surface staff work from:
what is waiting, and the two actions that settle what the automated roads
have not — a dismissal (the case needs no further action) and a forced
processing (work the original through the ingestion tail by hand). The record
itself is ``attribution_cases`` (``app.adapters.persistence.attribution``,
migration ``0033``); the service half is ``app.application.attribution``.

Every route is Administrator-only (``require_admin``), the §2.5 quarantine
review's posture: the queue is the tenant-wide mailbox's own — the cases are
opened by the ingestion cycle, which is deployment plumbing of the same class
as the connection and the ingestion trigger — so settling one by hand is the
same seat's work.

**No route here carries mail content.** The row holds no subject and no body
— the unregistered sender's words are not read before the confirmation
(ruling R2) — and the entries below are the coordinates a reviewer triages
on: the company, the status, the ask kind and when it left, the sender and
the thread anchors, and the stamps each terminal state carries. The message
itself stays in the mailbox and in the records registry, reached through the
company's own routes.

The two actions answer the same way for the same situations, so neither can
be wrong about a case the other could have settled: 404 for an unknown id
**and** for another tenant's (deliberately one answer), and 409
``already_closed`` once the case has left the awaits — resolved, closed or
dismissed. The processing route adds one refusal of its own: 409
``attribution_case_has_no_company`` for an awaiting company-less case
(ruling D9 — staff cannot force-process what has no company; the road into
the ingestion tail is per company). The processing route's own failures are
two kinds: a refusal that changed nothing (the §5.1 gate's reason, an
unusable original, a ``fetch_failed:<reason>``) is an outcome in the
response — the case stays awaiting and staff can process it again — while
the mailbox's own connection failures are the typed 409/502s the ingestion
trigger reports them with, the same vocabulary whichever leg met them.
"""

from datetime import datetime
from enum import StrEnum
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.mail.errors import MailProviderRefused, MailProviderUnavailable
from app.adapters.persistence.attribution import OPEN_STATUSES
from app.adapters.persistence.models import AttributionCase
from app.adapters.persistence.session import get_session
from app.adapters.storage.s3 import S3Storage
from app.api.deps import ActorRef, get_current_actor, require_admin
from app.api.documents import get_storage
from app.api.mail import NOT_CONNECTED
from app.application import attribution, mail, scanning
from app.domain.attribution import AttributionCaseStatus
from app.domain.mail import MailResponseInvalid
from app.domain.rbac import ActorContext

router = APIRouter(prefix="/api/v1/mail/attribution-cases", tags=["mail"])


class AttributionCaseListFilter(StrEnum):
    """The list's ``status`` filter: the surface's own vocabulary.

    ``awaiting`` is **all three** open states at once — the queue staff work
    from is one queue, not two — and ``resolved``/``closed``/``dismissed`` are
    the three terminal states one at a time. ``all`` is not a status: it is the
    absence of the filter, which is why this vocabulary lives on the route and
    not in ``app.domain.attribution.AttributionCaseStatus``, the closed set
    the database checks. Being an enum is what makes an unknown value a 422: a
    filter nobody understands must not read as an empty queue.
    """

    AWAITING = "awaiting"
    RESOLVED = "resolved"
    CLOSED = "closed"
    DISMISSED = "dismissed"
    ALL = "all"

    @property
    def statuses(self) -> tuple[AttributionCaseStatus, ...] | None:
        """The row statuses this filter selects, or ``None`` for every row."""
        if self is AttributionCaseListFilter.ALL:
            return None
        if self is AttributionCaseListFilter.AWAITING:
            return OPEN_STATUSES
        return (AttributionCaseStatus(self.value),)


class AttributionCaseEntryResponse(BaseModel):
    """One attribution case as staff read it — never anything of the mail.

    ``status`` is the row's own: one of the three awaits while the case
    waits, or the terminal state it settled in. ``company_id`` is ``null``
    for the clarification loop's company-less cases (they take their company
    at resolution or closure; ruling D2). ``ask_kind``/``ask_sent_at`` are
    the ask side — the kind the decision chose, and when it left (null while
    an ask is still owed, or was suppressed; ruling R3's
    visible-but-unasked posture). The two anchors are how a reply is matched
    to the case, and the three settlement columns say how it ended:
    ``resolved`` with the exchange the original became, ``closed`` with the
    instant the remedy arrived, ``dismissed`` with the instant staff decided.
    There is deliberately no field that could carry what the sender wrote,
    and the table behind the row holds none either.
    """

    id: UUID
    # NULL for a company-less case (the clarification loop's A/B classes,
    # migration 0034) — D9's list shape: the row names its company only when
    # it has one.
    company_id: UUID | None
    status: str
    ask_kind: str | None
    ask_sent_at: datetime | None
    sender_address: str
    thread_id: str | None
    original_rfc_message_id: str | None
    resolved_communication_id: UUID | None
    closed_at: datetime | None
    dismissed_at: datetime | None
    created_at: datetime


class AttributionCaseProcessResponse(BaseModel):
    """One forced processing's account of itself: the case, and the verdict.

    ``outcome`` is the ingestion tail's own verdict for the original
    (``ingested | skipped_known | denied | failed``), never a message's
    content. ``resolved`` is true exactly when a ``Communication`` for the
    original exists: the case moved to ``resolved`` with ``communication_id``
    behind it, and its queued ask was cancelled. A refusal — the gate's own
    reason, an unusable original, ``fetch_failed:<reason>`` — answers
    ``resolved`` false with the reason and leaves the case in its await
    (``status``), ready to be processed again.
    """

    case_id: UUID
    # The report's own: NULL while the processed case was company-less.
    company_id: UUID | None
    status: str
    outcome: str
    resolved: bool
    reason: str | None
    communication_id: UUID | None


def _entry(row: AttributionCase) -> AttributionCaseEntryResponse:
    return AttributionCaseEntryResponse(
        id=row.id,
        company_id=row.company_id,
        status=row.status,
        ask_kind=row.ask_kind,
        ask_sent_at=row.ask_sent_at,
        sender_address=row.sender_address,
        thread_id=row.thread_id,
        original_rfc_message_id=row.original_rfc_message_id,
        resolved_communication_id=row.resolved_communication_id,
        closed_at=row.closed_at,
        dismissed_at=row.dismissed_at,
        created_at=row.created_at,
    )


@router.get("")
async def list_attribution_cases(
    actor: Annotated[ActorRef, Depends(get_current_actor)],
    _: Annotated[ActorContext, Depends(require_admin())],
    session: Annotated[AsyncSession, Depends(get_session)],
    status: Annotated[AttributionCaseListFilter, Query()] = AttributionCaseListFilter.AWAITING,
) -> list[AttributionCaseEntryResponse]:
    """The tenant's cases, newest first.

    Metadata only, and the tenant is the actor's — never a parameter.

    **``status`` defaults to ``awaiting``** because this list is the queue
    staff work from, and a queue that also holds everything already settled
    is one people stop reading: the cases they still have to act on have to
    be what they see first. ``awaiting`` is all three open states at once — a
    case waiting for its instruction, one waiting for a confirmation, and one
    waiting for the client to say which company, are all owed staff attention —
    while ``resolved``/``closed``/``dismissed`` are how a reviewer looks at
    one end state on its own, and ``all`` at the history. An unknown value is
    a 422 from the closed vocabulary above — never an empty list, which would
    read as "nothing is waiting".
    """
    rows = await attribution.list_cases(
        session, tenant_id=actor.tenant_id, statuses=status.statuses
    )
    return [_entry(row) for row in rows]


@router.post("/{id}/dismiss")
async def dismiss_attribution_case(
    id: UUID,
    actor: Annotated[ActorRef, Depends(get_current_actor)],
    _: Annotated[ActorContext, Depends(require_admin())],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> AttributionCaseEntryResponse:
    """Close one open case that staff have decided needs no further action.

    The case this exists for: the message is never answered — the sender does
    not have a registered channel, or the client simply ignores the ask — and
    without a dismissal the row would sit in the staff queue for good. Nothing
    is read from the mailbox and nothing is stored: the dismissal is a
    statement about the row, recorded as ``dismissed`` with the instant and
    one ``attribution_case_dismissed`` entry, and the case's queued ask — when
    one is still waiting to deliver — is cancelled with it, because a letter
    asking for something nobody will answer must not reach the client after
    staff closed the book on it.

    404 for an unknown id **and** for another tenant's — deliberately one
    answer. 409 ``already_closed`` once the case has left the awaits —
    resolved, closed or dismissed — because which way it settled is the row's
    own fact and reopening it would be a second decision over the first one's
    entry. The read takes the row lock, so an overlapping dismissal loses the
    race and is refused rather than appending a second entry to the audit
    chain.
    """
    try:
        row = await attribution.dismiss_case(
            session,
            tenant_id=actor.tenant_id,
            case_id=id,
            actor_user_id=actor.user_id,
        )
    except attribution.AttributionCaseNotFoundError:
        raise HTTPException(status_code=404, detail="attribution case not found") from None
    except attribution.AttributionCaseNotOpenError:
        raise HTTPException(status_code=409, detail="already_closed") from None
    return _entry(row)


@router.post("/{id}/process")
async def process_attribution_case(
    id: UUID,
    actor: Annotated[ActorRef, Depends(get_current_actor)],
    _: Annotated[ActorContext, Depends(require_admin())],
    session: Annotated[AsyncSession, Depends(get_session)],
    store: Annotated[S3Storage, Depends(get_storage)],
) -> AttributionCaseProcessResponse:
    """Process one open case's original message, now, at staff's press.

    The tail the confirmation authorises (``process_case``), reached by the
    case's id: the original is read from the mailbox again, scanned, stored
    through the records registry and recorded as a ``Communication`` for the
    case's own company — and a success resolves the case with that exchange
    and cancels its queued ask, one ``attribution_case_resolved`` entry
    naming the case, the exchange and the acting member. This is the road
    back for a case the automated path could not take: a confirmation whose
    §5.1 gate denied the storage, or a fetching that failed, leaves the case
    awaiting for exactly this press.

    A refusal that changed nothing is an outcome in the response, not a
    status code: the gate's own bounded reason, ``invalid_message:<reason>``
    when the bytes are not a usable message, a mailbox that answered and
    could not produce this message (``fetch_failed:<reason>``) all answer
    ``resolved`` false with the case still awaiting — one unreachable message
    must not cost the case that authorised it. The mailbox's connection
    failures are the ingestion trigger's typed refusals instead: 409
    ``not_connected`` with no mailbox to read, and the provider 502s —
    ``mail_refresh_failed:<reason>`` when the grant is dead,
    ``mail_read_refused:<reason>`` when it refuses a read, ``mail_unavailable``
    when it cannot be reached, ``invalid_response:<reason>`` when an answer is
    not the shape the wire contract fixes.

    404 for an unknown id **and** for another tenant's — one answer — and 409
    ``already_closed`` once the case has left the awaits, raised before the
    mailbox is touched. An awaiting **company-less** case is refused instead
    with 409 ``attribution_case_has_no_company`` (ruling D9): staff cannot
    force-process what has no company — the road into the ingestion tail is
    per company, and the company-less cases' roads are the asks and answers
    the cycle drives. The read takes the row lock, so an overlapping
    processing loses the race and is refused rather than appending a second
    entry to the audit chain.
    """
    try:
        result = await attribution.process_open_case(
            session,
            store,
            scanning.scanner(),
            tenant_id=actor.tenant_id,
            case_id=id,
            actor_user_id=actor.user_id,
        )
    except attribution.AttributionCaseNotFoundError:
        raise HTTPException(status_code=404, detail="attribution case not found") from None
    except attribution.AttributionCaseNotOpenError:
        raise HTTPException(status_code=409, detail="already_closed") from None
    except attribution.AttributionCaseHasNoCompanyError:
        raise HTTPException(
            status_code=409, detail="attribution_case_has_no_company"
        ) from None
    except mail.MailNotConnected:
        raise HTTPException(status_code=409, detail=NOT_CONNECTED) from None
    except mail.MailRefreshFailed as exc:
        raise HTTPException(status_code=502, detail=f"mail_refresh_failed:{exc.reason}") from None
    except MailProviderRefused as exc:
        raise HTTPException(status_code=502, detail=f"mail_read_refused:{exc.reason}") from None
    except MailProviderUnavailable:
        raise HTTPException(status_code=502, detail="mail_unavailable") from None
    except MailResponseInvalid as exc:
        raise HTTPException(status_code=502, detail=f"invalid_response:{exc.reason}") from None
    return AttributionCaseProcessResponse(
        case_id=result.case.id,
        company_id=result.case.company_id,
        status=result.case.status,
        outcome=result.report.outcome.value,
        resolved=result.report.resolved,
        reason=result.report.reason,
        communication_id=result.report.communication_id,
    )
