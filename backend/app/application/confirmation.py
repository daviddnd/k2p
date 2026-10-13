"""Confirmation-request orchestration (scope §2.5, §7; data-model §2.9;
contract §5.1.8).

- ``generate`` is the **gated automation trigger**: it consults the single
  automation-gating seam with ``AutomationType.CONFIRMATION_GENERATION`` and
  fails closed with the gate's own reason, then resolves the company's
  registered client contacts, reads the master data **as of the generation
  day**, reads the tenant's **milestone panel** for the date the client is
  asked to reply by (the anniversary minus its configured offset, guarded
  against CR's deadline — never CR's own date), composes the bilingual message
  (``app.domain.confirmation``), stores it through the records-registry write
  path, supersedes the company's open draft for the same form, and writes one
  request row plus one audit entry.
- ``list_requests`` is the company view's read: every request the deployment
  has drafted for the company, newest first.

**The period's renewal is not read here.** Drafting a request is not a release:
the founder's revision of 2026-09-24 moved the gate to the moment a document
goes to a client, filled or not, so this trigger consults the §5.1 automation
gate and nothing else commercial. What a client has or has not answered about
continuing the engagement — and whether their fee has arrived — changes
nothing about what staff may draft.

**Nothing here sends.** There is no provider call, no outbox row, and no
``sent`` state: the composed message is stored (``documents``,
``message/rfc822``) for staff to review and send, and the send path is the
sending slice's. Scope §2.5 has the interim: TCSP staff send the draft from
their own mailbox.

**The gate is the trigger's, and a denial writes nothing** — no document, no
request row, no audit entry. The refusal is the point: scope §2.15 lists
confirmation requests first among the actions that run only for a company
whose package is active and within its coverage period.

**What the audit entry carries, and what it never carries.** It carries the
form, the dates, the version, the stored document's id, the recipients'
addresses (the inbound path's precedent: the sender address is recorded), and
the checklist's ``fact_id`` coordinates — a fixed vocabulary. It never carries
a question, an ``expected_value``, or any other master-data value: the words
the client is asked about live in the request row's ``checklist`` and in the
stored message, which is where the reply parser will read them, and the audit
chain is a record of what the product did rather than a copy of what it told
the client.

Services flush but never commit — the API session dependency commits at the
request edge, so a failed request leaves no partial writes.
"""

from dataclasses import dataclass
from datetime import UTC, date, datetime
from email.utils import make_msgid
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.persistence.confirmation import ConfirmationRequest
from app.adapters.persistence.models import Tenant
from app.application import (
    audit,
    company_master,
    contacts,
    documents,
    outbox,
    service_packages,
    tenant_config,
)
from app.application.documents import DocumentStore
from app.application.scanning import Scanner
from app.core.config import settings
from app.domain.audit import AuditAction
from app.domain.compliance import nar1_due_date
from app.domain.confirmation import (
    LIVE_REQUEST_STATUSES,
    RequestStatus,
    build_checklist,
    form_text,
    render_message,
    reply_deadline,
)
from app.domain.contacts import ChannelType
from app.domain.documents import (
    MESSAGE_CONTENT_TYPE,
    DocumentKind,
    DocumentOrigin,
    RetentionClass,
)
from app.domain.prior_filing import FormCode
from app.domain.send_modes import SendDestination, SendMode
from app.domain.service_packages import AutomationType, GatingReason


class AutomationDeniedError(Exception):
    """The §5.1 gate refused the trigger; ``reason`` names the failing leg."""

    def __init__(self, reason: GatingReason) -> None:
        self.reason = reason
        super().__init__(
            f"confirmation generation is not licensed for this company: {reason.value}"
        )


class MailNotConfiguredError(Exception):
    """This deployment has no mailbox address to write the request from.

    Scope §2.5: the message must come from the TCSP's own domain. Only the
    **address** is required — no connected credential — because nothing in
    this slice calls the provider: the draft is composed, stored, and sent by
    hand until the sending slice lands.
    """


class NoClientContactError(Exception):
    """The company has no associated person with an email address.

    The remedy is registering one (§2.4), which is why the refusal names the
    situation rather than writing an unaddressed message.
    """


class ClientContactNotFoundError(Exception):
    """The named contact is not an email recipient of this company.

    Either the person is not associated with the company at all, or the
    association exists but carries no email channel: both mean the caller
    named someone this request cannot be written to.
    """


class DraftConflictError(Exception):
    """Another draft for this company and form was committed concurrently.

    Two overlapping generations both read "no open draft", so both compute the
    same next version and both try to insert a live draft; the partial unique
    index lets exactly one commit, and the loser meets it here. **No two live
    drafts can ever commit** — the index is what guarantees that — so this is
    a refusal, not a hole: the caller re-reads (``GET`` the company's requests,
    which now shows the sibling's draft) and retries, and the retry is then an
    ordinary regeneration that supersedes it.
    """


