"""The command worker's cycle: receive, execute, report (ADR-0012 open
action 3; contract §5.2).

One cycle is the whole protocol, ordered so that a failure anywhere leaves
the deployment able to try again:

- **receive** polls the control plane and records each envelope that is new
  under its ``command_id``. A document with no usable id is counted and
  dropped — there is nothing to record it under and nothing to answer;
- **execute** runs every recorded envelope that has no outcome yet through
  the decode ladder, the local gate, and the registry, writing the outcome
  **and its audit entry** on this session before any report is attempted.
  That order is ADR-0012's requirement: the record of what happened is local,
  whether or not the report ever leaves the building;
- **report** sends every outcome the control plane has not acknowledged
  (``reported_at IS NULL``) and marks the accepted ones. A cycle interrupted
  between the POST and the mark re-reports; the control plane keeps the first
  outcome, so at-least-once reporting is exactly-once in the record.

Gating is layered, and every leg fails closed (§5.1 invariant, ADR-0012):
the tenant's tier is read through the entitlement seam, so a deployment whose
lease has lapsed to Free executes no orchestration and records
``entitlement_denied``; an envelope that names a company
(``local_record_refs.company_ref``) is additionally checked against that
company's assignment and package through the same single gate every other
automation trigger consults, and a reference that no longer resolves is
``stale_state``. ``ping`` names no company, so the tier leg is its whole gate.

Nothing here raises for a control-plane failure, and nothing here touches the
ungated paths: a vendor outage or a lapsed licence degrades this module's
work only.

The registry's first command was ``ping``: deployment metadata — versions,
uptime, clock — and no client data, so the transport could be exercised end
to end before any workflow command did. The workflow command of this slice
is ``schedule_orchestration``: the composed sweep. It adds no gate of its
own (the gate above checked the entitlement) and no audit action of its own
(each leg audits itself; the outcome is audited by the record path) — it
composes the five existing cycles on this session, in their pinned order,
and the command outcome is the composition's counts.
"""

import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.entitlement.activation_store import ActivationStore
from app.adapters.entitlement.control_plane_client import (
    ControlPlaneClient,
    ControlPlaneRefused,
    ControlPlaneUnavailable,
)
from app.adapters.persistence.commands import CommandInbox
from app.adapters.persistence.models import Tenant
from app.application import (
    audit,
    confirmation_discovery,
    documents,
    entitlement,
    lease,
    mail,
    mail_ingest,
    mail_send,
    reminders,
    reply_parsing,
    scanning,
    service_packages,
)
from app.application.company_master import CompanyNotFoundError
from app.application.documents import DocumentStore
from app.application.mail_send import SendDisposition
from app.application.scanning import Scanner
from app.core.config import APP_VERSION, settings
from app.domain.audit import AuditAction
from app.domain.commands import (
    COMPANY_REF,
    ENVELOPE_VERSION,
    CommandEnvelope,
    CommandErrorCode,
    CommandName,
    CommandStatus,
    EnvelopeInvalid,
    EnvelopeUnreadable,
    EnvelopeVersionUnsupported,
    command_id_of,
    decode_envelope,
)
from app.domain.service_packages import AutomationType, GatingReason

NOT_ACTIVATED = "not_activated"
NO_LOCAL_TENANT = "no_local_tenant"
LOCAL_TENANT_AMBIGUOUS = "local_tenant_ambiguous"

_PROCESS_STARTED_AT: datetime | None = None


@dataclass(frozen=True)
class CommandOutcome:
    """What this cycle decided about one command (the demo's view, not the wire's)."""

    command_id: UUID
    command: str | None
    status: CommandStatus
    error_code: CommandErrorCode | None


@dataclass(frozen=True)
class CycleReport:
    """What one cycle did, and what stopped it from doing more."""

    activated: bool
    received: int
    dropped: int
    executed: int
    reported: int
    poll_error: str | None = None
    execution_error: str | None = None
    report_error: str | None = None
    outcomes: tuple[CommandOutcome, ...] = ()


