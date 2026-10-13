"""Confirmation-request endpoints (scope §2.5, §7; contract §5.1.8).

``POST /companies/{id}/confirmation-requests`` drafts one confirmation
request for a company and form: the master data resolved on the day, the
bilingual message composed and stored as a WORM ``message/rfc822`` document,
and the request row that carries the checklist. It is **gated automation** —
the trigger consults the single automation-gating seam with
``AutomationType.CONFIRMATION_GENERATION`` and fails closed, surfacing the
gate's own reason (scope §2.15 lists confirmation requests first among the
actions that run only for a covered company).

**The send half (M2).** ``POST …/confirmation-requests/{id}/send`` is the
staff release the founder's ruling names: the request moves to ``queued``, one
outbox row records the intent, and the message is delivered from there — the
release gate first (a refusal is a 409 with the gate's own detail and the
queue entry keeps waiting), then the provider. The delivery is the same code
the background worker runs, so a request sent by hand and one released
automatically cannot diverge. **``automatic`` is the generator's release**: a
draft the trigger writes under that mode is already queued when it returns,
and the background loop (or ``POST /mail/send``) delivers it.

``GET /companies/{id}/confirmation-requests`` is the company's own list,
newest draft first: id, form, dates, status, version, recipients, checklist
and the id of the stored message. **Never the message body** — it is in the
records registry and is fetched through the existing documents route, the
same metadata-only discipline as the inbound-mail read.

Both routes are grant-gated via ``require_company_access`` (the §2.16
permission flags cover review and approve; drafting a request is ordinary
company-scoped staff work), and the read is **ungated** by the automation
gate: a lapsed licence must not lock the customer out of what the deployment
already drafted for them (ADR-0012).
"""

from datetime import date, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.persistence.confirmation import ConfirmationRequest
from app.adapters.persistence.session import get_session
from app.adapters.storage.s3 import S3Storage
from app.api.deps import ActorRef, get_current_actor, require_company_access
from app.api.documents import get_storage
from app.api.tenant_config import invalid_stored_config
from app.application import confirmation, mail_send, outbox, scanning
from app.application.company_master import CompanyNotFoundError
from app.domain.compliance import DueDateRuleNotEstablishedError
from app.domain.confirmation import (
    CompanyNameUnavailableError,
    NoClientReplyWindowError,
    UnsupportedFormError,
)
from app.domain.milestones import MilestoneValidationError
from app.domain.prior_filing import FormCode
from app.domain.rbac import ActorContext
from app.domain.send_modes import SendModesValidationError

router = APIRouter(prefix="/api/v1/companies", tags=["confirmation-requests"])


class GenerateConfirmationRequest(BaseModel):
    """Which form to confirm, for which made-up date, and to whom.

    ``target_date`` is required: the reply-by deadline is derived from it, and
    a request with no deadline gives the client nothing to reply by.
    ``contact_person_id`` narrows the recipients to one associated person;
    without it every associated person's email channel is addressed.
    """

    form_type: FormCode
    target_date: date
    contact_person_id: UUID | None = None


class RecipientResponse(BaseModel):
    """One addressee: the external contact and the channel it goes to."""

    person_id: UUID
    address: str


class ChecklistItemResponse(BaseModel):
    """One question of the request, with the value it is asked against.

    ``expected_value`` is ``null`` for a fact the product holds nothing for
    (the client is asked to supply it), and ``source_record_id`` is the
    effective-dated row the value came from.
    """

    fact_id: str
    question: str
    expected_value: str | None
    source_record_id: UUID | None


class ConfirmationRequestResponse(BaseModel):
    """One drafted request — its parameters and its checklist, never its body.

    ``reply_by`` is the date the client is given (the tenant's milestone panel
    applied to the period) and ``statutory_due`` the CR deadline it was guarded
    against; the latter is ``null`` on rows written before the panel existed,
    which were generated under the old rule rather than invented a guard.
    """

    id: UUID
    company_id: UUID
    form_type: str
    target_date: date
    reply_by: date
    statutory_due: date | None
    status: str
    version_no: int
    document_id: UUID
    recipients: list[RecipientResponse]
    checklist: list[ChecklistItemResponse]
    created_by: UUID | None
    created_at: datetime


