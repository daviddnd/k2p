"""The §2.5 quarantine review surface (scope §2.5; contract §5.1.7).

Scope §2.5 quarantines a reply from an unregistered address "for staff
review", and this is that review: what is waiting, and the two actions that
move it — a retry (work the message again through the decision that refused
it) and a dismiss (close it because no further action is needed). The record
itself is ``quarantined_messages``
(``app.adapters.persistence.quarantine``), written by the ingestion cycle when
a message cannot be attributed to exactly one company
(``app.application.mail_ingest``); the service half is
``app.application.quarantine``.

Every route is Administrator-only (``require_admin``): a quarantined message
has **no company** — that is why it is quarantined — so there is no per-company
grant for a General account to hold, and the tenant-wide mailbox is deployment
plumbing of the same class as the connection and the ingestion trigger.

**No route here carries mail content.** The list returns the sender address,
the provider's receipt time, the reason and the status — the metadata a
reviewer triages on, and the record holds nothing else; the message stays in
the mailbox (ADR-0005). The retry's report is the ingestion cycle's own
ids-and-outcomes shape (``app.api.mail``): the message is re-examined through
the order that produced it, its words are read from the provider when the
decision needs them, and a message a previous cycle already took is resolved
against the record it already has without touching the mailbox at all. That
report also carries the resolver's decision — the case, the evidence tier,
the score, the band it routes to and the candidate company ids — so staff
pressing retry are told what the rule saw, not only what it could not do; the
text any candidate matched on stays in the §2.7 assessment.
"""

from datetime import datetime
from enum import StrEnum
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.mail.errors import MailProviderRefused, MailProviderUnavailable
from app.adapters.persistence.quarantine import QuarantinedMessage
from app.adapters.persistence.session import get_session
from app.adapters.storage.s3 import S3Storage
from app.api.deps import ActorRef, get_current_actor, require_admin
from app.api.documents import get_storage
from app.api.mail import NOT_CONNECTED, MessageOutcomeResponse, message_outcome_response
from app.application import mail, quarantine, scanning
from app.domain.mail import MailResponseInvalid
from app.domain.quarantine import QuarantineStatus
from app.domain.rbac import ActorContext

router = APIRouter(prefix="/api/v1/mail/quarantine", tags=["mail"])


class QuarantineListFilter(StrEnum):
    """The list's ``status`` filter: the row's own statuses, plus ``all``.

    ``all`` is not a status — it is the absence of the filter — which is why
    this vocabulary lives on the route and not in
    ``app.domain.quarantine.QuarantineStatus``, the closed set the database
    checks. Being an enum is what makes an unknown value a 422: a filter
    nobody understands must not read as an empty queue.
    """

    PENDING = "pending"
    RESOLVED = "resolved"
    DISMISSED = "dismissed"
    ALL = "all"

    @property
    def status(self) -> QuarantineStatus | None:
        """The row status this filter selects, or ``None`` for every row."""
        if self is QuarantineListFilter.ALL:
            return None
        return QuarantineStatus(self.value)


class QuarantineEntryResponse(BaseModel):
    """One quarantined message as the reviewer reads it — never its content.

    ``status`` is the row's own — ``pending`` while it waits, or the terminal
    state it closed in: ``resolved`` with a ``communication_id`` behind it, or
    ``dismissed`` with the time staff closed it and nothing else.
    ``communication_id`` is the row's ``resolved_communication_id``, null
    while the row is still pending and always null for a dismissed one. There
    is deliberately no subject and no body here, and none in the table behind
    it.
    """

    id: UUID
    mailbox_message_id: str
    thread_id: str | None
    sender_address: str
    received_at: datetime
    reason: str
    status: str
    resolved_at: datetime | None
    dismissed_at: datetime | None
    communication_id: UUID | None


class QuarantineRetryResponse(BaseModel):
    """One retry's account of itself: the row as it stands, and the outcome.

    ``outcome`` is the ingestion cycle's per-message shape, so an attempt that
    ingested reports the company, the ``Communication``, the stored documents
    and their scan outcomes, and one that could not attribute the message
    reports the **current** reason — the decision made now, which need not be
    the row's stored ``reason``. **Since ADR-0019 §1 the outcome may instead
    be one of the two ``awaiting_*`` values, with the ``attribution_case_id``
    the attempt opened** — the message now sits on a thread bound to a company
    its sender is not a registered channel of — and the row stays ``pending``:
    the case, not the quarantine queue, is what that message awaits, and the
    dismissal is the row's way out.
    """

    quarantine_id: UUID
    status: str
    resolved_at: datetime | None
    communication_id: UUID | None
    outcome: MessageOutcomeResponse


def _entry(row: QuarantinedMessage) -> QuarantineEntryResponse:
    return QuarantineEntryResponse(
        id=row.id,
        mailbox_message_id=row.mailbox_message_id,
        thread_id=row.thread_id,
        sender_address=row.sender_address,
        received_at=row.received_at,
        reason=row.reason,
        status=row.status,
        resolved_at=row.resolved_at,
        dismissed_at=row.dismissed_at,
        communication_id=row.resolved_communication_id,
    )