@dataclass(frozen=True)
class CommandRun:
    """A handler's success: the telemetry to report, nothing else."""

    telemetry: Mapping[str, object]


@dataclass(frozen=True)
class GateRefusal:
    """The gate's refusal: the §5.1 error code and the machine reason."""

    error_code: CommandErrorCode
    reason: str


# A handler is called with the envelope it arrived in, the cycle's session,
# the tenant the outcome is attributed to, and the cycle's clock; it returns
# the telemetry to report. The session is the cycle's own: the command's
# outcome and its audit entry are recorded on it after the handler returns,
# so a handler works on that session and nothing the transport did not hand
# it.
CommandHandler = Callable[..., Awaitable[CommandRun]]

ORCHESTRATION_COUNT_KEYS: tuple[str, ...] = (
    "mail_ingested",
    "replies_parsed",
    "confirmations_generated",
    "confirmations_refused",
    "confirmations_skipped",
    "reminders_enqueued",
    "messages_sent",
    "cycles_failed",
)


@dataclass(frozen=True)
class OrchestrationReport:
    """The composed sweep's account: the eight counts and, in leg order, why legs failed."""

    counts: Mapping[str, int]
    causes: tuple[str, ...]


class OrchestrationFailedError(Exception):
    """A composed sweep with at least one failed leg: the counts and the causes.

    Raised by the orchestration handler so the command outcome records the
    failure honestly — a failed leg is not a success with low counts.
    """

    def __init__(self, *, counts: Mapping[str, int], causes: tuple[str, ...]) -> None:
        super().__init__(";".join(causes))
        self.counts = counts
        self.causes = causes


async def _ping(
    *, envelope: CommandEnvelope, session: AsyncSession, tenant_id: UUID, now: datetime
) -> CommandRun:
    """Deployment metadata: the build, how long this process has run, the clock.

    Reads nothing — not even the envelope it arrived in — and writes nothing:
    ``ping`` exists so the transport can be exercised end to end before any
    workflow command does, and so an operator can see a deployment answer.
    The session and tenant are accepted and ignored: the interface is one
    shape for every handler, and ``ping`` wants nothing from either.
    """
    return CommandRun(
        telemetry={
            "app_version": APP_VERSION,
            "uptime_seconds": max(0, int((now - _process_started_at()).total_seconds())),
            "clock": now.isoformat(),
        }
    )