@dataclass(frozen=True)
class Recipient:
    """One addressee of a request: the person, and the channel it goes to."""

    person_id: UUID
    address: str

    def as_dict(self) -> dict[str, str]:
        """The shape the request row's ``recipients`` JSON stores."""
        return {"person_id": str(self.person_id), "address": self.address}


async def generate(
    session: AsyncSession,
    store: DocumentStore,
    scanner: Scanner,
    *,
    tenant_id: UUID,
    company_id: UUID,
    actor_user_id: UUID | None,
    form_type: FormCode | str,
    target_date: date,
    as_of: date,
    contact_person_id: UUID | None = None,
) -> ConfirmationRequest:
    """Draft one confirmation request, store its message, and record it.

    ``target_date`` is the made-up date the request prepares for (the same
    parameter ``POST /companies/{id}/drafts`` takes). The ``reply_by`` the
    client is given is the **tenant's milestone panel** applied to that year:
    the anniversary minus ``client_reply_days_before``, floored at
    ``as_of + minimum_reply_window_days`` (``app.domain.milestones``,
    ``app.domain.confirmation.reply_deadline``) — CR's own deadline is the
    guard, never the printed date. A milestone that would put that date on or
    after the deadline refuses (``NoClientReplyWindowError``) — the last day CR
    allows is margin, not a client deadline — and so does a company type whose
    statutory rule is not established (``DueDateRuleNotEstablishedError``, still
    needed for the guard, which cannot verify "this is strictly inside CR's
    window" without it). ``as_of`` is the day the master data is read, the gate
    is decided, and the reply window is measured from.

    ``contact_person_id`` narrows the recipients to one associated person;
    without it every associated person's email channel is addressed. Refusals
    are typed and write nothing: ``UnsupportedFormError``,
    ``CompanyNotFoundError``, ``AutomationDeniedError``,
    ``MailNotConfiguredError``, ``NoClientContactError``,
    ``ClientContactNotFoundError``, ``CompanyNameUnavailableError``,
    ``DueDateRuleNotEstablishedError``, ``NoClientReplyWindowError``,
    ``DraftConflictError``.

    **The period's renewal is not consulted.** Drafting is not gated: the
    founder's revision of 2026-09-24 put the gate on the moment a document goes
    to the client, filled or not, so what a client has or has not answered
    changes nothing here and a ``pending`` renewal refuses nothing (ADR-0017).
    """
    # The form first: a form whose questions this product cannot write must
    # not reach any other check, let alone a stored message.
    form = form_text(form_type)
    form_code = FormCode(form_type)
    decision = await service_packages.check_automation(
        session,
        tenant_id=tenant_id,
        company_id=company_id,
        automation_type=AutomationType.CONFIRMATION_GENERATION,
        as_of=as_of,
    )
    if not decision.allowed:
        assert decision.reason is not None  # denied decisions always carry one
        raise AutomationDeniedError(decision.reason)
    company = await company_master.get_company(session, tenant_id=tenant_id, company_id=company_id)
    from_address = _mailbox_address()
    recipients = await _resolve_recipients(
        session, tenant_id=tenant_id, company_id=company.id, contact_person_id=contact_person_id
    )
    snapshot = await company_master.get_company_master(
        session, tenant_id=tenant_id, company_id=company.id, as_of=as_of
    )
    checklist = build_checklist(snapshot)
    # The tenant's panel decides the client-facing date; the statutory rule is
    # computed beside it as the guard (inside `reply_deadline`) and as the date
    # the row records, so a reader can see what the reply-by was checked
    # against — never as the date the client is given.
    milestones = await tenant_config.get_milestones(session, tenant_id=tenant_id)
    # The send mode decides whether this draft is released the moment it is
    # written. Read here, before anything is stored, so an unreadable block
    # refuses the whole trigger rather than leaving a stored message with no
    # request row (the fail-closed narrow read, §5.3).
    send_modes = await tenant_config.get_send_modes(session, tenant_id=tenant_id)
    reply_by = reply_deadline(
        company.anniversary_date,
        target_date,
        company_type=company.type,
        milestones=milestones,
        as_of=as_of,
    )
    statutory_due = nar1_due_date(
        company.anniversary_date, target_date.year, company_type=company.type
    )
    version = await _next_version(
        session, tenant_id=tenant_id, company_id=company.id, form_type=form_code.value
    )
    generated_at = datetime.now(UTC)
    rendered = render_message(
        form_type=form_code,
        name_en=None if snapshot.name is None else snapshot.name.name_en,
        name_zh=None if snapshot.name is None else snapshot.name.name_zh,
        cr_number=company.cr_number,
        br_number=company.br_number,
        reply_by=reply_by,
        checklist=checklist,
        from_address=from_address,
        to_addresses=[recipient.address for recipient in recipients],
        tenant_name=await _tenant_name(session, tenant_id=tenant_id),
        message_id=_message_id(from_address),
        sent_at=generated_at,
    )
    open_draft = await _open_draft(
        session, tenant_id=tenant_id, company_id=company.id, form_type=form_code.value
    )
    document = await documents.register_upload(
        session,
        store,
        tenant_id=tenant_id,
        company_id=company.id,
        actor_user_id=actor_user_id,
        kind=DocumentKind.GENERATED_DRAFT,
        origin=DocumentOrigin.GENERATED,
        content_type=MESSAGE_CONTENT_TYPE,
        data=rendered.message_bytes,
        scanner=scanner,
        form=form.code,
        retention_class=RetentionClass.CLIENT_COMMUNICATION,
        version=version,
    )
    if open_draft is not None:
        # Supersede before inserting: the partial unique index allows one live
        # request per company and form, and the flip has to reach the database
        # first — the version chain keeps the old row and its document. A live
        # request that was already released has its pending send cancelled in
        # the same transaction (``outbox.supersede``), so a superseded letter
        # cannot leave afterwards.
        if open_draft.document_id != document.id:
            document.supersedes_document_id = open_draft.document_id
        await outbox.supersede(session, request=open_draft)
        await session.flush()
    row = ConfirmationRequest(
        tenant_id=tenant_id,
        company_id=company.id,
        form_type=form_code.value,
        target_date=target_date,
        reply_by=reply_by,
        # The guard's own date, durable on the row: the reply-by was verified
        # against CR's deadline for this period, and the record says which.
        statutory_due=statutory_due,
        status=RequestStatus.DRAFT.value,
        version_no=version,
        document_id=document.id,
        checklist=[item.as_dict() for item in checklist],
        recipients=[recipient.as_dict() for recipient in recipients],
        created_by=actor_user_id,
    )
    # The one live draft per company and form is enforced by a partial unique
    # index; two overlapping generations both read "no open draft", compute the
    # same version and race to insert. The savepoint keeps the loser's failure
    # from poisoning the transaction, and the refusal tells the caller to
    # re-read and retry instead of showing them a server error.
    try:
        async with session.begin_nested():
            session.add(row)
            await session.flush()
    except IntegrityError as exc:
        raise DraftConflictError from exc
    # The release: the client destination's mode is the tenant's, and under
    # ``automatic`` the generation *is* the release (the controller's ruling —
    # immediate sends, no scheduler). The outbox row is written in this same
    # transaction, which is ADR-0011's rule: the intent is durable before
    # anything leaves, and the worker (or ``POST /mail/send``) delivers it.
    released: outbox.Release | None = None
    if send_modes.mode_for(SendDestination.CLIENT) is SendMode.AUTOMATIC:
        released = await outbox.release(
            session,
            tenant_id=tenant_id,
            company_id=company.id,
            request_id=row.id,
            actor_user_id=None,
            now=generated_at,
        )
    details: dict[str, object] = {
        "form_type": row.form_type,
        "target_date": row.target_date,
        "reply_by": row.reply_by,
        "version_no": row.version_no,
        "document_id": str(document.id),
        "recipients": [recipient.address for recipient in recipients],
        # The questions' coordinates, never their words: which facts the
        # client was asked about is auditable, what the product told them is
        # in the stored message and the request row.
        "facts": [item.fact_id for item in checklist],
    }
    if open_draft is not None:
        details["superseded_request_id"] = str(open_draft.id)
    if released is not None:
        # Recorded on the draft entry because this is where the mode decided
        # it: the release has its own entry, and the mode is the reason there
        # was one at all.
        details["send_mode"] = SendMode.AUTOMATIC.value
        details["destination"] = outbox.CONFIRMATION_DESTINATION.value
        details["outbox_id"] = str(released.job.id)
    await audit.record_event(
        session,
        tenant_id=tenant_id,
        action=AuditAction.CONFIRMATION_REQUEST_DRAFTED,
        actor_user_id=actor_user_id,
        company_id=company.id,
        entity_type="confirmation_request",
        entity_id=row.id,
        details=details,
    )
    return row