def _request_response(row: ConfirmationRequest) -> ConfirmationRequestResponse:
    return ConfirmationRequestResponse(
        id=row.id,
        company_id=row.company_id,
        form_type=row.form_type,
        target_date=row.target_date,
        reply_by=row.reply_by,
        statutory_due=row.statutory_due,
        status=row.status,
        version_no=row.version_no,
        document_id=row.document_id,
        recipients=[RecipientResponse(**recipient) for recipient in row.recipients],
        checklist=[ChecklistItemResponse(**item) for item in row.checklist],
        created_by=row.created_by,
        created_at=row.created_at,
    )


@router.post("/{company_id}/confirmation-requests", status_code=201)
async def generate_confirmation_request(
    company_id: UUID,
    payload: GenerateConfirmationRequest,
    actor: Annotated[ActorRef, Depends(get_current_actor)],
    _: Annotated[ActorContext, Depends(require_company_access())],
    session: Annotated[AsyncSession, Depends(get_session)],
    store: Annotated[S3Storage, Depends(get_storage)],
) -> ConfirmationRequestResponse:
    """Draft, store, and record one confirmation request (gated).

    The date the client is asked to reply by comes from the tenant's milestone
    panel (`GET`/`PUT /tenant-config`), counted back from the company's
    anniversary — never CR's own deadline, which stays as margin.

    **Nothing about the period's renewal is consulted** (ADR-0017 as amended
    2026-09-24): drafting and preparing a document are not gated, so this
    trigger refuses nothing for a period the client has not answered — the
    gate sits on the release, at the download of the stored letter. What it
    does refuse is the §5.1 automation gate's own reasons.

    403 ``automation_denied: <reason>`` when confirmation generation is not
    licensed for the company (tier capability, assignment, coverage, or the
    package's services) — and a denial writes nothing at all. 404 for an
    unknown or cross-tenant company; 422 ``unsupported_form`` for a form this
    slice cannot confirm, ``nar1_rule_not_established`` when the company's
    type has no NAR1 rule to guard the reply-by against (the date is never
    guessed), ``no_client_reply_window`` when the milestone panel would put the
    client's reply-by on or after CR's deadline or leave them less than their
    minimum reply window, ``company_name_unavailable`` when no name is in force
    (an unnamed company cannot be identified), ``no_client_contact`` when the
    company has no email contact, and ``client_contact_not_found`` when a
    named contact cannot receive; 409 ``draft_conflict`` when another
    generation for the same company and form committed first (the caller
    re-reads and retries — the retry supersedes it); 503
    ``mail_not_configured`` when the deployment has no mailbox address to
    write from.
    """
    try:
        row = await confirmation.generate(
            session,
            store,
            scanning.scanner(),
            tenant_id=actor.tenant_id,
            company_id=company_id,
            actor_user_id=actor.user_id,
            form_type=payload.form_type,
            target_date=payload.target_date,
            as_of=date.today(),
            contact_person_id=payload.contact_person_id,
        )
    except CompanyNotFoundError:
        raise HTTPException(status_code=404, detail="company not found") from None
    except confirmation.AutomationDeniedError as exc:
        raise HTTPException(
            status_code=403, detail=f"automation_denied: {exc.reason.value}"
        ) from None
    except confirmation.MailNotConfiguredError:
        raise HTTPException(status_code=503, detail="mail_not_configured") from None
    except confirmation.NoClientContactError:
        raise HTTPException(status_code=422, detail="no_client_contact") from None
    except confirmation.ClientContactNotFoundError:
        raise HTTPException(status_code=422, detail="client_contact_not_found") from None
    except confirmation.DraftConflictError:
        raise HTTPException(status_code=409, detail="draft_conflict") from None
    except UnsupportedFormError:
        raise HTTPException(status_code=422, detail="unsupported_form") from None
    except DueDateRuleNotEstablishedError:
        raise HTTPException(status_code=422, detail="nar1_rule_not_established") from None
    except NoClientReplyWindowError:
        raise HTTPException(status_code=422, detail="no_client_reply_window") from None
    except CompanyNameUnavailableError:
        raise HTTPException(status_code=422, detail="company_name_unavailable") from None
    except MilestoneValidationError as exc:
        raise invalid_stored_config(exc) from None
    except SendModesValidationError as exc:
        # The send-mode block decides whether this draft is released the moment
        # it is written, so an unreadable one refuses the trigger — the same
        # 409 ``tenant_config_invalid`` the milestone block answers, and for the
        # same reason: the read that derives from the block is the read that
        # cannot proceed on a block nobody can parse.
        raise invalid_stored_config(exc) from None
    return _request_response(row)