async def _run_orchestration(
    *,
    session: AsyncSession,
    store: DocumentStore,
    scanner: Scanner,
    tenant_id: UUID,
    now: datetime,
) -> OrchestrationReport:
    """The five cycles in their pinned order — ingest, parse, discovery,
    reminders, send (§5.1.17's sweep runs between parse and reminders:
    discovery generates the ask, reminders then chase unanswered requests).

    Every leg runs on this session and every leg runs even when an earlier
    one failed: the sweep is one command, and a broken reminder cycle must
    not silence the outbox behind it. The two ingest skip classes mean
    "nothing to read" (no mailbox, no local tenant), not failure; everything
    else that escapes a leg is contained and named ``leg:ExceptionClass``.
    A sweep that returns with ``config_error`` set failed its leg even
    though it returned calmly. The discovery leg has no skip class of its
    own: generation is local, so a missing mailbox is not its failure — at
    most it is the sweep's own ``mail_not_configured`` refusal, one of the
    refused counts, and delivery belongs to the send leg. The counts carry
    every key, defaulting to 0 for a leg that contributed nothing;
    ``cycles_failed`` counts the failed legs (one per cause), never mapped
    from a leg report.
    """
    counts = {key: 0 for key in ORCHESTRATION_COUNT_KEYS}
    causes: list[str] = []

    try:
        ingested = await mail_ingest.run_cycle(
            session, store=store, scanner=scanner, tenant_id=tenant_id, now=now
        )
    except (mail.MailNotConnected, mail_ingest.NoLocalTenantError):
        pass  # nothing to read, or no tenant to read for: a skip, not a failure
    except Exception as exc:
        causes.append(f"ingest:{exc.__class__.__name__}")
    else:
        counts["mail_ingested"] = ingested.ingested

    try:
        parsed = await reply_parsing.run_cycle(
            session, store=store, scanner=scanner, tenant_id=tenant_id, as_of=now.date()
        )
    except Exception as exc:
        causes.append(f"parse:{exc.__class__.__name__}")
    else:
        counts["replies_parsed"] = parsed.parsed

    try:
        discovered = await confirmation_discovery.sweep(
            session, store, scanner, tenant_id=tenant_id, now=now
        )
    except Exception as exc:
        causes.append(f"discovery:{exc.__class__.__name__}")
    else:
        counts["confirmations_generated"] = discovered.generated
        counts["confirmations_refused"] = discovered.refused
        counts["confirmations_skipped"] = discovered.skipped
        if discovered.config_error is not None:
            causes.append("discovery:config_error")

    try:
        swept = await reminders.sweep(session, store, scanner, tenant_id=tenant_id, now=now)
    except Exception as exc:
        causes.append(f"reminders:{exc.__class__.__name__}")
    else:
        counts["reminders_enqueued"] = swept.enqueued
        if swept.config_error is not None:
            causes.append("reminders:config_error")

    try:
        sent = await mail_send.run_cycle(session, store, tenant_id=tenant_id, now=now)
    except Exception as exc:
        causes.append(f"send:{exc.__class__.__name__}")
    else:
        counts["messages_sent"] = sent.count(SendDisposition.SENT)

    counts["cycles_failed"] = len(causes)  # one per failed leg, not per command
    return OrchestrationReport(counts=counts, causes=tuple(causes))


async def _schedule_orchestration(
    *, envelope: CommandEnvelope, session: AsyncSession, tenant_id: UUID, now: datetime
) -> CommandRun:
    """The composed sweep: the five cycles in their pinned order, nothing else.

    No gating of its own (the gate above already checked the entitlement)
    and no audit action of its own (each leg audits itself; the outcome is
    audited by the record path). On a failed leg the sweep's counts and
    causes are still real telemetry, so they travel inside the raised error
    rather than being discarded.
    """
    report = await _run_orchestration(
        session=session,
        store=documents.get_storage(),
        scanner=scanning.scanner(),
        tenant_id=tenant_id,
        now=now,
    )
    if report.causes:
        raise OrchestrationFailedError(counts=report.counts, causes=report.causes)
    return CommandRun(telemetry=report.counts)


HANDLERS: Mapping[str, CommandHandler] = {
    CommandName.PING.value: _ping,
    CommandName.SCHEDULE_ORCHESTRATION.value: _schedule_orchestration,
}

_AUDIT_ACTIONS: Mapping[CommandStatus, AuditAction] = {
    CommandStatus.SUCCEEDED: AuditAction.COMMAND_EXECUTED,
    CommandStatus.FAILED: AuditAction.COMMAND_FAILED,
    CommandStatus.REJECTED: AuditAction.COMMAND_REJECTED,
}


def _process_started_at() -> datetime:
    """When this process first handled a cycle — the closest thing to uptime.

    Memoized rather than read at import (an import-time clock read is a hidden
    side effect; a deployment's first cycle and its process start differ by a
    scheduling tick).
    """
    global _PROCESS_STARTED_AT
    if _PROCESS_STARTED_AT is None:
        _PROCESS_STARTED_AT = datetime.now(UTC)
    return _PROCESS_STARTED_AT


