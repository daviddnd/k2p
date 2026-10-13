"""The multi-company split (workstream ③, ADR-0019 §3; migration ``0036``).

One inbound message deterministically attributed to **several** of the
sender's own companies becomes per-company work: the parent row parks the
message, the one indivisible gateway call asks the provider to divide the
tokenized content, and each work item carries its company's restored slice
into the ordinary per-company flows (the parse cycle's work-item leg first).
The provider is never asked which companies are involved (ruling D1) — the
involved set is the sender's own, the token→company binding stays in
request-scoped memory, and what crosses the wire is placeholders and prose
under the split profile's no-anchor shape (ruling D2).

**One row per message, ever.** The parent is keyed by the provider message
id (unique per tenant, the quarantine/case discipline): a re-examination —
the cursor's overlap, the E2 handoff racing a cycle — meets the winner's row
inside a savepoint rather than opening a second split.

**The guard order, and what each refusal means.** The envelope anti-abuse
guards (①'s ``ask_allowed``: an ``Auto-Submitted`` original, a null
``Return-Path``) and then the pre-flight (ruling D4's all-or-nothing
per-company legs — any involved company ``ai_disabled`` or failing the
``AI_REPLY_PARSING`` gate parks the whole parent, and ruling D5's bounds:
``split_max_companies`` and the sender's daily cap) park the parent
``staff_only`` with the reason named — never a failed cycle, never a partial
split. A gateway refusal parks the same way. Only the provider's answer
itself, failing the span grammar (ruling D3: an unknown token, a duplicated
or missing company, an empty span, a division that does not reconstruct the
content), moves the parent to ``failed`` — the answer was received and is
not trustworthy, which is a different fact from "not attempted". Every
parked parent stays staff-visible on the split surface; nothing is guessed.

**The audit vocabulary** (the ①② pattern, ruling D10): ``split_parent_created``
on the open (company column NULL — the parent is no company's record),
``split_parent_staff_only`` / ``split_parent_failed`` on the parks, and one
``split_item_created`` + ``split_parent_done`` pair when the items exist.

The status vocabularies below are this record's own (the ``SendDisposition``
precedent); the database CHECKs are the backstop, as everywhere else.
Services flush but never commit — the caller's session edge commits.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from email.message import EmailMessage
from email.policy import SMTP
from email.utils import format_datetime
from enum import StrEnum
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.persistence.models import SplitParent, SplitWorkItem
from app.application import (
    ai_gateway,
    audit,
    company_master,
    documents,
    officers,
    service_packages,
)
from app.application.attribution import ask_allowed
from app.application.documents import DocumentStore
from app.application.scanning import Scanner
from app.core.config import settings
from app.domain.ai_gateway import (
    SplitPreFlight,
    SplitPreFlightDecision,
    decide_split_pre_flight,
    split_violations_parsed,
)
from app.domain.attribution import CompanyReference
from app.domain.audit import AuditAction
from app.domain.communications import message_text
from app.domain.documents import (
    MESSAGE_CONTENT_TYPE,
    DocumentKind,
    DocumentOrigin,
    RetentionClass,
)
from app.domain.redaction import RedactionRefused, restore_tokens
from app.domain.service_packages import AutomationType


# The parent's own statuses (the DB CHECK is the backstop).
class SplitParentStatus(StrEnum):
    """Where one split parent stands (ruling D6's CHECK, verbatim)."""

    SPLIT_PENDING = "split_pending"
    SPLIT_DONE = "split_done"
    STAFF_ONLY = "staff_only"
    FAILED = "failed"


class SplitWorkItemStatus(StrEnum):
    """Where one work item stands (the per-company flow's vocabulary)."""

    AWAITING_PARSE = "awaiting_parse"
    PARSED = "parsed"
    FAILED = "failed"


@dataclass(frozen=True)
class SplitReport:
    """What one split pass reached — coordinates only.

    ``status`` is the parent's own value; ``reason`` is the bounded code for
    a parked parent (the pre-flight's refusal vocabulary, ``ask_not_allowed``,
    ``refused:<reason>``, ``unavailable``, ``no_content``, ``ambiguous_value``,
    or ``grammar_violations`` for a failed division). ``item_ids`` is empty
    unless the parent reached ``split_done``.
    """

    parent_id: UUID
    status: SplitParentStatus
    reason: str | None = None
    item_ids: tuple[UUID, ...] = ()


async def split_message(
    session: AsyncSession,
    store: DocumentStore,
    scanner: Scanner,
    *,
    tenant_id: UUID,
    mailbox_message_id: str,
    sender_address: str,
    subject: str,
    received_at: datetime,
    raw: bytes,
    involved: Sequence[CompanyReference],
    auto_submitted: str | None,
    return_path: str | None,
    daily_splits_for_sender: int,
    now: datetime | None = None,
    actor_user_id: UUID | None = None,
) -> SplitReport:
    """Split one multi-company message into per-company work items.

    ``raw`` is the message's bytes (the caller read them to decide — the
    resolver's read at E1, the parked original's re-read at E2); ``involved``
    is the sender's own matched companies (the resolver's decisive candidates
    or the answer grammar's matched set — ruling D1), and
    ``daily_splits_for_sender`` is the caller's count of this sender's
    automatic splits in the rolling day (ruling D5's cap). Documents'
    validation errors (an unusable raw message) propagate: with no raw there
    is no parent row the schema can hold, and the caller parks the message
    the way today's flow does.
    """
    at = now if now is not None else datetime.now(UTC)
    try:
        # One row per message, ever: the savepoint confines the unique index's
        # refusal so the concurrent race answers with the winner's row (the
        # ①② discipline). The raw registers through the single records seam,
        # company-less — the split parent's raw inbound message is the one
        # inbound record with no company (0035's widening, 0036's parent).
        async with session.begin_nested():
            raw_document = await documents.register_upload(
                session,
                store,
                tenant_id=tenant_id,
                company_id=None,
                actor_user_id=actor_user_id,
                kind=DocumentKind.CLIENT_REPLY,
                origin=DocumentOrigin.EMAIL_ATTACHMENT,
                retention_class=RetentionClass.CLIENT_COMMUNICATION,
                content_type=MESSAGE_CONTENT_TYPE,
                data=raw,
                scanner=scanner,
            )
            parent = SplitParent(
                tenant_id=tenant_id,
                mailbox_message_id=mailbox_message_id,
                sender_address=sender_address,
                subject=subject,
                received_at=received_at,
                raw_document_id=raw_document.id,
                status=SplitParentStatus.SPLIT_PENDING.value,
            )
            session.add(parent)
            await session.flush()
    except IntegrityError:
        existing = await _parent_for_message(
            session, tenant_id=tenant_id, mailbox_message_id=mailbox_message_id
        )
        if existing is None:
            raise  # the unique index is not what refused this insert
        return _report(existing)
    await audit.record_event(
        session,
        tenant_id=tenant_id,
        action=AuditAction.SPLIT_PARENT_CREATED,
        actor_user_id=actor_user_id,
        company_id=None,
        entity_type="split_parent",
        entity_id=parent.id,
        details={
            "mailbox_message_id": mailbox_message_id,
            "sender_address": sender_address,
            "involved_count": len(involved),
        },
    )

    if not ask_allowed(auto_submitted=auto_submitted, return_path=return_path):
        return await _park(
            session,
            parent,
            SplitParentStatus.STAFF_ONLY,
            "ask_not_allowed",
            actor_user_id=actor_user_id,
        )

    content = message_text(raw)
    if not content:
        return await _park(
            session, parent, SplitParentStatus.STAFF_ONLY, "no_content",
            actor_user_id=actor_user_id,
        )

    facts: list[tuple[bool, bool]] = []
    for reference in involved:
        company = await company_master.get_company(
            session, tenant_id=tenant_id, company_id=reference.company_id
        )
        gate = await service_packages.check_automation(
            session,
            tenant_id=tenant_id,
            company_id=reference.company_id,
            automation_type=AutomationType.AI_REPLY_PARSING,
            as_of=at.date(),
        )
        facts.append((company.ai_disabled, gate.allowed))
    pre_flight = decide_pre_flight(
        involved_count=len(involved),
        daily_splits_for_sender=daily_splits_for_sender,
        company_facts=facts,
    )
    if pre_flight.decision is SplitPreFlightDecision.STAFF_ONLY:
        assert pre_flight.reason is not None  # a park always names why
        return await _park(
            session, parent, SplitParentStatus.STAFF_ONLY, pre_flight.reason.value,
            actor_user_id=actor_user_id,
        )

    person_names: list[str] = []
    for reference in involved:
        appointments = await officers.list_appointments(
            session, tenant_id=tenant_id, company_id=reference.company_id, as_of=at.date()
        )
        for appointment in appointments:
            person_names.extend(officers.person_name_forms(appointment.person))
    lexicon, value_to_company = _lexicon(involved, person_names)
    try:
        call = await ai_gateway.infer_split(
            session,
            tenant_id=tenant_id,
            parent_id=parent.id,
            language="en",
            content=content,
            tokenize_values=lexicon,
            actor_user_id=actor_user_id,
        )
    except ai_gateway.InferenceRefused as exc:
        return await _park(
            session, parent, SplitParentStatus.STAFF_ONLY, f"refused:{exc.reason}",
            actor_user_id=actor_user_id,
        )
    except RedactionRefused as exc:
        # The compose could not make the content defensible, or the strict
        # restore refused an invented value: nothing here is landed, and the
        # parent parks the way a gateway refusal parks.
        return await _park(
            session, parent, SplitParentStatus.STAFF_ONLY, f"refused:{exc.reason}",
            actor_user_id=actor_user_id,
        )
    except ai_gateway.InferenceUnavailable as exc:
        return await _park(
            session, parent, SplitParentStatus.STAFF_ONLY, f"unavailable:{exc.code}",
            actor_user_id=actor_user_id,
        )

    token_to_company: dict[str, UUID] = {}
    ambiguous_values: set[str] = set()
    for token, value in call.tokens.items():
        owners = value_to_company.get(value, set())
        if len(owners) == 1:
            token_to_company[token] = next(iter(owners))
        elif len(owners) > 1:
            ambiguous_values.add(value)
    if ambiguous_values:
        # Two involved companies share an identifying value: the binding
        # cannot be resolved locally, and the provider never sees it (ruling
        # D1) — staff disambiguate, never a guessed item.
        return await _park(
            session, parent, SplitParentStatus.STAFF_ONLY, "ambiguous_value",
            actor_user_id=actor_user_id,
        )
    company_tokens = set(token_to_company)
    violations = split_violations_parsed(
        call.result,
        tokens=set(call.tokens),
        company_tokens=company_tokens,
        content=call.text,
    )
    if violations:
        parent.status = SplitParentStatus.FAILED.value
        await session.flush()
        await audit.record_event(
            session,
            tenant_id=tenant_id,
            action=AuditAction.SPLIT_PARENT_FAILED,
            actor_user_id=actor_user_id,
            company_id=None,
            entity_type="split_parent",
            entity_id=parent.id,
            details={"violations": [item.value for item in violations]},
        )
        return _report(parent, reason="grammar_violations")

    spans = call.result
    assert isinstance(spans, list)  # the grammar admitted it
    items: list[SplitWorkItem] = []
    for span in spans:
        company_id = token_to_company[span["company"]]
        # The slice restores locally, strictly: a value the model wrote that
        # this call never sent refuses the answer rather than passing it into
        # a company's records.
        slice_text = restore_tokens(span["text"], call.tokens, allowed_literals=frozenset())
        assert isinstance(slice_text, str)  # a span's text restores to a string
        slice_document = await documents.register_upload(
            session,
            store,
            tenant_id=tenant_id,
            company_id=company_id,
            actor_user_id=actor_user_id,
            kind=DocumentKind.CLIENT_REPLY,
            origin=DocumentOrigin.EMAIL_ATTACHMENT,
            retention_class=RetentionClass.CLIENT_COMMUNICATION,
            content_type=MESSAGE_CONTENT_TYPE,
            data=_slice_message(
                sender_address=sender_address,
                subject=subject,
                received_at=received_at,
                body=slice_text,
            ),
            scanner=scanner,
        )
        item = SplitWorkItem(
            tenant_id=tenant_id,
            parent_id=parent.id,
            company_id=company_id,
            slice_document_id=slice_document.id,
            status=SplitWorkItemStatus.AWAITING_PARSE.value,
        )
        session.add(item)
        await session.flush()
        items.append(item)
    parent.status = SplitParentStatus.SPLIT_DONE.value
    await session.flush()
    item_ids = tuple(item.id for item in items)
    await audit.record_event(
        session,
        tenant_id=tenant_id,
        action=AuditAction.SPLIT_ITEM_CREATED,
        actor_user_id=actor_user_id,
        company_id=None,
        entity_type="split_parent",
        entity_id=parent.id,
        details={
            "item_ids": [str(item_id) for item_id in item_ids],
            "company_ids": [str(item.company_id) for item in items],
        },
    )
    await audit.record_event(
        session,
        tenant_id=tenant_id,
        action=AuditAction.SPLIT_PARENT_DONE,
        actor_user_id=actor_user_id,
        company_id=None,
        entity_type="split_parent",
        entity_id=parent.id,
        details={"item_ids": [str(item_id) for item_id in item_ids]},
    )
    return _report(parent, item_ids=item_ids)


def decide_pre_flight(
    *,
    involved_count: int,
    daily_splits_for_sender: int,
    company_facts: Sequence[tuple[bool, bool]],
) -> SplitPreFlight:
    """The pre-flight with this deployment's configured bounds (ruling D5) —
    the pure rule fed by the caller's resolved facts."""
    return decide_split_pre_flight(
        involved_count=involved_count,
        max_companies=settings.split_max_companies,
        daily_splits_for_sender=daily_splits_for_sender,
        daily_cap_per_sender=settings.split_daily_cap_per_sender,
        company_facts=company_facts,
    )


def _lexicon(
    involved: Sequence[CompanyReference], person_names: Sequence[str] = ()
) -> tuple[dict[str, list[str]], dict[str, set[UUID]]]:
    """The split's token map (ruling D2): the involved companies'
    identifying values under the tokenizer's closed labels, the involved
    companies' person names beside them (the existing machinery's person
    coverage), and the value→company binding kept local. Every
    company-identifying value a span can reference maps home; a value two
    involved companies share stays ambiguous and parks the parent rather
    than guessing. Person names tokenize without a company binding — a span
    references its company, not its people."""
    names: list[str] = []
    crs: list[str] = []
    brns: list[str] = []
    value_to_company: dict[str, set[UUID]] = {}
    for reference in involved:
        for value in (reference.name, reference.cr_number, reference.br_number):
            if value:
                value_to_company.setdefault(value, set()).add(reference.company_id)
        if reference.name:
            names.append(reference.name)
        if reference.cr_number:
            crs.append(reference.cr_number)
        if reference.br_number:
            brns.append(reference.br_number)
    lexicon: dict[str, list[str]] = {"COMPANY": names}
    if crs:
        lexicon["CR"] = crs
    if brns:
        lexicon["BRN"] = brns
    if person_names:
        lexicon["PERSON"] = list(person_names)
    return lexicon, value_to_company


def _slice_message(
    *, sender_address: str, subject: str, received_at: datetime, body: str
) -> bytes:
    """One work item's slice as a stored message: the restored per-company
    excerpt, message-shaped so the parse leg reads it through the ordinary
    text path. The envelope facts are the original's own (the slice is the
    original's content, attributed to this company)."""
    message = EmailMessage(policy=SMTP)
    message["From"] = sender_address
    message["Subject"] = subject
    message["Date"] = format_datetime(received_at)
    message.set_content(body)
    return message.as_bytes()


async def _park(
    session: AsyncSession,
    parent: SplitParent,
    status: SplitParentStatus,
    reason: str,
    *,
    actor_user_id: UUID | None,
) -> SplitReport:
    """Park one parent for staff with its reason named — never a failed
    cycle: the row stays visible on the split surface, and the entry writes
    the company column NULL (the parent's own event, ruling D10)."""
    parent.status = status.value
    await session.flush()
    await audit.record_event(
        session,
        tenant_id=parent.tenant_id,
        action=AuditAction.SPLIT_PARENT_STAFF_ONLY,
        actor_user_id=actor_user_id,
        company_id=None,
        entity_type="split_parent",
        entity_id=parent.id,
        details={"reason": reason},
    )
    return _report(parent, reason=reason)


def _report(
    parent: SplitParent,
    *,
    reason: str | None = None,
    item_ids: tuple[UUID, ...] = (),
) -> SplitReport:
    return SplitReport(
        parent_id=parent.id,
        status=SplitParentStatus(parent.status),
        reason=reason,
        item_ids=item_ids,
    )


async def _parent_for_message(
    session: AsyncSession, *, tenant_id: UUID, mailbox_message_id: str
) -> SplitParent | None:
    """The parent one provider message id already opened, if any — the
    idempotency answer behind the unique index's refusal."""
    found: SplitParent | None = await session.scalar(
        select(SplitParent).where(
            SplitParent.tenant_id == tenant_id,
            SplitParent.mailbox_message_id == mailbox_message_id,
        )
    )
    return found


async def splits_for_sender_count(
    session: AsyncSession, *, tenant_id: UUID, sender_address: str, at: datetime
) -> int:
    """How many split parents this sender opened on ``at``'s day — the daily
    cap's read (ruling D5). Parents are counted however they settled: the cap
    bounds the splitting, and a parent that parked is still a split the
    sender drove."""
    count = await session.scalar(
        select(func.count())
        .select_from(SplitParent)
        .where(
            SplitParent.tenant_id == tenant_id,
            SplitParent.sender_address == sender_address,
            func.date(SplitParent.created_at) == at.date(),
        )
    )
    return int(count or 0)


async def list_split_parents(
    session: AsyncSession, *, tenant_id: UUID, status: SplitParentStatus | None = None
) -> list[SplitParent]:
    """The tenant's split parents, newest first, optionally one status — the
    staff surface's read (coordinates only, the attribution-cases custody
    bar)."""
    statement = select(SplitParent).where(SplitParent.tenant_id == tenant_id)
    if status is not None:
        statement = statement.where(SplitParent.status == status.value)
    rows = await session.scalars(
        statement.order_by(SplitParent.created_at.desc(), SplitParent.id)
    )
    return list(rows.all())


async def get_split_parent(
    session: AsyncSession, *, tenant_id: UUID, parent_id: UUID
) -> SplitParent | None:
    """One parent by id, tenant-scoped — or ``None`` (unknown and another
    tenant's are one answer, the quarantine surface's rule)."""
    found: SplitParent | None = await session.scalar(
        select(SplitParent).where(
            SplitParent.id == parent_id, SplitParent.tenant_id == tenant_id
        )
    )
    return found


async def items_for_parent(
    session: AsyncSession, *, tenant_id: UUID, parent_id: UUID
) -> list[SplitWorkItem]:
    """One parent's work items, oldest first — the per-company coordinates
    the surface names."""
    rows = await session.scalars(
        select(SplitWorkItem)
        .where(SplitWorkItem.tenant_id == tenant_id, SplitWorkItem.parent_id == parent_id)
        .order_by(SplitWorkItem.created_at, SplitWorkItem.id)
    )
    return list(rows.all())


async def awaiting_work_items(session: AsyncSession, *, tenant_id: UUID) -> list[SplitWorkItem]:
    """The parse cycle's work-item listing: items still owed a parse, oldest
    first — the reply leg's "no parse row" listing, the item leg's shape."""
    rows = await session.scalars(
        select(SplitWorkItem)
        .where(
            SplitWorkItem.tenant_id == tenant_id,
            SplitWorkItem.status == SplitWorkItemStatus.AWAITING_PARSE.value,
        )
        .order_by(SplitWorkItem.created_at, SplitWorkItem.id)
    )
    return list(rows.all())


__all__ = [
    "SplitParentStatus",
    "SplitReport",
    "SplitWorkItemStatus",
    "awaiting_work_items",
    "get_split_parent",
    "items_for_parent",
    "list_split_parents",
    "split_message",
    "splits_for_sender_count",
]