@router.get("/{company_id}/confirmation-requests")
async def list_confirmation_requests(
    company_id: UUID,
    actor: Annotated[ActorRef, Depends(get_current_actor)],
    _: Annotated[ActorContext, Depends(require_company_access())],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> list[ConfirmationRequestResponse]:
    """The company's confirmation requests, newest draft first.

    Ungated by the automation gate (ADR-0012): what the deployment already
    drafted is the customer's own record, so the read keeps working on a
    lapsed licence. 404 for an unknown or cross-tenant company.
    """
    try:
        rows = await confirmation.list_requests(
            session, tenant_id=actor.tenant_id, company_id=company_id
        )
    except CompanyNotFoundError:
        raise HTTPException(status_code=404, detail="company not found") from None
    return [_request_response(row) for row in rows]


@router.post("/{company_id}/confirmation-requests/{request_id}/send")
async def send_confirmation_request(
    company_id: UUID,
    request_id: UUID,
    actor: Annotated[ActorRef, Depends(get_current_actor)],
    _: Annotated[ActorContext, Depends(require_company_access())],
    session: Annotated[AsyncSession, Depends(get_session)],
    store: Annotated[S3Storage, Depends(get_storage)],
) -> ConfirmationRequestResponse:
    """Release one confirmation request and deliver its stored message.

    The staff half of the founder's send-mode ruling: a request held for
    review is released here, and the delivery runs the worker's own per-row
    order — the release gate before the message leaves, then the provider.
    Idempotent in the way that matters: a second call on a request whose
    message has gone out delivers the request's held reminder letters instead
    (§5.1.11's 2026-09-29 extension) and answers **409 ``already_sent``** only
    when nothing is held — never a second copy of anything — and a call on one
    already released retries its pending send instead of recording a second
    release.

    The response's ``status`` is ``sent`` when the provider accepted the
    message (or Sent already held it — the reconcile), and ``queued`` when the
    release stands and the delivery has not happened yet: the gate refused
    (409, with its own detail), no mailbox is configured (503), or the provider
    failed (502 — transient, or ``mail_unconfirmed`` when the outcome is
    genuinely unknown). Nothing is lost in any of those: the queue entry keeps
    the reason and the next cycle tries again.

    404 for an unknown or cross-tenant request; 409 ``already_sent`` /
    ``request_superseded`` for a request that cannot be released again — with
    the 2026-09-29 extension §5.1.11's row records: a press on a request
    already ``sent`` releases the request's held follow-up letters (the
    sweep's ``review_required`` reminder rows and the reconfirmer's
    ``review_required`` reconfirmation rows — both park on
    ``HELD_NOT_BEFORE``, and the release path is one mechanism for both
    kinds) and delivers each one, answering ``409 already_sent`` only when
    nothing is held, so a manual release stays legal in either mode; 409
    with the release gate's own detail when the period or the coverage refuses
    (``service_not_in_scope: …``, ``continuation_not_recorded: <year>``,
    ``payment_not_received: <year>`` — request rows only; a follow-up row
    never consults the release gate, §5.1.12); 409 ``document_not_available:<status>``;
    409 ``document_missing`` (the letter's document row is gone) and 409
    ``not_pending`` (another delivery raced this press and had the row — the
    sender's locked re-read, §5.1.11); 503 ``mail_not_configured``; 502
    ``mail_refresh_failed:<code>``,
    ``mail_read_refused:<code>``, ``mail_unavailable``,
    ``invalid_response:<reason>``, ``mail_send_deferred:<code>``,
    ``mail_send_failed:<code>`` or ``mail_send_unconfirmed``.

    **The stored send-mode block is deliberately not read here.** A manual
    release is legal in either mode — the mode decides whether the *generator*
    releases a client draft by itself — so a block nobody can parse has no
    bearing on this route, and the generation trigger is the block's only
    reader.
    """
    try:
        released = await outbox.release(
            session,
            tenant_id=actor.tenant_id,
            company_id=company_id,
            request_id=request_id,
            actor_user_id=actor.user_id,
        )
    except outbox.RequestNotFoundError:
        raise HTTPException(status_code=404, detail="confirmation request not found") from None
    except outbox.RequestNotSendableError as exc:
        if exc.reason != "already_sent":
            raise HTTPException(status_code=409, detail=exc.reason) from None
        return await _release_and_deliver_reminders(
            session,
            store,
            tenant_id=actor.tenant_id,
            company_id=company_id,
            request_id=request_id,
            actor_user_id=actor.user_id,
        )
    # ADR-0011 §1: the intent is durable **before** anything leaves the
    # deployment, so the delivery below happens on a session whose release has
    # already been committed. A crash between the two leaves a queued request
    # and a pending row, which the next cycle delivers — never a lost release.
    await session.commit()
    outcome = await mail_send.deliver_one(session, store, job=released.job)
    # The outcome (and, when the gate refused, its entry) is committed before
    # the answer — §5.1.9's own discipline: a refusal rolled back at the API
    # edge would leave no trace at all.
    await session.commit()
    if outcome.disposition is not mail_send.SendDisposition.SENT:
        raise _send_refusal(outcome)
    return _request_response(released.request)


async def _release_and_deliver_reminders(
    session: AsyncSession,
    store: S3Storage,
    *,
    tenant_id: UUID,
    company_id: UUID,
    request_id: UUID,
    actor_user_id: UUID | None,
) -> ConfirmationRequestResponse:
    """The send route's follow-up half (§5.1.11, extended 2026-09-29; widened
    by slice M5b): the press on a ``sent`` request moves its held follow-up
    rows — the reminder letters the sweep parked and the reconfirmation letter
    the reconfirmer parked — to the press instant and delivers each released
    row with the sender's own per-row order. One release path serves both
    kinds (``outbox.release_reminders`` is the mechanism, whatever the kind).

    Nothing held is exactly the idempotence refusal the route has always
    answered — a second press after the follow-ups went out is ``409
    already_sent``, not a second email. The commit discipline mirrors the
    single-row path: the release is durable before anything leaves, and each
    row's outcome (and, for a refusal, its entry) commits before the next row
    starts.
    """
    request = await outbox.get_request(
        session, tenant_id=tenant_id, company_id=company_id, request_id=request_id
    )
    released = await outbox.release_reminders(
        session,
        tenant_id=tenant_id,
        company_id=company_id,
        request_id=request_id,
        actor_user_id=actor_user_id,
    )
    if not released:
        raise HTTPException(status_code=409, detail="already_sent")
    await session.commit()
    for job in released:
        outcome = await mail_send.deliver_one(session, store, job=job)
        await session.commit()
        if outcome.disposition is not mail_send.SendDisposition.SENT:
            raise _send_refusal(outcome)
    return _request_response(request)


def _send_refusal(outcome: mail_send.SendOutcome) -> HTTPException:
    """The wire shape of a delivery that did not send.

    One mapping, documented in the route above: the detail names the reason
    (the gate's own detail string for a refusal, a bounded code otherwise) and
    the status names the class — 409 when the request or the release refused,
    503 when the deployment has no mailbox configured at all, and 502 when the
    provider is the problem. The two guard-only outcomes are 409 too, not the
    provider's 502 (§5.1.3): ``not_pending`` (the row left ``pending`` between
    this press's release and its delivery — the sender's locked re-read, so
    another delivery had it) and ``document_missing`` (the letter's document
    row is gone) are facts about the request's own state, reachable only for an
    impossible or raced one, and neither names a provider failure.
    ``document_not_available`` keeps its §5.1.3
    meaning and status.
    """
    detail = outcome.reason or "mail_send_unavailable"
    if outcome.disposition is mail_send.SendDisposition.CANCELLED:
        return HTTPException(status_code=409, detail=detail)
    if outcome.disposition is mail_send.SendDisposition.REFUSED:
        return HTTPException(status_code=409, detail=detail)
    if detail == mail_send.REASON_NOT_CONFIGURED:
        return HTTPException(status_code=503, detail=detail)
    if detail.startswith("document_not_available"):
        return HTTPException(status_code=409, detail=detail)
    if detail in ("document_missing", "not_pending"):
        return HTTPException(status_code=409, detail=detail)
    return HTTPException(status_code=502, detail=detail)