async def run_cycle(session: AsyncSession, *, now: datetime | None = None) -> CycleReport:
    """Run one receive → execute → report cycle; never raises for a remote failure."""
    at = now if now is not None else datetime.now(UTC)
    credential = ActivationStore(settings.cp_activation_token_file).load().credential

    poll_error: str | None = None
    received = 0
    dropped = 0
    if credential is None:
        poll_error = NOT_ACTIVATED
    elif not settings.cp_base_url:
        poll_error = lease.NOT_CONFIGURED
    else:
        try:
            documents = await lease.client().fetch_commands(
                token=credential.token, envelope_version=ENVELOPE_VERSION
            )
        except ControlPlaneUnavailable:
            poll_error = lease.UNREACHABLE
        except ControlPlaneRefused as exc:
            poll_error = f"control_plane_refused:{exc.reason}"
        else:
            received, dropped = await _receive(session, documents=documents, now=at)

    tenant_id, execution_error = await _execution_tenant(session)
    executed = 0
    outcomes: list[CommandOutcome] = []
    if tenant_id is not None:
        executed, outcomes = await _execute_pending(session, tenant_id=tenant_id, now=at)

    reported, report_error = await _report_pending(
        session, credential=None if credential is None else credential.token, now=at
    )
    return CycleReport(
        activated=credential is not None,
        received=received,
        dropped=dropped,
        executed=executed,
        reported=reported,
        poll_error=poll_error,
        execution_error=execution_error,
        report_error=report_error,
        outcomes=tuple(outcomes),
    )


async def _receive(
    session: AsyncSession, *, documents: list[dict[str, object]], now: datetime
) -> tuple[int, int]:
    """Record the envelopes the control plane handed over; count the unreadable."""
    received = 0
    dropped = 0
    for document in documents:
        command_id = command_id_of(document)
        if command_id is None:
            dropped += 1
            continue
        if await session.get(CommandInbox, command_id) is not None:
            continue  # re-delivery: the row already records what happened
        session.add(CommandInbox(command_id=command_id, envelope=dict(document), received_at=now))
        received += 1
    await session.flush()
    return received, dropped


async def _execute_pending(
    session: AsyncSession, *, tenant_id: UUID, now: datetime
) -> tuple[int, list[CommandOutcome]]:
    """Execute every recorded envelope that has no outcome yet."""
    rows = list(
        (
            await session.scalars(
                select(CommandInbox)
                .where(CommandInbox.status.is_(None))
                .order_by(CommandInbox.received_at, CommandInbox.command_id)
            )
        ).all()
    )
    outcomes = [
        await _execute_one(session, row=row, tenant_id=tenant_id, now=now, depth=len(rows))
        for row in rows
    ]
    return len(rows), outcomes