async def list_requests(
    session: AsyncSession, *, tenant_id: UUID, company_id: UUID
) -> list[ConfirmationRequest]:
    """The company's confirmation requests, newest draft first.

    Ungated (ADR-0012): the requests are the customer's own records, and a
    lapsed licence must not lock them out of what the deployment drafted for
    them. ``CompanyNotFoundError`` for an unknown or cross-tenant company.
    """
    company = await company_master.get_company(session, tenant_id=tenant_id, company_id=company_id)
    rows = await session.scalars(
        select(ConfirmationRequest)
        .where(ConfirmationRequest.company_id == company.id)
        .order_by(ConfirmationRequest.created_at.desc(), ConfirmationRequest.version_no.desc())
    )
    return list(rows.all())


def _mailbox_address() -> str:
    """The mailbox every client-facing message goes out from (scope §2.5).

    The configured address is load-bearing twice over: it is the ``From``
    header and it is the ``Message-ID``'s domain, so a deployment without one
    refuses rather than composing a letter that could not be recognised as
    the TCSP's own.
    """
    address = settings.mail_address.strip()
    if not address:
        raise MailNotConfiguredError
    return address


def _message_id(from_address: str) -> str:
    """A unique ``Message-ID`` in the mailbox's own domain.

    A reply's ``In-Reply-To`` carries this id back, which is one of the
    thread-matching signals §2.5 names; the domain is the deployment's own, so
    the header cannot claim a domain the TCSP does not own.
    """
    domain = from_address.rpartition("@")[2]
    return make_msgid(domain=domain or None)


