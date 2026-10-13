"""Command-channel endpoints on the execution plane (ADR-0012 open action 3;
contract §5.2).

The deployment is the one that polls, so there is no route here that accepts
a command; these two are for the humans who operate it:

- ``POST /commands/run`` runs exactly one cycle — poll, execute, report — and
  returns what it did. This is the reviewer's, operator's, and demo's door
  into the worker: the background loop is off by default, and this route
  works whether or not it is running. It answers 200 with the report even
  when the vendor is unreachable, because "nothing could be polled" is a
  result the caller needs to see, not an error to squeeze out of a status
  code. A command executed here is audited exactly like one the loop runs.
- ``GET /commands`` lists what this deployment has received and what it
  decided: the outcome, the error code, and the metadata-only telemetry,
  never the envelope's parameters (the local record has them; this view is
  for an operator watching the channel work).

Both are Administrator-only: the command channel is deployment plumbing, in
the same class as activation and the entitlement lease.

There is deliberately no route that executes one command by hand, and none
that reports a result: an outcome is produced by executing, and reported by
the cycle that produced it — both ungated paths (the manual fill → sign →
export line, the records registry, the audit trail) are unaffected by
anything here.
"""

from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.persistence.commands import CommandInbox
from app.adapters.persistence.session import get_session
from app.api.deps import require_admin
from app.application import commands
from app.domain.rbac import ActorContext

router = APIRouter(prefix="/api/v1/commands", tags=["commands"])


class CommandOutcomeResponse(BaseModel):
    command_id: UUID
    command: str | None
    status: str
    error_code: str | None


class CycleReportResponse(BaseModel):
    """One cycle's account of itself: what it moved, and what stopped it."""

    activated: bool
    received: int
    dropped: int
    executed: int
    reported: int
    poll_error: str | None
    execution_error: str | None
    report_error: str | None
    outcomes: list[CommandOutcomeResponse]


class ReceivedCommandResponse(BaseModel):
    """One received envelope, as the deployment decided about it."""

    command_id: UUID
    command: str | None
    envelope_version: int | None
    status: str | None
    error_code: str | None
    received_at: datetime
    completed_at: datetime | None
    reported_at: datetime | None
    telemetry: dict[str, Any]


def _report_response(report: commands.CycleReport) -> CycleReportResponse:
    return CycleReportResponse(
        activated=report.activated,
        received=report.received,
        dropped=report.dropped,
        executed=report.executed,
        reported=report.reported,
        poll_error=report.poll_error,
        execution_error=report.execution_error,
        report_error=report.report_error,
        outcomes=[
            CommandOutcomeResponse(
                command_id=outcome.command_id,
                command=outcome.command,
                status=outcome.status.value,
                error_code=outcome.error_code.value if outcome.error_code else None,
            )
            for outcome in report.outcomes
        ],
    )


@router.post("/run")
async def run_one_cycle(
    _: Annotated[ActorContext, Depends(require_admin())],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> CycleReportResponse:
    """Run exactly one receive → execute → report cycle, now."""
    return _report_response(await commands.run_cycle(session))


@router.get("")
async def list_received_commands(
    _: Annotated[ActorContext, Depends(require_admin())],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> list[ReceivedCommandResponse]:
    """What this deployment has received, newest first, with its outcomes."""
    rows = await session.scalars(
        select(CommandInbox).order_by(CommandInbox.received_at.desc(), CommandInbox.command_id)
    )
    return [_received_response(row) for row in rows.all()]


def _received_response(row: CommandInbox) -> ReceivedCommandResponse:
    result = row.result or {}
    telemetry = result.get("telemetry")
    envelope_version = row.envelope.get("envelope_version")
    command = row.envelope.get("command")
    return ReceivedCommandResponse(
        command_id=row.command_id,
        command=command if isinstance(command, str) else None,
        envelope_version=envelope_version if isinstance(envelope_version, int) else None,
        status=row.status,
        error_code=result.get("error_code") if isinstance(result.get("error_code"), str) else None,
        received_at=row.received_at,
        completed_at=row.completed_at,
        reported_at=row.reported_at,
        telemetry=telemetry if isinstance(telemetry, dict) else {},
    )