async def _execute_one(
    session: AsyncSession, *, row: CommandInbox, tenant_id: UUID, now: datetime, depth: int
) -> CommandOutcome:
    """The decode ladder, the gate, then the handler — or the refusal that stops it."""
    document = row.envelope
    try:
        envelope = decode_envelope(document)
    except EnvelopeUnreadable:
        # Cannot happen for a stored row (the id is its primary key); refuse
        # rather than assume, because the alternative is executing a guess.
        return await _refuse(
            session,
            row=row,
            tenant_id=tenant_id,
            command=None,
            error_code=CommandErrorCode.LOCAL_VALIDATION_FAILED,
            reason="unreadable_envelope",
            depth=depth,
            now=now,
        )
    except EnvelopeVersionUnsupported as exc:
        return await _refuse(
            session,
            row=row,
            tenant_id=tenant_id,
            command=_command_name(document),
            error_code=CommandErrorCode.UNSUPPORTED_ENVELOPE_VERSION,
            reason=str(exc),
            depth=depth,
            now=now,
        )
    except EnvelopeInvalid as exc:
        return await _refuse(
            session,
            row=row,
            tenant_id=tenant_id,
            command=_command_name(document),
            error_code=CommandErrorCode.LOCAL_VALIDATION_FAILED,
            reason=str(exc),
            depth=depth,
            now=now,
        )

    handler = HANDLERS.get(envelope.command)
    if handler is None:
        return await _refuse(
            session,
            row=row,
            tenant_id=tenant_id,
            command=envelope.command,
            error_code=CommandErrorCode.UNKNOWN_COMMAND,
            reason="not_in_registry",
            depth=depth,
            now=now,
        )

    refusal = await _gate(session, envelope=envelope, tenant_id=tenant_id, now=now)
    if refusal is not None:
        return await _refuse(
            session,
            row=row,
            tenant_id=tenant_id,
            command=envelope.command,
            error_code=refusal.error_code,
            reason=refusal.reason,
            depth=depth,
            now=now,
        )

    started = time.monotonic()
    try:
        run = await handler(envelope=envelope, session=session, tenant_id=tenant_id, now=now)
    except OrchestrationFailedError as exc:
        # A composed leg failed: the counts are real telemetry and the causes
        # are named, so they are recorded rather than replaced by a generic
        # refusal. Duration has no honest value on this path — the sweep's
        # legs ran for their own accounts — and is not invented.
        return await _record(
            session,
            row=row,
            tenant_id=tenant_id,
            command=envelope.command,
            status=CommandStatus.FAILED,
            error_code=CommandErrorCode.EXECUTION_FAILED,
            reason=";".join(exc.causes),
            telemetry={**exc.counts, "queue_depth": depth},
            now=now,
        )
    except Exception as exc:  # a handler must never take the worker down
        return await _refuse(
            session,
            row=row,
            tenant_id=tenant_id,
            command=envelope.command,
            error_code=CommandErrorCode.EXECUTION_FAILED,
            reason=exc.__class__.__name__,
            depth=depth,
            now=now,
        )
    duration_ms = max(0, int((time.monotonic() - started) * 1000))
    return await _record(
        session,
        row=row,
        tenant_id=tenant_id,
        command=envelope.command,
        status=CommandStatus.SUCCEEDED,
        error_code=None,
        reason=None,
        telemetry={**run.telemetry, "duration_ms": duration_ms, "queue_depth": depth},
        now=now,
    )


async def _gate(
    session: AsyncSession, *, envelope: CommandEnvelope, tenant_id: UUID, now: datetime
) -> GateRefusal | None:
    """The §5.1 gate for one envelope; ``None`` means it may run."""
    company_ref = envelope.local_record_refs.get(COMPANY_REF)
    if company_ref is None:
        # A deployment-scoped command has no company legs. The tier leg is
        # the whole gate, read from the same entitlement seam the company
        # gate reads, so the two can never disagree about the licence.
        capabilities = (await entitlement.get_entitlement(session)).capabilities
        if AutomationType.WORKFLOW_ORCHESTRATION not in capabilities:
            return GateRefusal(
                error_code=CommandErrorCode.ENTITLEMENT_DENIED,
                reason=GatingReason.TIER_CAPABILITY_MISSING.value,
            )
        return None
    try:
        decision = await service_packages.check_automation(
            session,
            tenant_id=tenant_id,
            company_id=UUID(company_ref),
            automation_type=AutomationType.WORKFLOW_ORCHESTRATION,
            as_of=now.date(),
        )
    except CompanyNotFoundError:
        # The control plane is working from a company this deployment no
        # longer has: a state mismatch, not a licence problem.
        return GateRefusal(error_code=CommandErrorCode.STALE_STATE, reason="company_ref_unknown")
    if not decision.allowed:
        assert decision.reason is not None  # a denial always names its leg
        return GateRefusal(
            error_code=CommandErrorCode.ENTITLEMENT_DENIED, reason=decision.reason.value
        )
    return None


async def _refuse(
    session: AsyncSession,
    *,
    row: CommandInbox,
    tenant_id: UUID,
    command: str | None,
    error_code: CommandErrorCode,
    reason: str,
    depth: int,
    now: datetime,
) -> CommandOutcome:
    """Write a refusal: the command was read and deliberately not executed."""
    status = (
        CommandStatus.FAILED
        if error_code is CommandErrorCode.EXECUTION_FAILED
        else CommandStatus.REJECTED
    )
    return await _record(
        session,
        row=row,
        tenant_id=tenant_id,
        command=command,
        status=status,
        error_code=error_code,
        reason=reason,
        telemetry={"queue_depth": depth},
        now=now,
    )