async def _tenant_name(session: AsyncSession, *, tenant_id: UUID) -> str:
    """The TCSP's own name, for the message's signature block."""
    name = await session.scalar(select(Tenant.name).where(Tenant.id == tenant_id))
    assert name is not None  # a caller is authenticated against this tenant's users
    return name


async def _open_draft(
    session: AsyncSession, *, tenant_id: UUID, company_id: UUID, form_type: str
) -> ConfirmationRequest | None:
    """The request a regeneration of this company and form closes, if one exists.

    The partial unique index makes this at most one row *live*; the lookup
    also finds a ``sent`` request, because a regeneration over a request
    whose letter has left supersedes it too — its reminder chain ends with it
    (contract §5.1.12, ruled a Critical fix 2026-09-29). Reading it before the
    new version is written is what lets the old row be closed rather than
    coexist.
    """
    row: ConfirmationRequest | None = await session.scalar(
        select(ConfirmationRequest).where(
            ConfirmationRequest.tenant_id == tenant_id,
            ConfirmationRequest.company_id == company_id,
            ConfirmationRequest.form_type == form_type,
            ConfirmationRequest.status.in_(
                [status.value for status in LIVE_REQUEST_STATUSES]
                + [RequestStatus.SENT.value]
            ),
        )
    )
    return row


async def _next_version(
    session: AsyncSession, *, tenant_id: UUID, company_id: UUID, form_type: str
) -> int:
    """The next version of this company's draft lineage for this form.

    The lineage, not the open draft: a superseded draft keeps its version, so
    a regeneration after a supersession is still the next number rather than a
    reuse of one already recorded.
    """
    latest = await session.scalar(
        select(func.max(ConfirmationRequest.version_no)).where(
            ConfirmationRequest.tenant_id == tenant_id,
            ConfirmationRequest.company_id == company_id,
            ConfirmationRequest.form_type == form_type,
        )
    )
    return int(latest or 0) + 1


async def _resolve_recipients(
    session: AsyncSession, *, tenant_id: UUID, company_id: UUID, contact_person_id: UUID | None
) -> tuple[Recipient, ...]:
    """The company's email recipients, in a deterministic order.

    Every associated person's email channel is addressed, whatever the
    channel's status: a bounced address is the sending slice's to refuse, and
    silently dropping a recipient would be invisible to the staff member
    reviewing the draft. The persons are the ones ``list_company_contacts``
    returns, in that read's own order — ``Person.created_at``, so the
    company's oldest contact comes first — and each person's addresses are
    sorted, so the same company composes the same ``To`` every time.
    """
    persons = await contacts.list_company_contacts(
        session, tenant_id=tenant_id, company_id=company_id
    )
    if contact_person_id is not None:
        persons = [entry for entry in persons if entry.person.id == contact_person_id]
        if not persons:
            raise ClientContactNotFoundError(
                f"person {contact_person_id} is not a contact of this company"
            )
    recipients = tuple(
        Recipient(person_id=entry.person.id, address=address)
        for entry in persons
        for address in sorted(
            channel.identifier
            for channel in entry.channels
            if channel.channel_type == ChannelType.EMAIL.value
        )
    )
    if not recipients:
        if contact_person_id is not None:
            raise ClientContactNotFoundError(
                f"person {contact_person_id} has no email channel on this company"
            )
        raise NoClientContactError(
            "the company has no contact with an email address; register one before "
            "a confirmation request can be written"
        )
    return recipients


__all__ = [
    "AutomationDeniedError",
    "ClientContactNotFoundError",
    "DraftConflictError",
    "MailNotConfiguredError",
    "NoClientContactError",
    "Recipient",
    "generate",
    "list_requests",
]