@router.get("")
async def list_quarantine(
    actor: Annotated[ActorRef, Depends(get_current_actor)],
    _: Annotated[ActorContext, Depends(require_admin())],
    session: Annotated[AsyncSession, Depends(get_session)],
    status: Annotated[QuarantineListFilter, Query()] = QuarantineListFilter.PENDING,
) -> list[QuarantineEntryResponse]:
    """The tenant's quarantined messages, newest receipt first.

    Metadata only, and the tenant is the actor's — never a parameter.

    **``status`` defaults to ``pending``** because this list is the queue
    staff work from, and a queue that also holds everything already closed is
    one people stop reading: the rows they still have to act on have to be
    what they see first. ``status=all`` is there for a reviewer checking the
    history, and ``resolved``/``dismissed`` for looking at one closed state
    on its own. An unknown value is a 422 from the closed vocabulary above —
    never an empty list, which would read as "nothing is waiting".
    """
    rows = await quarantine.list_quarantined(
        session, tenant_id=actor.tenant_id, status=status.status
    )
    return [_entry(row) for row in rows]


@router.post("/{id}/retry")
async def retry_quarantined_message(
    id: UUID,
    actor: Annotated[ActorRef, Depends(get_current_actor)],
    _: Annotated[ActorContext, Depends(require_admin())],
    session: Annotated[AsyncSession, Depends(get_session)],
    store: Annotated[S3Storage, Depends(get_storage)],
) -> QuarantineRetryResponse:
    """Re-examine one quarantined message as the cycle would, now.

    No company id is taken, deliberately: this re-runs the same deterministic
    decision (scope §2.5/§7), so a message that still cannot be attributed
    answers honestly and stays pending. The staff remedy is then to register
    the sender's contact channel (``/persons`` and ``/companies/{id}/contacts``)
    or record the company the message names, and retry.

    404 for an unknown id **and** for one belonging to another tenant — the
    two are deliberately one answer. 409 ``already_resolved`` or
    ``already_dismissed`` when the row has already been closed — which of the
    two says how it closed — including the overlapping-retry case: the loser
    of the race is refused, not served twice. 409 ``not_connected`` when the
    deployment has no mailbox to read (no connected credential and no
    configured IMAP settings). 502 for the provider failures the ingestion
    trigger reports the same way — including ``mail_read_refused:not_found``
    for a message the mailbox no longer holds, where the dismissal is the
    remedy.
    """
    try:
        result = await quarantine.retry(
            session,
            store,
            scanning.scanner(),
            tenant_id=actor.tenant_id,
            quarantine_id=id,
            actor_user_id=actor.user_id,
        )
    except quarantine.QuarantineNotFoundError:
        raise HTTPException(status_code=404, detail="quarantine not found") from None
    except quarantine.QuarantineAlreadyResolvedError:
        raise HTTPException(status_code=409, detail="already_resolved") from None
    except quarantine.QuarantineAlreadyDismissedError:
        raise HTTPException(status_code=409, detail="already_dismissed") from None
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
    return QuarantineRetryResponse(
        quarantine_id=result.row.id,
        status=result.row.status,
        resolved_at=result.row.resolved_at,
        communication_id=result.row.resolved_communication_id,
        outcome=message_outcome_response(result.outcome),
    )


@router.post("/{id}/dismiss")
async def dismiss_quarantined_message(
    id: UUID,
    actor: Annotated[ActorRef, Depends(get_current_actor)],
    _: Annotated[ActorContext, Depends(require_admin())],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> QuarantineEntryResponse:
    """Close one pending row that staff have decided needs no further action.

    The row a retry cannot close: when the client deleted the mail, the retry
    meets the provider's not-found refusal for ever, and without a dismissal
    the message would sit in the queue for good. Nothing is read from the
    mailbox and nothing is stored — the dismissal is a statement about the
    row, recorded with ``dismissed_at`` and one audit entry
    (``mail_quarantine_dismissed``), not about the mail.

    Only a ``pending`` row can be dismissed. 404 for an unknown id **and** for
    one belonging to another tenant — deliberately one answer. 409
    ``already_resolved`` when the message was attributed after all (that row
    has a ``Communication`` and is not dismissed over it), and 409
    ``already_dismissed`` when staff already closed it. The read takes the row
    lock, so an overlapping dismissal loses the race and is refused rather
    than appending a second entry to the audit chain.
    """
    try:
        row = await quarantine.dismiss(
            session,
            tenant_id=actor.tenant_id,
            quarantine_id=id,
            actor_user_id=actor.user_id,
        )
    except quarantine.QuarantineNotFoundError:
        raise HTTPException(status_code=404, detail="quarantine not found") from None
    except quarantine.QuarantineAlreadyResolvedError:
        raise HTTPException(status_code=409, detail="already_resolved") from None
    except quarantine.QuarantineAlreadyDismissedError:
        raise HTTPException(status_code=409, detail="already_dismissed") from None
    return _entry(row)