async def _record(
    session: AsyncSession,
    *,
    row: CommandInbox,
    tenant_id: UUID,
    command: str | None,
    status: CommandStatus,
    error_code: CommandErrorCode | None,
    reason: str | None,
    telemetry: Mapping[str, object],
    now: datetime,
) -> CommandOutcome:
    """Store one outcome, its result document, and its audit entry together.

    ADR-0009 is why they share the session: an executed, refused, or failed
    command is recorded locally in the same transaction that records *that*
    it happened — so the audit trail is complete even when the report never
    leaves the building. The audit entry carries the outcome (``command``,
    ``status``, ``error_code``, ``reason``), never the envelope's parameters.
    """
    row.status = status.value
    row.result = {
        "envelope_version": ENVELOPE_VERSION,
        "command_id": str(row.command_id),
        "status": status.value,
        "error_code": error_code.value if error_code is not None else None,
        "telemetry": dict(telemetry),
        "completed_at": now.isoformat(),
    }
    row.completed_at = now
    await audit.record_event(
        session,
        tenant_id=tenant_id,
        action=_AUDIT_ACTIONS[status],
        entity_type="command",
        entity_id=row.command_id,
        details={
            "command": command,
            "status": status.value,
            "error_code": error_code.value if error_code is not None else None,
            "reason": reason,
        },
    )
    await session.flush()
    return CommandOutcome(
        command_id=row.command_id, command=command, status=status, error_code=error_code
    )


async def _report_pending(
    session: AsyncSession, *, credential: str | None, now: datetime
) -> tuple[int, str | None]:
    """Send every unacknowledged outcome; stop at the first failure to reach the vendor."""
    rows = list(
        (
            await session.scalars(
                select(CommandInbox)
                .where(CommandInbox.status.is_not(None), CommandInbox.reported_at.is_(None))
                .order_by(CommandInbox.received_at, CommandInbox.command_id)
            )
        ).all()
    )
    if not rows:
        return 0, None
    if credential is None:
        return 0, NOT_ACTIVATED
    if not settings.cp_base_url:
        return 0, lease.NOT_CONFIGURED

    client: ControlPlaneClient = lease.client()
    reported = 0
    for row in rows:
        assert row.result is not None  # the status/result constraint holds
        try:
            await client.submit_command_result(token=credential, result=row.result)
        except ControlPlaneUnavailable:
            return reported, lease.UNREACHABLE
        except ControlPlaneRefused as exc:
            return reported, f"control_plane_refused:{exc.reason}"
        row.reported_at = now
        reported += 1
    await session.flush()
    return reported, None


async def _execution_tenant(session: AsyncSession) -> tuple[UUID | None, str | None]:
    """The tenant whose audit chain records what this cycle executes.

    A deployment is single-tenant (ADR-0002), so "the tenant" is the one row
    the local ``tenants`` table holds. None, or several, means this module
    cannot say whose compliance record a command's outcome belongs to — and
    an execution nobody can attribute is not permitted, so it executes
    nothing and says why.
    """
    pending = await session.scalar(
        select(CommandInbox.command_id).where(CommandInbox.status.is_(None)).limit(1)
    )
    if pending is None:
        return None, None
    tenant_ids = list(
        (await session.scalars(select(Tenant.id).order_by(Tenant.created_at).limit(2))).all()
    )
    if not tenant_ids:
        return None, NO_LOCAL_TENANT
    if len(tenant_ids) > 1:
        return None, LOCAL_TENANT_AMBIGUOUS
    return tenant_ids[0], None


def _command_name(document: Mapping[str, object]) -> str | None:
    command = document.get("command")
    return command if isinstance(command, str) else None
