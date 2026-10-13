"""Attribution-case lifecycle (ADR-0019 §1; migrations ``0033``/``0034``).

The application half of the registration/confirmation flow and its
clarification loop (workstream ②): the case row the ingestion cycle opens
when a threaded message's sender is not a registered channel of the chain's
company, or when an unthreaded message is an unambiguously
deterministic-but-unattributed one (the A/B classes, ruling D4); the ask that
case owes; and the acts that end it — the registered channel's confirmation,
the message that arrives in answer to the 2.1 ask, the client's
which-company answer, and the registered channel's first-contact remedy.
``open_case``, ``enqueue_ask``, ``confirm_case``, ``process_case``,
``close_registration_cases``, ``close_first_contact_cases`` and
``resolve_clarification_case`` are one lifecycle written in one place; the
staff surface's ``list_cases``, ``dismiss_case`` and ``process_open_case``
read it and settle what the automated roads have not.

**What opening a case is.** ``decide`` (``app.domain.attribution``) has
already resolved the threaded message from metadata alone; ``decide_unthreaded``
has done the same for the two unthreaded classes. ``open_case`` maps the
verdict to the row — ``ask_registered_channel`` →
``awaiting_registered_channel`` with ask kind ``registered_channel``, and
``ask_confirmation`` → ``awaiting_confirmation`` with ask kind
``confirmation``; ``ask_first_contact`` → ①'s ``awaiting_registered_channel``
with ①'s kind (a company-less 2.1 variant), and ``ask_clarification`` →
``awaiting_clarification`` with kind ``clarification`` — and writes one row
through ``app.adapters.persistence.attribution``, keyed by the provider
message id. The two thread anchors travel with it (the provider ``thread_id``
and the original's RFC 5322 ``Message-ID``), because a reply is matched by
either. The two unthreaded classes open with **no company** (ruling D2): the
row takes its company at resolution (the A-class answer, ruling D6) or
closure (the B-class remedy, ruling D8), and every event the company-less row
writes carries ``company_id = NULL`` (ruling D10).

**What opening a case is not.** It fetches nothing, scans nothing, stores no
document and writes no ``Communication``: the unregistered sender's body is
not read before the confirmation (ruling R2), so a case that opens is the
statement that the message is parked — not that it was received into the
records registry. Nothing here talks to the mailbox at all: the ask's own
enqueue composes from envelope facts the caller already holds, and the
mail-send cycle delivers it like every other queued letter.

**The ask's enqueue** (``enqueue_ask``) is where the asks are decided and
composed, and every reason an ask may be kept off is checked there, before
anything is written. The company-bound cases carry ①'s guard order: the
case's own state, suppression (ruling R3), the §5.1 gate (``check_automation``
for the case's company), the anti-abuse headers, and the authority asks'
hourly cap (``attribution_ask_hourly_cap``). A **company-less case is asked
under ②'s rules instead** (rulings D3/D7), inside this same function — the
one enqueue path, not a second sender:

- **No §5.1 gate** (ruling D3) — a company-less case has no company to gate
  on, and the ask *replaces* ungated core quarantine handling: gating the
  replacement on paid automation would regress Free tenants.
- **Suppression** — ``latest_ask_for_anchor``: one ask per (tenant, sender,
  anchor); another open case already owning the anchor's ask keeps this one
  quiet.
- **The anti-abuse guards** — ①'s ``ask_allowed`` over the two envelope
  headers, unchanged (ruling C6).
- **The convenience asks' hourly cap** — ``clarification_ask_hourly_cap``,
  counted over the company-less pool only (``count_asks_since``'s
  ``company_less`` leg), so a burst of cold or ambiguous mail cannot starve
  ①'s authority asks — and vice versa.
- **The two-ask bound** — ``count_asks_for_case``: one ask, one re-ask on a
  later unresolvable message from the same sender on the anchor, then never
  again (ruling D7); staff dismiss/retry remains.

**The company-less letter's registration.** The letters compose in full
(Letters A and B, ``app.domain.attribution``'s founder-gated copy, addressed
to the sender alone — ruling D8 — and led by the TCSP-named subject, C10),
and register through the single records-registry
seam **tenant-scoped**: migration ``0035`` (the registry widening, built HELD
like its predecessors) made ``documents.company_id`` nullable, and the seam's
company-less shape stores the letter under the tenant-level ``_correspondence``
fallback segment — operator-readable, deterministic (the seam's own rule,
``app.application.documents``). The enqueue then queues the request-less,
company-less outbox row exactly the way ①'s asks ride it, stamps
``ask_sent_at``, and writes the one ``attribution_ask_enqueued`` entry with
the company column NULL (ruling D10); delivery is §5.1.11's cycle, which
records the outbound exchange with ``company_id = NULL`` (the widening's
second column). Until the founder's verbatim copy approval is recorded, a
deployment holds both hourly caps at ``0`` — the mechanism sends; the posture
gates it (the records note, contract §5.1.7).

**The ask's enqueue** (``enqueue_ask``) is where the two §1 asks are decided
and composed, and every reason an ask may be kept off is checked there, in
this order, before anything is written:

- **The case's own state** — a case that has left the two awaits is never
  asked again. The cursor's overlap re-examines the same provider message id
  and ``open_case`` returns the terminal row as it stands; without this check
  the re-read would reopen a settled case with a fresh ask.
- **Suppression** (ruling R3) — ``latest_ask_for_thread``: when an open case
  on the same anchor has already asked, this case is not asked about. The
  anchor is the provider ``thread_id`` **or** any RFC 5322 ``Message-ID`` in
  the case's ancestor set — the original's own id, plus the
  ``In-Reply-To``/``References`` tokens the message carries — so a second
  message on the chain meets the first case's ask even where no provider
  thread id exists (the IMAP leg). The case stays open with ``ask_sent_at``
  NULL, visible to staff.
- **The §5.1 gate** (the review's first ruling) — ``check_automation`` for
  the case's company, ``EMAIL_INGESTION``: a company whose licence or package
  does not cover the ingestion is not asked either; its case stays open.
- **The anti-abuse guards** (ruling R4) — ``ask_allowed`` over the two
  envelope headers (``Auto-Submitted``, a ``Return-Path`` with no address)
  and the tenant's hourly cap (``attribution_ask_hourly_cap``; ``0`` = never
  ask). A guard that trips suppresses this ask only, never the cycle.

An ask that passes all of them is composed from envelope facts alone — the
two bilingual drafts are ``app.domain.attribution``'s (founder-gated copy),
threaded on the original's RFC ``Message-ID`` so the answer comes back to
this thread — stored through the records registry as the letter's WORM
original (``generated_draft``, retention ``client_communication``), queued
through ``app.application.outbox`` as a request-less ``attribution_ask`` row
(send-on-release: the tenant's ``SendMode`` never holds a notice), and the
case is stamped ``ask_sent_at`` — all in the caller's transaction, so the
suppression read sees the ask the moment the row exists. One
``attribution_ask_enqueued`` entry names the act; the suppressed paths write
none.

**The confirming act** (``confirm_case``, ruling R5). A registered channel
of the case's company answering the confirmation ask is the authorisation:
``confirm_case`` pins that stored reply to every case still
``awaiting_confirmation`` on the anchor (``confirmation_communication_id``)
and nothing else — the status stays the await, because the confirmation
authorises processing rather than being it, and a case whose processing the
§5.1 gate then denies must stay waiting for staff rather than look settled.
The awaiting state is also the guard that makes the act idempotent: a case
that has left the awaits is never re-confirmed. When the gate kept the
confirming reply from being stored, the pin stays NULL and the act is still
recorded — it is a fact about authority, and the ingestion cycle (which owns
the message's row) writes its one entry.

**Processing the original** (``process_case``, ruling R6). The tail of the
ingestion order re-runs for the case's ``mailbox_message_id`` with the
company fixed to the case's own (``mail_ingest.ingest_case_message``: the
§5.1 gate → raw fetch → scan/store → attachments → ``Communication`` → one
audit entry, the ordinary reply join included), reached through a
function-level import so the two services' import graph stays acyclic. A
Communication for the original — freshly written, or already there because
another road (the §2.5 retry) recorded it — is success: the case moves to
``resolved`` with ``resolved_communication_id``, and its queued ask is
cancelled (a settled case is never asked). A denial or a failure returns the
bounded reason and leaves the case awaiting: the gate is checked here for
this message alone, and staff can process again from the case surface. The
mailbox's own read failures are the one exception to "failures are
outcomes": a provider that answers and cannot produce this message
(``MailProviderRefused``, ``MailProviderUnavailable``,
``MailResponseInvalid``) becomes ``fetch_failed:<reason>`` rather than an
abort, because one unreachable message must not cost the confirmation that
authorised it; a mailbox that cannot be reached at all still propagates (the
ingestion cycle's own rule).

**The 2.1 closure** (``close_registration_cases``, ruling R7). The
registered channel's own message on the thread is the remedy the 2.1 ask
asked for: every case still ``awaiting_registered_channel`` among the ones
the caller read moves to ``closed`` with ``closed_at``, its queued ask is
cancelled the same way, and one ``attribution_case_closed`` entry names the
ids that closed — one entry, because it is one message that brought the
remedy. Cases that awaited a confirmation, and cases that have settled, are
left exactly as they are; a second call closes nothing and writes nothing.
The caller runs it for a message that did **not** act as the confirmation
(ruling M2): the act attempts every open awaiting case on the anchor, so a
closure behind it would close a case whose processing the gate denied — the
one that must stay awaiting for the staff surface's retry (ruling R6).

**The B-class closure** (``close_first_contact_cases``, ruling D8). The
company-less variant of the 2.1 closure: any message on the anchor whose
sender is a registered channel of **any** company is the remedy a
first-contact case asked for — the sender has arrived through a registered
address. Exactly the company-less ``awaiting_registered_channel`` cases among
the caller's read close (their company-bound cousins are ①'s, closed only by
the chain's own registered channel through ``close_registration_cases``),
their queued asks are cancelled, and one ``attribution_case_closed`` entry —
with ``company_id = NULL``, the D10 rule for company-less events — names the
ids. The closing message itself is not consumed: it follows its own
registered-sender path, and this function never reads it.

**The A-class answer** (``resolve_clarification_case``, ruling D6). A later
message on the anchor from the case's sender whose first non-empty line the
answer grammar accepts — exactly one of the sender's companies, by normalized
name or CR/BRN digit sequence (``match_company_reference``, over the
references the caller resolved from the contacts and the master data) — is
the resolution: the case takes the named company (``set_case_company``, so
the composite foreign key pins the pair), the ingestion tail re-runs for the
parked original with the company fixed (① R6's own ``process_case``), and the
case moves to ``resolved`` naming the exchange. Zero or several matches stay
awaiting — several is flagged for staff as the future ③ handoff — and the
caller may then drive the bounded re-ask. One ``attribution_case_resolved``
entry names the case, the exchange and the company the answer chose; a tail
refusal (the §5.1 gate, an unusable original) leaves the case awaiting with
its company now stamped, ready for the staff surface's retry.

**The staff surface** (``app.api.attribution``). The three entry points the
API's routes call. ``list_cases`` is the queue's read — metadata only, one or
more statuses, ``None`` every row — and is where the surface's ``awaiting``
speaks the three open statuses at once. ``dismiss_case`` closes an open case
staff have decided needs no further action: ``dismissed`` with the caller's
instant, the queued ask cancelled, one entry. ``process_open_case`` is the
staff-forced processing — the confirmation's own road into ``process_case``,
reached by id rather than through the thread, so a case whose confirmation
the gate denied (or whose original the mailbox could not produce) is workable
by hand. It **refuses a company-less case** (ruling D9): staff cannot
force-process what has no company — the road into the tail is per company —
and the typed ``AttributionCaseHasNoCompanyError`` is the route's 409
``attribution_case_has_no_company``. Both actions read the row **under the
row lock** and refuse a case that has left the awaits with the same typed
error; the lock is what keeps two overlapping settles from appending two
entries to the append-only chain for one mutation.

**One row per message, ever.** The unique index on ``(tenant_id,
mailbox_message_id)`` is the same idempotency key the ingestion cycle and the
quarantine table use: a re-examination of the same message — the cursor's
overlap, a §2.5 retry — writes no second row, and the refused insert is
answered with the winner's row inside a savepoint rather than raised (the
``record_outbound_message`` discipline). ``ask_sent_at`` stays NULL when the
case opens: the row is written before any ask is enqueued, and only the
enqueue stamps it (rulings R3/R4).

**The audit entries.** Opening a case emits one ``attribution_case_opened``
entry through the single write path (``app.application.audit.record_event``)
on the caller's session, carrying the case id, the sender and the status —
never a fragment of the mail; a company-less case's entry writes
``company_id = NULL`` (ruling D10 — the per-company compliance report ignores
company-less events, which surface on the case surface instead). An enqueued
ask emits one ``attribution_ask_enqueued`` entry, coordinates only (the
case, its kind, the row, the letter and the destination). A closure emits
one ``attribution_case_closed`` entry naming the case ids it closed. The
confirmation's own ``attribution_confirmed`` entry is written by the act's
caller (``app.application.mail_ingest``), because it names what the act
achieved — which cases resolved, to which stored exchanges; a refused
duplicate writes none of them: the row that already exists was audited when
it was created. The staff surface writes the case's own two settles: one
``attribution_case_dismissed`` entry (the case id) and one
``attribution_case_resolved`` entry (the case, the exchange it resolved to
and the acting member); the A-class answer's road writes the same resolved
entry with the system actor — one entry per mutation, and none for a refusal
that changed nothing.

Services flush but never commit — the caller's session edge commits.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from email.policy import SMTP
from email.utils import format_datetime, make_msgid
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.mail.errors import MailProviderRefused, MailProviderUnavailable
from app.adapters.persistence.attribution import (
    OPEN_STATUSES,
    count_asks_for_case,
    count_asks_since,
    create_case,
    latest_ask_for_anchor,
    latest_ask_for_thread,
    set_ask_sent,
    set_case_company,
    set_confirmation,
    set_status,
)
from app.adapters.persistence.attribution import list_cases as list_case_rows
from app.adapters.persistence.documents import Document
from app.adapters.persistence.models import AttributionCase, Tenant
from app.application import (
    audit,
    company_master,
    contacts,
    documents,
    outbox,
    service_packages,
)
from app.application.documents import DocumentStore
from app.application.scanning import Scanner
from app.core.config import settings
from app.domain.attribution import (
    AttributionAction,
    AttributionAskKind,
    AttributionCaseStatus,
    CompanyReference,
    UnthreadedAction,
    ask_subject,
    clarification_ask_letter,
    companyless_ask_subject,
    confirmation_ask_letter,
    first_contact_ask_letter,
    match_company_references,
    registration_ask_letter,
)
from app.domain.audit import AuditAction
from app.domain.communications import IngestOutcome, normalized_address
from app.domain.documents import (
    MESSAGE_CONTENT_TYPE,
    DocumentKind,
    DocumentOrigin,
    RetentionClass,
)
from app.domain.mail import MailResponseInvalid
from app.domain.service_packages import AutomationType

# The two ask actions, and the row each one opens: the awaiting status and the
# ask kind the decision chose. ``plain`` and ``process`` open nothing — there
# is no case to keep — so they are deliberately absent and refused below.
_OPEN_STATUS: dict[AttributionAction, tuple[AttributionCaseStatus, AttributionAskKind]] = {
    AttributionAction.ASK_REGISTERED_CHANNEL: (
        AttributionCaseStatus.AWAITING_REGISTERED_CHANNEL,
        AttributionAskKind.REGISTERED_CHANNEL,
    ),
    AttributionAction.ASK_CONFIRMATION: (
        AttributionCaseStatus.AWAITING_CONFIRMATION,
        AttributionAskKind.CONFIRMATION,
    ),
}

# The clarification loop's two unthreaded asks and the rows each opens (ruling
# D4): the first-contact ask reuses ①'s status and kind — its letter is the
# company-less variant — and the which-company ask carries the new status and
# kind. Both open with no company (ruling D2); ``quarantine`` opens nothing.
_UNTHREADED_OPEN_STATUS: dict[
    UnthreadedAction, tuple[AttributionCaseStatus, AttributionAskKind]
] = {
    UnthreadedAction.ASK_FIRST_CONTACT: (
        AttributionCaseStatus.AWAITING_REGISTERED_CHANNEL,
        AttributionAskKind.REGISTERED_CHANNEL,
    ),
    UnthreadedAction.ASK_CLARIFICATION: (
        AttributionCaseStatus.AWAITING_CLARIFICATION,
        AttributionAskKind.CLARIFICATION,
    ),
}


async def open_case(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    company_id: UUID | None,
    mailbox_message_id: str,
    sender_address: str,
    action: AttributionAction | UnthreadedAction,
    thread_id: str | None = None,
    original_rfc_message_id: str | None = None,
) -> tuple[AttributionCase, bool]:
    """Open one case for one original message; ``(case, created)``.

    ``action`` is the decision's verdict — one of ``decide``'s two asks for a
    threaded message, or one of ``decide_unthreaded``'s two asks for the
    clarification loop's classes. Anything else opens nothing (a
    ``plain``/``process``/``quarantine`` message is not a case, and a caller
    reaching here with one is a bug — a ``ValueError`` says so rather than
    writing a row the decision never chose). The two families are exclusive in
    the company they carry: ①'s asks need the chain's company, and the
    unthreaded asks open **company-less** — a mismatch is refused the same way,
    because the row's shape is the decision's own statement.

    The provider ``mailbox_message_id`` is the idempotency key; ``sender_address``
    is the address the decision read, and the two anchors are what a reply is
    later matched by — either may be absent, and absence matches nothing.

    ``created`` is ``False`` when the message already had a case: the
    existing row is returned as it stands, no second audit entry is written,
    and the caller reports the same case the first examination opened. The
    refusal the unique index raises is confined to a savepoint, so the
    caller's transaction survives it — the overlapping-cycle case — and an
    ``IntegrityError`` the index cannot explain (no row to re-read) is
    re-raised.
    """
    if isinstance(action, UnthreadedAction):
        if company_id is not None:
            raise ValueError("the unthreaded asks open company-less cases; got a company")
        try:
            status, ask_kind = _UNTHREADED_OPEN_STATUS[action]
        except KeyError:
            raise ValueError(
                f"open_case is for the two unthreaded asks, got {action!r}"
            ) from None
    else:
        if company_id is None:
            raise ValueError("the threaded asks need the chain's company; got none")
        try:
            status, ask_kind = _OPEN_STATUS[action]
        except KeyError:
            raise ValueError(f"open_case is for the two asks, got {action!r}") from None
    try:
        # The savepoint keeps a refused insert from poisoning the caller's
        # transaction: the loser of the race has its answer (the winner's row)
        # rather than a dead session.
        async with session.begin_nested():
            case = await create_case(
                session,
                tenant_id=tenant_id,
                company_id=company_id,
                mailbox_message_id=mailbox_message_id,
                sender_address=sender_address,
                status=status,
                thread_id=thread_id,
                original_rfc_message_id=original_rfc_message_id,
                ask_kind=ask_kind,
            )
    except IntegrityError:
        existing = await _case_for_message(
            session, tenant_id=tenant_id, mailbox_message_id=mailbox_message_id
        )
        if existing is None:
            raise  # the unique index is not what refused this insert
        return existing, False
    await audit.record_event(
        session,
        tenant_id=tenant_id,
        action=AuditAction.ATTRIBUTION_CASE_OPENED,
        actor_user_id=None,
        company_id=company_id,
        entity_type="attribution_case",
        entity_id=case.id,
        details={
            "case_id": str(case.id),
            "sender_address": sender_address,
            "status": status.value,
        },
    )
    return case, True


async def _case_for_message(
    session: AsyncSession, *, tenant_id: UUID, mailbox_message_id: str
) -> AttributionCase | None:
    """The case one provider message id already opened, if any.

    The read behind the idempotency answer: the same key the unique index
    holds, read after a refused insert to return the row that refused it. A
    row already in this session's identity map is found by the same query;
    no other tenant's row ever is (the read is tenant-scoped).
    """
    found: AttributionCase | None = await session.scalar(
        select(AttributionCase).where(
            AttributionCase.tenant_id == tenant_id,
            AttributionCase.mailbox_message_id == mailbox_message_id,
        )
    )
    return found


# --- The confirming act, processing, and the 2.1 closure -----------------------

# The bounded codes that travel with the two ends of a case. ``PROCESS_CASE_NOT_OPEN``
# is ``process_case``'s refusal for a case that has left the awaits (the staff
# surface's ``process_open_case`` answers it with its own 409, the guard behind
# its locked read); the three ASK_CANCELLED_* values are the reasons written on
# the queued ask row the settle cancels.
PROCESS_CASE_NOT_OPEN = "process_case_not_open"
ASK_CANCELLED_ON_RESOLVE = "case_resolved"
ASK_CANCELLED_ON_CLOSE = "case_closed"
ASK_CANCELLED_ON_DISMISS = "case_dismissed"

# The clarification loop's own codes. ``ASK_LIMIT_REACHED`` is the two-ask
# bound (ruling D7: one ask, one re-ask, then never again); ``ANSWER_UNRESOLVED``
# is the answer grammar's zero-or-several verdict (ruling D6 — staff's case,
# several flagged as the future ③ handoff). (``letter_not_registrable`` —
# the ② wall's refusal — retired with migration 0035: the registry widening
# removed the wall, and no live path or suite references the string.)
ASK_LIMIT_REACHED = "ask_limit_reached"
ANSWER_UNRESOLVED = "answer_unresolved"

# Ruling D7's ceiling: one ask and one re-ask per case, then never again.
_MAX_ASKS_PER_CASE = 2

# The prefix a mailbox read failure is reported under — the message itself
# could not be produced, so nothing of it was processed and the case waits.
_FETCH_FAILED = "fetch_failed"


@dataclass(frozen=True)
class ProcessReport:
    """What ``process_case`` reached — the case, its outcome, its exchange.

    ``outcome`` is the ingestion tail's verdict for the original
    (``app.domain.communications.IngestOutcome``), never a message's content:
    ``ingested`` for a fresh landing, ``skipped_known`` when another road had
    already recorded it, ``denied``/``failed`` for the two ways the tail
    refuses. ``resolved`` says whether the case moved to ``resolved`` — which
    it does exactly when a ``Communication`` for the original exists — and
    ``communication_id`` names that exchange. ``reason`` carries the bounded
    code of a refusal, and is ``None`` on success.
    """

    case_id: UUID
    # NULL for a company-less case (the clarification loop's A/B classes,
    # migration 0034) — a refusal report for one names no company.
    company_id: UUID | None
    outcome: IngestOutcome
    resolved: bool
    reason: str | None = None
    communication_id: UUID | None = None


async def confirm_case(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    case: AttributionCase,
    confirmation_communication_id: UUID | None = None,
) -> bool:
    """Record the authorising confirmation on one case (ruling R5); applied?

    The case must still be ``awaiting_confirmation``: that awaiting state is
    the guard, and it is what makes the act idempotent — a case that has left
    the awaits (resolved by its processing, closed by R7, dismissed by staff)
    is never re-confirmed and has nothing left to record. Returns whether the
    act was recorded.

    ``confirmation_communication_id`` is the stored confirming exchange —
    ``None`` when the §5.1 gate kept that reply out of the registry, in which
    case the pointer stays NULL while the act itself is still recorded: it is
    a fact about authority, and the caller writes the entry that names it.
    The status is deliberately left where it is. The confirmation authorises
    processing; it is not the processing. Only ``process_case`` moves the
    case, so a processing the gate denies leaves it awaiting for staff
    instead of looking settled.
    """
    if case.status != AttributionCaseStatus.AWAITING_CONFIRMATION.value:
        return False
    recorded = await set_confirmation(
        session,
        tenant_id=tenant_id,
        case_id=case.id,
        communication_id=confirmation_communication_id,
    )
    return recorded is not None


async def process_case(
    session: AsyncSession,
    store: DocumentStore,
    scanner: Scanner,
    *,
    tenant_id: UUID,
    case: AttributionCase,
    actor_user_id: UUID | None = None,
    now: datetime | None = None,
) -> ProcessReport:
    """Process one case's original message, and resolve the case (ruling R6).

    The ingestion tail re-runs for ``case.mailbox_message_id`` with the
    company fixed to the case's own and nothing else of the original's
    attribution re-decided — the case is the decision. The original's
    envelope is read again (its receipt time and thread headers are what the
    ``Communication`` row carries), so the mailbox is resolved here like any
    other read: the same leg the ingestion cycle used.

    Success is a ``Communication`` for the original — written by this call,
    or already there because another road (a §2.5 retry after the sender was
    registered) recorded it — and it moves the case to ``resolved`` with
    ``resolved_communication_id``, cancelling the queued ask (a settled case
    is never asked). A §5.1 gate denial, an unusable raw message, or a
    mailbox that answered and could not produce this message returns the
    bounded reason inside the report and changes nothing: the case stays
    awaiting, and staff can process it again. ``actor_user_id`` is the staff
    member a forced processing acts as (the tail's audit entries carry it);
    ``None`` is the system actor the confirmation path runs under.
    """
    at = now if now is not None else datetime.now(UTC)
    if case.status not in _OPEN_STATUS_VALUES:
        return ProcessReport(
            case_id=case.id,
            company_id=case.company_id,
            outcome=IngestOutcome.SKIPPED_KNOWN,
            resolved=False,
            reason=PROCESS_CASE_NOT_OPEN,
        )
    # The ingestion tail runs per company; ①'s open cases all carry one
    # (workstream ②'s company-less asks refuse this road — D9's owner).
    assert case.company_id is not None
    # A function-level import: ``app.application.mail_ingest`` imports this
    # module at module level (the case lifecycle is what its attribution step
    # calls), so reaching back for the ingestion tail from inside the function
    # keeps the two services' import graph acyclic.
    from app.application import mail_ingest

    try:
        outcome = await mail_ingest.ingest_case_message(
            session,
            store,
            scanner,
            at=at,
            tenant_id=tenant_id,
            company_id=case.company_id,
            mailbox_message_id=case.mailbox_message_id,
            actor_user_id=actor_user_id,
        )
    except (MailProviderRefused, MailProviderUnavailable, MailResponseInvalid) as exc:
        # The mailbox answered and could not produce this message: an outcome,
        # not an abort, because one unreachable original must not cost the
        # confirmation that authorises it (the act's own message is stored and
        # its cases still resolve or wait).
        return ProcessReport(
            case_id=case.id,
            company_id=case.company_id,
            outcome=IngestOutcome.FAILED,
            resolved=False,
            reason=_fetch_failed_reason(exc),
        )
    if outcome.communication_id is None:
        # Nothing of the original reached the records registry: the gate
        # denied, its bytes were unusable, or nothing could be read for it.
        return ProcessReport(
            case_id=case.id,
            company_id=case.company_id,
            outcome=outcome.outcome,
            resolved=False,
            reason=outcome.reason,
        )
    moved = await set_status(
        session,
        tenant_id=tenant_id,
        case_id=case.id,
        status=AttributionCaseStatus.RESOLVED,
        resolved_communication_id=outcome.communication_id,
    )
    if moved is None:
        return ProcessReport(
            case_id=case.id,
            company_id=case.company_id,
            outcome=outcome.outcome,
            resolved=False,
            reason=PROCESS_CASE_NOT_OPEN,
        )
    await _cancel_pending_ask(
        session, tenant_id=tenant_id, case_id=case.id, reason=ASK_CANCELLED_ON_RESOLVE
    )
    return ProcessReport(
        case_id=case.id,
        company_id=case.company_id,
        outcome=outcome.outcome,
        resolved=True,
        communication_id=outcome.communication_id,
    )


@dataclass(frozen=True)
class ClarificationResolution:
    """What the answer grammar's verdict became (ruling D6) — coordinates only.

    ``resolved`` is true exactly when the case moved: the first line named
    exactly one of the sender's companies, the case took it (``company_id``)
    and the ingestion tail turned the parked original into the exchange
    ``communication_id`` names. ``reason`` carries the bounded code otherwise —
    ``answer_unresolved`` (zero or several companies named: staff's case) or
    the tail's own refusal (the §5.1 gate, an unusable original,
    ``fetch_failed:<reason>``) when the answer named a company but the tail
    could not land it; the case then stays awaiting with its company stamped,
    ready for the staff surface's retry.
    """

    case_id: UUID
    resolved: bool
    company_id: UUID | None = None
    communication_id: UUID | None = None
    reason: str | None = None
    # Workstream ③'s E2: the answer named several of the sender's companies.
    # The case closes with the multi-company handoff named in its audit (the
    # company column stays NULL — the handoff is the record), and the caller
    # hands the parked original and this set to the split.
    handoff: bool = False
    matched_company_ids: tuple[UUID, ...] = ()


async def resolve_clarification_case(
    session: AsyncSession,
    store: DocumentStore,
    scanner: Scanner,
    *,
    tenant_id: UUID,
    case: AttributionCase,
    first_line: str,
    companies: Sequence[CompanyReference],
    actor_user_id: UUID | None = None,
    now: datetime | None = None,
) -> ClarificationResolution:
    """Resolve one A-class case from its sender's answer (ruling D6).

    ``first_line`` is the answer's first non-empty body line — read by the
    caller, just in time, the way ① reads the confirm grammar; ``companies``
    is the sender's own registered company references (contacts plus master
    data), resolved by the caller. ``match_company_reference`` decides:
    exactly one match stamps the company on the case (the composite foreign
    key pins the pair, and everything the tail writes afterwards carries it)
    and re-runs the ingestion tail for the parked original — ① R6's own
    ``process_case``, one code path — moving the case to ``resolved`` with the
    exchange; one ``attribution_case_resolved`` entry names the act. Zero
    matches change nothing and stay awaiting — the caller may then drive the
    bounded re-ask (ruling D7). Several matches close the case with the
    multi-company handoff named in its audit: the parked original hands to
    the split, and the caller drives it with the matched set. A tail refusal
    leaves the company stamped and the case awaiting: the answer was real,
    and the staff surface's retry is its road.
    """
    matched_set = match_company_references(first_line, companies)
    if len(matched_set) >= 2:
        moved = await set_status(
            session,
            tenant_id=tenant_id,
            case_id=case.id,
            status=AttributionCaseStatus.CLOSED,
            at=now if now is not None else datetime.now(UTC),
        )
        if moved is None:
            return ClarificationResolution(
                case_id=case.id, resolved=False, reason=PROCESS_CASE_NOT_OPEN
            )
        await _cancel_pending_ask(
            session, tenant_id=tenant_id, case_id=case.id, reason=ASK_CANCELLED_ON_CLOSE
        )
        await audit.record_event(
            session,
            tenant_id=tenant_id,
            action=AuditAction.ATTRIBUTION_CASE_CLOSED,
            actor_user_id=None,
            company_id=None,
            details={
                "case_ids": [str(case.id)],
                "handoff": "multi_company_split",
                "matched_company_ids": [str(company_id) for company_id in sorted(matched_set)],
            },
        )
        return ClarificationResolution(
            case_id=case.id,
            resolved=False,
            handoff=True,
            matched_company_ids=tuple(sorted(matched_set)),
        )
    matched = next(iter(matched_set), None)
    if matched is None:
        return ClarificationResolution(case_id=case.id, resolved=False, reason=ANSWER_UNRESOLVED)
    stamped = await set_case_company(
        session, tenant_id=tenant_id, case_id=case.id, company_id=matched
    )
    if stamped is None:
        # The caller read the case in this tenant; a None here is a race with
        # a deletion this schema never performs — refused as the closed case.
        return ClarificationResolution(
            case_id=case.id, resolved=False, reason=PROCESS_CASE_NOT_OPEN
        )
    report = await process_case(
        session,
        store,
        scanner,
        tenant_id=tenant_id,
        case=stamped,
        actor_user_id=actor_user_id,
        now=now,
    )
    if not report.resolved:
        return ClarificationResolution(
            case_id=case.id,
            resolved=False,
            company_id=matched,
            reason=report.reason,
        )
    assert report.communication_id is not None  # resolved cases name their exchange
    await audit.record_event(
        session,
        tenant_id=tenant_id,
        action=AuditAction.ATTRIBUTION_CASE_RESOLVED,
        actor_user_id=actor_user_id,
        company_id=matched,
        entity_type="attribution_case",
        entity_id=case.id,
        details={
            "case_id": str(case.id),
            "communication_id": str(report.communication_id),
            "actor_user_id": None if actor_user_id is None else str(actor_user_id),
        },
    )
    return ClarificationResolution(
        case_id=case.id,
        resolved=True,
        company_id=matched,
        communication_id=report.communication_id,
    )


async def close_registration_cases(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    cases: Sequence[AttributionCase],
    now: datetime | None = None,
) -> tuple[AttributionCase, ...]:
    """Close the 2.1 cases a registered channel's message answered (ruling R7).

    ``cases`` is the caller's read of the open cases on one thread anchor, one
    company's — for example what ``open_cases_for_thread`` returned for the
    message a registered channel sent. Among them, exactly the
    ``awaiting_registered_channel`` ones close: their remedy (an instruction
    from a registered channel) has arrived, and that message itself is the
    instruction, so the parked original is not processed. Cases awaiting a
    confirmation and cases that have settled since are left exactly as they
    are, which is also what makes a second call a no-op: nothing closes, and
    nothing is written.

    **The caller runs this only for a message that did not act as the
    confirmation** (ruling M2). The act attempts every open awaiting case on
    the anchor, so closure behind it would close a case whose processing the
    gate denied — exactly the case that must stay awaiting for the staff
    surface's retry (ruling R6); with the act's path skipped, the closure sees
    only cases nothing has tried.

    Each closed case moves to ``closed`` with the caller's instant, and its
    queued ask is cancelled — a letter asking for an instruction must not
    reach the client after the instruction arrived. One
    ``attribution_case_closed`` entry names the ids that closed: one message
    brought the remedy, so one entry records it. Returns the rows that
    closed, oldest first as the caller listed them.
    """
    at = now if now is not None else datetime.now(UTC)
    closed: list[AttributionCase] = []
    for case in cases:
        if case.status != AttributionCaseStatus.AWAITING_REGISTERED_CHANNEL.value:
            continue
        moved = await set_status(
            session,
            tenant_id=tenant_id,
            case_id=case.id,
            status=AttributionCaseStatus.CLOSED,
            at=at,
        )
        if moved is None:
            continue
        await _cancel_pending_ask(
            session, tenant_id=tenant_id, case_id=case.id, reason=ASK_CANCELLED_ON_CLOSE
        )
        closed.append(moved)
    if not closed:
        return ()
    await audit.record_event(
        session,
        tenant_id=tenant_id,
        action=AuditAction.ATTRIBUTION_CASE_CLOSED,
        actor_user_id=None,
        company_id=closed[0].company_id,
        details={"case_ids": [str(case.id) for case in closed]},
    )
    return tuple(closed)


async def close_first_contact_cases(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    cases: Sequence[AttributionCase],
    now: datetime | None = None,
) -> tuple[AttributionCase, ...]:
    """Close the B-class cases a registered channel's message answered (D8).

    The company-less variant of the 2.1 closure: the caller's read is the
    tenant-scoped open cases on one anchor (``open_cases_for_anchor``), and
    among them exactly the **company-less** ``awaiting_registered_channel``
    ones close — the sender arrived through a registered address, which is the
    remedy Letter B asked for. Company-bound 2.1 cases on the same read are
    ①'s: their remedy is the chain's own registered channel, and
    ``close_registration_cases`` is the only road that closes them. Each closed
    case moves to ``closed`` with the caller's instant, its queued ask is
    cancelled (a letter asking for a registered channel must not reach the
    client after one arrived), and one ``attribution_case_closed`` entry —
    with ``company_id = NULL``, the D10 rule for company-less events — names
    the ids: one message brought the remedy, so one entry records it. Cases
    that awaited something else, and cases that have settled since, are left
    exactly as they are; a second call closes nothing and writes nothing. The
    closing message itself is the caller's to process — this function never
    reads it.
    """
    at = now if now is not None else datetime.now(UTC)
    closed: list[AttributionCase] = []
    for case in cases:
        if case.status != AttributionCaseStatus.AWAITING_REGISTERED_CHANNEL.value:
            continue
        if case.company_id is not None:
            continue
        moved = await set_status(
            session,
            tenant_id=tenant_id,
            case_id=case.id,
            status=AttributionCaseStatus.CLOSED,
            at=at,
        )
        if moved is None:
            continue
        await _cancel_pending_ask(
            session, tenant_id=tenant_id, case_id=case.id, reason=ASK_CANCELLED_ON_CLOSE
        )
        closed.append(moved)
    if not closed:
        return ()
    await audit.record_event(
        session,
        tenant_id=tenant_id,
        action=AuditAction.ATTRIBUTION_CASE_CLOSED,
        actor_user_id=None,
        company_id=None,
        details={"case_ids": [str(case.id) for case in closed]},
    )
    return tuple(closed)


async def _cancel_pending_ask(
    session: AsyncSession, *, tenant_id: UUID, case_id: UUID, reason: str
) -> UUID | None:
    """Cancel the case's queued ask if one is still pending; its row id else.

    The I2b rule the two settles share: a case that has ended must not have
    its ask delivered afterwards — the letter asks for something already
    answered, and an ask arriving after the fact reads as the deployment
    ignoring its own correspondence. ``outbox.cancel`` moves the row to
    ``cancelled`` with the bounded reason beside it; a row already delivered
    or failed is not this function's to move, and the pending read does not
    return one.
    """
    row = await outbox.pending_attribution_ask(session, tenant_id=tenant_id, case_id=case_id)
    if row is None:
        return None
    await outbox.cancel(session, job=row, reason=reason)
    return row.id


def _fetch_failed_reason(exc: Exception) -> str:
    """One bounded code for a mailbox that answered and could not produce a
    message: the leg's own reason, or the unavailability it names.

    ``MailProviderRefused``/``MailResponseInvalid`` carry a short provider
    code (never a body, the adapters' rule); ``MailProviderUnavailable``
    carries none, so its own name is the reason.
    """
    reason = getattr(exc, "reason", None)
    if isinstance(reason, str) and reason:
        return f"{_FETCH_FAILED}:{reason}"
    return _FETCH_FAILED


# --- The staff surface: the list, the dismissal, the forced processing --------
#
# What ``app.api.attribution``'s routes call. The list is the read the queue is
# rendered from; the two actions settle a case the automated roads have not —
# a dismissal is staff saying the case needs no further action, and the forced
# processing is staff working the original through the ingestion tail by hand.
# Both read the row under the same lock and refuse every terminal state with
# the same typed error, so the two cannot disagree about a case either of them
# could have settled.


class AttributionCaseNotFoundError(Exception):
    """No case with that id in the actor's tenant."""


class AttributionCaseNotOpenError(Exception):
    """The case has left the awaits — resolved, closed or dismissed."""


class AttributionCaseHasNoCompanyError(Exception):
    """The case is awaiting with no company (ruling D9): staff cannot
    force-process what has no company — the road into the ingestion tail is
    per company — and the route answers this with 409
    ``attribution_case_has_no_company``."""


@dataclass(frozen=True)
class CaseProcessResult:
    """One staff-forced processing: the case as it now stands, and the report.

    ``case`` is the row after the attempt — ``resolved`` with the exchange it
    became on success, still in its await on a refusal — and ``report`` is the
    ingestion tail's verdict (``ProcessReport``). The two travel together
    because the surface answers both: the status a reviewer reads and the
    bounded reason a refusal carries.
    """

    case: AttributionCase
    report: ProcessReport


async def list_cases(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    statuses: Sequence[AttributionCaseStatus] | None = None,
) -> list[AttributionCase]:
    """The tenant's cases, newest first, optionally one or more statuses.

    ``None`` is every row (the surface's ``all``); one status is the
    adapter's own read; more than one — the surface's ``awaiting``, both open
    states as one queue — is one read per status merged into the adapter's
    order (``created_at`` descending, the id as the tie-break), so the mix is
    ordered as a single query would have ordered it.

    The reads are the adapter's and stay tenant-scoped; this function exists
    so the surface speaks its own vocabulary — a set of statuses, because
    "awaiting" is two of them — rather than the table's.
    """
    if statuses is None:
        return await list_case_rows(session, tenant_id=tenant_id)
    if len(statuses) == 1:
        return await list_case_rows(session, tenant_id=tenant_id, status=statuses[0])
    rows: list[AttributionCase] = []
    for status in statuses:
        rows.extend(await list_case_rows(session, tenant_id=tenant_id, status=status))
    # Two stable passes for the mixed direction: the id ascending, then
    # ``created_at`` descending (stability keeps the id order within an
    # instant, which is the adapter's own tie-break).
    rows.sort(key=lambda case: case.id)
    rows.sort(key=lambda case: case.created_at, reverse=True)
    return rows


async def dismiss_case(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    case_id: UUID,
    actor_user_id: UUID | None = None,
    now: datetime | None = None,
) -> AttributionCase:
    """Close one open case staff have decided needs no further action.

    The row moves to ``dismissed`` with the caller's instant, its queued ask —
    when one is still pending — is cancelled (a settled case is never asked,
    the I2b rule the other settles share), and one
    ``attribution_case_dismissed`` entry names the case and the actor. Nothing
    is read from the mailbox and nothing is stored: the dismissal is a
    statement about the row, and the row keeps its sender, its anchors and its
    ask kind as the history of why it existed.

    Only an open case can be dismissed. The **locked read** is what makes the
    guard hold against a concurrent settle — the loser of two overlapping
    dismissals reads ``dismissed`` and is refused rather than appending a
    second entry to the append-only chain. ``AttributionCaseNotFoundError``
    for an unknown id **and** for another tenant's (deliberately one answer),
    and ``AttributionCaseNotOpenError`` for a case that has left the awaits.
    """
    at = now if now is not None else datetime.now(UTC)
    case = await _locked_case(session, tenant_id=tenant_id, case_id=case_id)
    if case is None:
        raise AttributionCaseNotFoundError
    if case.status not in _OPEN_STATUS_VALUES:
        raise AttributionCaseNotOpenError
    moved = await set_status(
        session,
        tenant_id=tenant_id,
        case_id=case.id,
        status=AttributionCaseStatus.DISMISSED,
        at=at,
    )
    assert moved is not None  # the locked read found it in this tenant
    await _cancel_pending_ask(
        session, tenant_id=tenant_id, case_id=case.id, reason=ASK_CANCELLED_ON_DISMISS
    )
    await audit.record_event(
        session,
        tenant_id=tenant_id,
        action=AuditAction.ATTRIBUTION_CASE_DISMISSED,
        actor_user_id=actor_user_id,
        company_id=case.company_id,
        entity_type="attribution_case",
        entity_id=case.id,
        details={"case_id": str(case.id)},
    )
    await session.flush()
    return moved


async def process_open_case(
    session: AsyncSession,
    store: DocumentStore,
    scanner: Scanner,
    *,
    tenant_id: UUID,
    case_id: UUID,
    actor_user_id: UUID | None = None,
    now: datetime | None = None,
) -> CaseProcessResult:
    """Process one open case's original at a staff member's press (ruling R6).

    The surface's forced processing: ``process_case``'s own tail, reached by
    the case's id rather than through the thread — the road back for a case
    whose confirmation the gate denied, or whose original the mailbox could
    not produce, and which would otherwise wait for ever.

    The **locked read** is the guard: an unknown or another tenant's id is
    ``AttributionCaseNotFoundError`` (the route's 404), a case that has left
    the awaits is ``AttributionCaseNotOpenError`` (the route's 409
    ``already_closed``), and an open **company-less** case is
    ``AttributionCaseHasNoCompanyError`` (the route's 409
    ``attribution_case_has_no_company``, ruling D9) — all raised before the
    mailbox is touched, so a settled or company-less case costs no provider
    call. A successful processing writes one ``attribution_case_resolved``
    entry naming the case, the exchange it resolved to and the acting member:
    it is the staff act's own record, so it is written here and not by
    ``process_case``, whose other callers record their own acts instead. A refusal
    — the §5.1 gate's own reason, an unusable original,
    ``fetch_failed:<reason>`` — changes nothing and writes no entry: the case
    stays awaiting, and staff can process it again.

    The mailbox's own connection failures (``MailNotConnected``,
    ``MailRefreshFailed``) propagate unchanged, exactly as they do out of the
    ingestion trigger; the route maps them to the same 409/502 pair.
    """
    at = now if now is not None else datetime.now(UTC)
    case = await _locked_case(session, tenant_id=tenant_id, case_id=case_id)
    if case is None:
        raise AttributionCaseNotFoundError
    if case.status not in _OPEN_STATUS_VALUES:
        raise AttributionCaseNotOpenError
    if case.company_id is None:
        # Ruling D9: the forced processing runs the ingestion tail per
        # company, and an awaiting company-less case has none to run it for.
        raise AttributionCaseHasNoCompanyError
    report = await process_case(
        session,
        store,
        scanner,
        tenant_id=tenant_id,
        case=case,
        actor_user_id=actor_user_id,
        now=at,
    )
    if not report.resolved and report.reason == PROCESS_CASE_NOT_OPEN:
        # ``process_case``'s internal guard, which the locked read normally
        # makes unreachable: another writer settled the row between the read
        # and the attempt. Refused as the closed case it now is.
        raise AttributionCaseNotOpenError
    if report.resolved:
        await audit.record_event(
            session,
            tenant_id=tenant_id,
            action=AuditAction.ATTRIBUTION_CASE_RESOLVED,
            actor_user_id=actor_user_id,
            company_id=case.company_id,
            entity_type="attribution_case",
            entity_id=case.id,
            details={
                "case_id": str(case.id),
                "communication_id": str(report.communication_id),
                "actor_user_id": (
                    None if actor_user_id is None else str(actor_user_id)
                ),
            },
        )
        await session.flush()
    return CaseProcessResult(case=case, report=report)


async def _locked_case(
    session: AsyncSession, *, tenant_id: UUID, case_id: UUID
) -> AttributionCase | None:
    """One case by id, tenant-scoped and row-locked — or ``None``.

    The two staff actions' shared door: the lock serializes overlapping
    settles, so the loser of a race reads the settled status and is refused
    rather than appending a second entry to the append-only chain (the
    ``quarantine.dismiss`` rule). ``None`` is an unknown id **and** another
    tenant's alike; the callers answer both with one not-found.
    """
    locked: AttributionCase | None = await session.scalar(
        select(AttributionCase)
        .where(
            AttributionCase.id == case_id,
            AttributionCase.tenant_id == tenant_id,
        )
        .with_for_update()
    )
    return locked


# --- The ask: its guards, its letter, and its enqueue --------------------------

# The bounded reasons an ask did not leave — the report's vocabulary, named
# here so the tests and any later surface read one spelling. Nothing switches
# on them: each is a fact about why a case is still waiting for its ask.
ASK_SUPPRESSED = "ask_suppressed"  # an ask already left this thread (R3) or anchor (D7)
ASK_GATED = "ask_gated"  # the company is not licensed/covered for ingestion
ASK_NOT_ALLOWED = "ask_not_allowed"  # the anti-abuse headers (R4)
ASK_CAP_REACHED = "ask_cap_reached"  # the pool's hourly ceiling (R4/D7)
ASK_CASE_NOT_OPEN = "ask_case_not_open"  # the case has left the awaits

# The status values an ask may be enqueued for: the adapter's three awaits, as
# values, because the case row carries the status as a string.
_OPEN_STATUS_VALUES = frozenset(member.value for member in OPEN_STATUSES)


class AttributionAskError(Exception):
    """The ask's letter could not be composed; ``reason`` is the bounded code.

    Two reasons: ``company_name_unavailable`` (scope §7 — every client-facing
    message identifies the company by name and number, and a company with no
    name in force is not written to) and ``mail_not_configured`` (no mailbox
    address to write from, so the letter could not go out as the TCSP's own).
    The enqueue contains both: a letter that cannot compose is a suppressed
    ask and a still-open case, never a failed cycle.
    """

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class AskReport:
    """What ``enqueue_ask`` decided — coordinates only.

    ``enqueued`` is True when a pending ``attribution_ask`` row now carries
    the ask: ``outbox_id`` and ``document_id`` name it, and ``met_pending_row``
    says this call met a row already pending for the case (the winner's letter
    is what the row will deliver, and ``document_id`` is ``None`` — the fresh
    document is registered but the row will not carry it). ``reason`` is the
    bounded code for an ask that did not go out — ``ask_case_not_open``,
    ``ask_suppressed``, ``ask_gated``, ``ask_not_allowed``, ``ask_cap_reached``,
    ``ask_limit_reached`` (the two-ask bound), ``company_name_unavailable``
    or ``mail_not_configured`` — and ``None`` when it did. No subject, no
    recipient and no body travels here: the stored ``.eml`` is the letter's
    only home.
    """

    enqueued: bool
    reason: str | None = None
    outbox_id: UUID | None = None
    document_id: UUID | None = None
    met_pending_row: bool = False


def ask_allowed(*, auto_submitted: str | None, return_path: str | None) -> bool:
    """Ruling R4's two metadata-only anti-abuse guards.

    An original that announces itself machine-generated is not answered with a
    machine-generated reply — the loop this guard exists to break — and a
    ``Return-Path`` carrying no address (``<>``, the null reverse-path bounces
    and backscatter are sent with) is not answered at all. Both facts come
    from the envelope (``app.domain.mail.MessageMetadata``); nothing here
    reads the message. ``Auto-Submitted`` suppresses on any value it carries,
    including the standard's explicit ``no``: the guard deliberately leans on
    the safe side, at the cost of not answering a correspondent who announced
    they are not a robot.
    """
    if (auto_submitted or "").strip():
        return False
    if return_path is None:
        return True
    content = return_path.strip()
    if content.startswith("<") and content.endswith(">"):
        content = content[1:-1]
    return bool(content.strip())


async def enqueue_ask(
    session: AsyncSession,
    store: DocumentStore,
    scanner: Scanner,
    *,
    tenant_id: UUID,
    case: AttributionCase,
    recipients: Sequence[str],
    rfc_message_ids: Sequence[str],
    original_subject: str,
    original_received_at: datetime,
    auto_submitted: str | None,
    return_path: str | None,
    now: datetime | None = None,
) -> AskReport:
    """Decide this case's ask and enqueue it, or keep it off.

    Company-bound cases carry ①'s guard order (the module docstring's list:
    the case's own state, suppression, the §5.1 gate, the anti-abuse headers,
    the authority asks' hourly cap). A company-less case is routed to the
    clarification loop's own guard set — no §5.1 gate, anchor suppression, the
    anti-abuse headers, the convenience asks' cap, the two-ask bound — and its
    letter stops at the records registry's company-less refusal; both roads
    answer the same ``AskReport``.

    ``case`` is the row ``open_case`` just wrote (or found): its company, its
    ask kind and its two anchors are the ask's coordinates. ``recipients`` are
    the ask's To addresses as the caller resolved them — the registered
    channels ``decide`` returned for the confirmation ask, or the sender for
    the registered-channel ask — and ``original_subject``/
    ``original_received_at`` are the envelope facts the confirmation letter
    quotes (ruling R2). ``auto_submitted``/``return_path`` are the original's
    two anti-abuse headers, read from the metadata the ingestion cycle
    already holds. ``rfc_message_ids`` are the tokens the original's own
    ``In-Reply-To``/``References`` carry — its place on the chain; the case's
    ``original_rfc_message_id`` is merged in here (deduped), so both a
    re-examination of the same message and any descendant on the chain meet
    the case the suppression read must see.

    A case that has left the two awaits is never asked again: the cursor's
    overlap re-reads the same provider message id, ``open_case`` returns the
    terminal row as it stands, and this call refuses rather than reopening a
    settled case with a fresh ask. An ask that goes out is one transaction's
    work: the stored letter, the request-less outbox row, the case's
    ``ask_sent_at`` stamp and one ``attribution_ask_enqueued`` entry. Every
    refusal answers an ``AskReport`` with its bounded reason and writes
    nothing — an awaiting case stays open with ``ask_sent_at`` NULL, which is
    the R3 posture: visible to staff, never silently closed.
    """
    at = now if now is not None else datetime.now(UTC)
    if case.status not in _OPEN_STATUS_VALUES:
        return AskReport(enqueued=False, reason=ASK_CASE_NOT_OPEN)
    if case.company_id is None:
        return await _enqueue_company_less_ask(
            session,
            store,
            scanner,
            at=at,
            tenant_id=tenant_id,
            case=case,
            recipients=recipients,
            rfc_message_ids=rfc_message_ids,
            original_subject=original_subject,
            original_received_at=original_received_at,
            auto_submitted=auto_submitted,
            return_path=return_path,
        )
    anchors = list(
        dict.fromkeys(
            anchor
            for anchor in (case.original_rfc_message_id, *rfc_message_ids)
            if anchor
        )
    )
    if (
        await latest_ask_for_thread(
            session,
            tenant_id=tenant_id,
            company_id=case.company_id,
            thread_id=case.thread_id,
            rfc_message_ids=anchors,
        )
        is not None
    ):
        return AskReport(enqueued=False, reason=ASK_SUPPRESSED)
    decision = await service_packages.check_automation(
        session,
        tenant_id=tenant_id,
        company_id=case.company_id,
        automation_type=AutomationType.EMAIL_INGESTION,
        as_of=at.date(),
    )
    if not decision.allowed:
        # The ruling's "ask gated on EMAIL_INGESTION": no ask, the case stays
        # open, and nothing is recorded as an enqueue.
        return AskReport(enqueued=False, reason=ASK_GATED)
    if not ask_allowed(auto_submitted=auto_submitted, return_path=return_path):
        return AskReport(enqueued=False, reason=ASK_NOT_ALLOWED)
    if (
        await count_asks_since(
            session, tenant_id=tenant_id, since=at - timedelta(hours=1), company_less=False
        )
        >= settings.attribution_ask_hourly_cap
    ):
        return AskReport(enqueued=False, reason=ASK_CAP_REACHED)
    if case.ask_kind is None:
        # ``open_case`` always writes the kind for the two asks; a case without
        # one is not something this function can compose a letter for.
        raise ValueError("enqueue_ask needs the case's ask kind; open_case writes one")
    kind = AttributionAskKind(case.ask_kind)
    try:
        document = await _compose_and_store(
            session,
            store,
            scanner,
            tenant_id=tenant_id,
            case=case,
            kind=kind,
            recipients=recipients,
            original_subject=original_subject,
            original_received_at=original_received_at,
            at=at,
        )
    except AttributionAskError as exc:
        # Failures are outcomes, never aborts: the cycle that opened the case
        # finishes its other messages, and the case stays open for staff.
        return AskReport(enqueued=False, reason=exc.reason)
    enqueued = await outbox.enqueue_attribution_ask(
        session,
        tenant_id=tenant_id,
        company_id=case.company_id,
        case_id=case.id,
        letter_document_id=document.id,
    )
    row = enqueued.row
    met_pending_row = not enqueued.created
    # The audit names the letter the row will actually deliver: the winner's
    # on a met pending row, the fresh one otherwise — never the other pairing.
    delivered_document_id = row.letter_document_id if met_pending_row else document.id
    assert delivered_document_id is not None  # an ask row names its letter
    await set_ask_sent(session, tenant_id=tenant_id, case_id=case.id, at=at)
    await audit.record_event(
        session,
        tenant_id=tenant_id,
        action=AuditAction.ATTRIBUTION_ASK_ENQUEUED,
        actor_user_id=None,
        company_id=case.company_id,
        entity_type="attribution_case",
        entity_id=case.id,
        details={
            "case_id": str(case.id),
            "ask_kind": kind.value,
            "outbox_id": str(row.id),
            "document_id": str(delivered_document_id),
            "destination": outbox.CONFIRMATION_DESTINATION.value,
            "met_pending_row": met_pending_row,
        },
    )
    await session.flush()
    return AskReport(
        enqueued=True,
        outbox_id=row.id,
        document_id=None if met_pending_row else document.id,
        met_pending_row=met_pending_row,
    )


async def _enqueue_company_less_ask(
    session: AsyncSession,
    store: DocumentStore,
    scanner: Scanner,
    *,
    at: datetime,
    tenant_id: UUID,
    case: AttributionCase,
    recipients: Sequence[str],
    rfc_message_ids: Sequence[str],
    original_subject: str,
    original_received_at: datetime,
    auto_submitted: str | None,
    return_path: str | None,
) -> AskReport:
    """The clarification loop's company-less enqueue — ②'s guard set (D3/D7).

    ``enqueue_ask``'s composition variant, not a second sender: no §5.1 gate
    (a company-less case has none, and gating the quarantine replacement on
    paid automation would regress Free tenants — ruling D3); anchor
    suppression (one ask per (tenant, sender, anchor)); ①'s anti-abuse headers
    unchanged; the convenience asks' own hourly cap, counted over the
    company-less pool so neither pool can starve the other; and the two-ask
    bound (one re-ask on a later unresolvable message, then never again). The
    letter composes in full — the sender alone is the recipient (ruling D8),
    the subject leads with the TCSP's own name (C10) — registers through the
    single records-registry seam **tenant-scoped** (migration ``0035``: the
    ``_correspondence`` fallback segment, the seam's own rule), and queues the
    request-less company-less outbox row the way ①'s asks ride it — one
    transaction's work: the stored letter, the row, the case's ``ask_sent_at``
    stamp and one ``attribution_ask_enqueued`` entry with the company column
    NULL (ruling D10). Delivery is §5.1.11's cycle, never here.

    Every refusal answers an ``AskReport`` and writes nothing — the R3
    posture — and a refusal here is not a failed cycle: the case is visible to
    staff either way.
    """
    anchors = list(
        dict.fromkeys(
            anchor
            for anchor in (case.original_rfc_message_id, *rfc_message_ids)
            if anchor
        )
    )
    if (
        await latest_ask_for_anchor(
            session,
            tenant_id=tenant_id,
            thread_id=case.thread_id,
            rfc_message_ids=anchors,
            exclude_case_id=case.id,
        )
        is not None
    ):
        return AskReport(enqueued=False, reason=ASK_SUPPRESSED)
    if not ask_allowed(auto_submitted=auto_submitted, return_path=return_path):
        return AskReport(enqueued=False, reason=ASK_NOT_ALLOWED)
    if (
        await count_asks_since(
            session, tenant_id=tenant_id, since=at - timedelta(hours=1), company_less=True
        )
        >= settings.clarification_ask_hourly_cap
    ):
        return AskReport(enqueued=False, reason=ASK_CAP_REACHED)
    if (
        await count_asks_for_case(session, tenant_id=tenant_id, case_id=case.id)
        >= _MAX_ASKS_PER_CASE
    ):
        return AskReport(enqueued=False, reason=ASK_LIMIT_REACHED)
    if case.ask_kind is None:
        # ``open_case`` always writes the kind for the two unthreaded asks.
        raise ValueError("enqueue_ask needs the case's ask kind; open_case writes one")
    kind = AttributionAskKind(case.ask_kind)
    if not recipients:
        raise ValueError("a company-less ask goes to the sender; the caller supplies it")
    try:
        tcsp_name = await _tenant_name(session, tenant_id=tenant_id)
        message = _compose_company_less_message(
            case=case,
            kind=kind,
            tcsp_name=tcsp_name,
            recipients=recipients,
            original_subject=original_subject,
            original_received_at=original_received_at,
            at=at,
        )
        letter = await documents.register_upload(
            session,
            store,
            tenant_id=tenant_id,
            company_id=None,
            actor_user_id=None,
            kind=DocumentKind.GENERATED_DRAFT,
            origin=DocumentOrigin.GENERATED,
            content_type=MESSAGE_CONTENT_TYPE,
            data=message.as_bytes(),
            scanner=scanner,
            retention_class=RetentionClass.CLIENT_COMMUNICATION,
        )
    except AttributionAskError as exc:
        # Failures are outcomes, never aborts: the cycle that opened the case
        # finishes its other messages, and the case stays open for staff.
        return AskReport(enqueued=False, reason=exc.reason)
    enqueued = await outbox.enqueue_attribution_ask(
        session,
        tenant_id=tenant_id,
        company_id=None,
        case_id=case.id,
        letter_document_id=letter.id,
    )
    row = enqueued.row
    met_pending_row = not enqueued.created
    # The audit names the letter the row will actually deliver: the winner's
    # on a met pending row, the fresh one otherwise — never the other pairing.
    delivered_document_id = row.letter_document_id if met_pending_row else letter.id
    assert delivered_document_id is not None  # an ask row names its letter
    await set_ask_sent(session, tenant_id=tenant_id, case_id=case.id, at=at)
    await audit.record_event(
        session,
        tenant_id=tenant_id,
        action=AuditAction.ATTRIBUTION_ASK_ENQUEUED,
        actor_user_id=None,
        company_id=None,
        entity_type="attribution_case",
        entity_id=case.id,
        details={
            "case_id": str(case.id),
            "ask_kind": kind.value,
            "outbox_id": str(row.id),
            "document_id": str(delivered_document_id),
            "destination": outbox.CONFIRMATION_DESTINATION.value,
            "met_pending_row": met_pending_row,
        },
    )
    await session.flush()
    return AskReport(
        enqueued=True,
        outbox_id=row.id,
        document_id=None if met_pending_row else letter.id,
        met_pending_row=met_pending_row,
    )


async def _tenant_name(session: AsyncSession, *, tenant_id: UUID) -> str:
    """The tenant's own name — the TCSP name the company-less subject leads
    with (C10). The case's foreign key guarantees the row; a tenant without
    a name cannot compose a letter that reads as the TCSP's own."""
    name: str | None = await session.scalar(
        select(Tenant.name).where(Tenant.id == tenant_id)
    )
    if not name:
        raise AttributionAskError("tenant_name_unavailable")
    return name


def _compose_company_less_message(
    *,
    case: AttributionCase,
    kind: AttributionAskKind,
    tcsp_name: str,
    recipients: Sequence[str],
    original_subject: str,
    original_received_at: datetime,
    at: datetime,
) -> EmailMessage:
    """Compose Letter A or B — the company-less asks' bilingual drafts (D5).

    The same assembly as ①'s letters (plain-text UTF-8 with CRLF, RFC 2047
    headers, a fresh ``Message-ID`` in the mailbox's own domain, the thread
    headers carrying the original's RFC ``Message-ID`` so the answer comes
    back to the anchor), with what the company-bound letters carry replaced by
    the company-less shape: the subject leads with the TCSP's name instead of
    a company's (C10), the body names no company at all, and the recipients
    are used as given — the caller passes the sender alone (ruling D8), and no
    per-company stored-form lookup exists to restore. The composition is
    returned for the caller to register tenant-scoped and queue — the letter
    that rides the outbox is these exact bytes.
    """
    from_address = _mailbox_address()
    if kind is AttributionAskKind.CLARIFICATION:
        body = clarification_ask_letter(
            sender_address=case.sender_address,
            original_subject=original_subject,
            original_received_at=original_received_at,
        )
    elif kind is AttributionAskKind.REGISTERED_CHANNEL:
        body = first_contact_ask_letter(
            sender_address=case.sender_address,
            original_subject=original_subject,
            original_received_at=original_received_at,
        )
    else:
        raise ValueError("a company-less case carries only the two company-less asks")
    message = EmailMessage(policy=SMTP)
    message["From"] = from_address
    message["To"] = ", ".join(recipients)
    message["Subject"] = companyless_ask_subject(tcsp_name=tcsp_name, kind=kind)
    message["Date"] = format_datetime(at)
    message["Message-ID"] = make_msgid(domain=from_address.rpartition("@")[2] or None)
    if case.original_rfc_message_id is not None:
        # The reply rides back on the original's own thread, so the answer
        # lands where the case's anchor is matched.
        message["In-Reply-To"] = case.original_rfc_message_id
        message["References"] = case.original_rfc_message_id
    message.set_content(body)
    return message


async def _compose_and_store(
    session: AsyncSession,
    store: DocumentStore,
    scanner: Scanner,
    *,
    tenant_id: UUID,
    case: AttributionCase,
    kind: AttributionAskKind,
    recipients: Sequence[str],
    original_subject: str,
    original_received_at: datetime,
    at: datetime,
) -> Document:
    """Compose the ask and register its WORM original — the reconfirmer's
    assembly, mirrored: plain-text UTF-8 with CRLF and RFC 2047 headers
    (``policy.SMTP``), a fresh ``Message-ID`` in the mailbox's own domain, the
    thread headers carrying the original's RFC ``Message-ID`` when the case
    has one, the ask's recipients in the form the tenant registered, stored
    through the records registry with the scan rule applied.

    The company is named and numbered in both language halves (scope §7): a
    company with no name in force is refused rather than written to, and a
    deployment with no mailbox address cannot compose a message that would be
    recognised as its own. Both refusals are ``AttributionAskError`` — the
    caller answers a suppressed ask, not a failed cycle. Only company-bound
    cases reach here: ``enqueue_ask`` routes the company-less ones to the
    two Letters A/B composition, and this function's company reads are ①'s
    own invariant.
    """
    assert case.company_id is not None  # the caller routes company-less cases away
    from_address = _mailbox_address()
    snapshot = await company_master.get_company_master(
        session, tenant_id=tenant_id, company_id=case.company_id, as_of=at.date()
    )
    name = None if snapshot.name is None else (snapshot.name.name_en or snapshot.name.name_zh)
    if name is None:
        raise AttributionAskError("company_name_unavailable")
    cr_number = snapshot.company.cr_number
    br_number = snapshot.company.br_number
    subject = ask_subject(
        company_name=name, cr_number=cr_number, br_number=br_number, kind=kind
    )
    if kind is AttributionAskKind.CONFIRMATION:
        body = confirmation_ask_letter(
            company_name=name,
            cr_number=cr_number,
            br_number=br_number,
            sender_address=case.sender_address,
            original_subject=original_subject,
            original_received_at=original_received_at,
        )
    else:
        body = registration_ask_letter(
            company_name=name, cr_number=cr_number, br_number=br_number
        )
    message = EmailMessage(policy=SMTP)
    message["From"] = from_address
    message["To"] = ", ".join(
        await _recipient_forms(
            session,
            tenant_id=tenant_id,
            company_id=case.company_id,
            recipients=recipients,
        )
    )
    message["Subject"] = subject
    message["Date"] = format_datetime(at)
    message["Message-ID"] = make_msgid(domain=from_address.rpartition("@")[2] or None)
    if case.original_rfc_message_id is not None:
        # The reply rides back on the original's own thread, so the
        # confirmation lands where the case's anchor is matched.
        message["In-Reply-To"] = case.original_rfc_message_id
        message["References"] = case.original_rfc_message_id
    message.set_content(body)
    return await documents.register_upload(
        session,
        store,
        tenant_id=tenant_id,
        company_id=case.company_id,
        actor_user_id=None,
        kind=DocumentKind.GENERATED_DRAFT,
        origin=DocumentOrigin.GENERATED,
        content_type=MESSAGE_CONTENT_TYPE,
        data=message.as_bytes(),
        scanner=scanner,
        retention_class=RetentionClass.CLIENT_COMMUNICATION,
    )


async def _recipient_forms(
    session: AsyncSession, *, tenant_id: UUID, company_id: UUID, recipients: Sequence[str]
) -> tuple[str, ...]:
    """The ask's To addresses in the form the tenant's channel rows store.

    ``decide`` carries recipients in their comparison form (trimmed,
    case-folded — ``app.domain.communications.normalized_address``); a client
    reading the ask should see the address it registered, so each recipient is
    looked up among the company's registered email channels and restored to
    its stored form, falling back to the comparison form when nothing matches
    (the 2.1 ask goes to the sender, who by definition is not a registered
    channel).
    """
    comparison: list[str] = []
    seen: set[str] = set()
    for item in recipients:
        normalized = normalized_address(item)
        if normalized and normalized not in seen:
            seen.add(normalized)
            comparison.append(normalized)
    if not comparison:
        return ()
    stored = await contacts.stored_channel_forms(
        session, tenant_id=tenant_id, company_id=company_id, identifiers=comparison
    )
    return tuple(stored.get(item, item) for item in comparison)


def _mailbox_address() -> str:
    """The mailbox every client-facing message goes out from (scope §2.5).

    The configured address is load-bearing twice over: it is the ``From``
    header and the ``Message-ID``'s domain, so a deployment without one
    refuses rather than composing a letter that could not be recognised as
    the TCSP's own (the generator's, the sweep's and the reconfirmer's own
    rule).
    """
    address = settings.mail_address.strip()
    if not address:
        raise AttributionAskError("mail_not_configured")
    return address


__all__ = [
    "ANSWER_UNRESOLVED",
    "ASK_CANCELLED_ON_CLOSE",
    "ASK_CANCELLED_ON_DISMISS",
    "ASK_CANCELLED_ON_RESOLVE",
    "ASK_CAP_REACHED",
    "ASK_CASE_NOT_OPEN",
    "ASK_GATED",
    "ASK_LIMIT_REACHED",
    "ASK_NOT_ALLOWED",
    "ASK_SUPPRESSED",
    "PROCESS_CASE_NOT_OPEN",
    "AskReport",
    "AttributionAskError",
    "AttributionCaseHasNoCompanyError",
    "AttributionCaseNotFoundError",
    "AttributionCaseNotOpenError",
    "CaseProcessResult",
    "ClarificationResolution",
    "ProcessReport",
    "ask_allowed",
    "close_first_contact_cases",
    "close_registration_cases",
    "confirm_case",
    "dismiss_case",
    "enqueue_ask",
    "list_cases",
    "open_case",
    "process_case",
    "process_open_case",
    "resolve_clarification_case",
]
