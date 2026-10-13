"""The confirmation-discovery sweep (scope §2.5; the discovery slice).

One cycle walks the tenant's companies and asks, per company, the panel's own
question — which period's data-confirmation ask date has arrived with no
confirmation request yet (R3) — and delegates each due company to the existing
generation trigger (``app.application.confirmation.generate``, contract
§5.1.8):

- **Selection mirrors the panel, bounded by it.** The population is every
  company of the tenant (a system operation has no grant filter; the panel's
  Administrator scope is the same set, R3/R4 — dormant companies included,
  exactly as the panel has them). The candidate years are the panel's own
  ``range(as_of.year - 1, as_of.year + 3)`` (``app.domain.confirmation_discovery``
  states the range once). Per company the cycle attempts the earliest arrived,
  unasked candidate year; periods CR's reply window has already closed are
  refused by the trigger's own ``no_client_reply_window`` guard — the cycle
  reports the refusal, never works around it.
- **"Asked" is the panel's done-signal (R2).** Any ``confirmation_requests``
  row for ``(company, year)`` — any status, superseded history included —
  makes the arrived period a skip with the vocabulary ``already_requested``.
  The skip set the cycle reports is pinned to the panel read's done set by
  the R2 alignment test, not by shared code (R9).
- **Generation is the trigger's, unchanged.** The cycle calls
  ``confirmation.generate`` as the system actor (``actor_user_id=None``, the
  reminders precedent) with NAR1, the attempted year's anniversary as the
  made-up date, and ``as_of`` the cycle's day. Under the tenant's
  ``automatic`` client mode the returned request is already queued (the
  trigger composes the release itself); under ``review_required`` it stays a
  held draft. The release gate is not consulted here (R5): drafting is not a
  release, and the founder's 2026-09-24 ruling put the gate at delivery.
- **A refusal is a report entry and never a write** (the trigger's standing
  semantics): the trigger's typed exceptions are caught and named with its
  own wire vocabulary — ``automation_denied: <reason>``,
  ``no_client_contact``, ``client_contact_not_found``, ``company_not_found``,
  ``no_client_reply_window``, ``company_name_unavailable``,
  ``nar1_rule_not_established``, ``unsupported_form``, ``draft_conflict``,
  ``mail_not_configured`` — the mapping ``app.api.confirmation`` uses, plus
  ``company_not_found`` for the one window the route never meets: the
  population read and the attempt share one transaction, but a concurrent
  commit can retire the company between them, and the contract's "never a 500
  for one company" names the refusal rather than raising. No request row, no
  outbox row, no audit entry accompanies a refusal. The audit chain records
  the trigger's own ``confirmation_request_drafted`` entry on a generation
  and nothing else: the cycle writes no audit vocabulary of its own
  (R5-of-the-plan).
- **A stored milestone block nobody can read stops the cycle before it
  starts**, exactly like the reminder sweep: the report names the block's
  violations in ``config_error`` (the route maps it to ``409
  tenant_config_invalid``), nothing is examined and nothing is written. The
  trigger's own send-mode read failing mid-cycle is mapped the same way,
  defensively — the cycle stops naming the block rather than composing
  further letters on numbers the tenant did not choose.
- **Overlapping cycles serialize** on a transaction-scoped advisory lock
  keyed to the tenant, taken before any read or write. Without it two cycles
  (the background loop plus an administrator's press, or two administrators)
  both read "due and unasked" and both call the trigger: the loser's
  generation supersedes the winner's just-committed draft — two rows, two
  audit entries, a letter orphaned by the loser. The lock makes the second
  cycle wait and re-derive from the winner's committed transaction, where the
  period now reads asked. The lock space is this module's own constant —
  ``"CDSW"`` — never the reminder sweep's ``"RMSW"``: a discovery key can
  never serialize against a different feature's lock on the same database.
  Stated residual, as the reminders sweep records its own: the key hashes the
  tenant id's first four bytes, so two tenants colliding on those bytes
  serialize against each other — exclusion is preserved either way; only a
  little latency is shared.

Everything here flushes and never commits: the API session dependency (or the
worker's own edge) commits, so a generated request, its queued outbox row and
its audit entry land together.
"""

from dataclasses import dataclass
from datetime import UTC, date, datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.persistence.confirmation import ConfirmationRequest
from app.adapters.persistence.models import Company
from app.application import confirmation, tenant_config
from app.application.company_master import CompanyNotFoundError
from app.application.documents import DocumentStore
from app.application.scanning import Scanner
from app.domain.compliance import DueDateRuleNotEstablishedError, anniversary_in_year
from app.domain.confirmation import (
    CompanyNameUnavailableError,
    NoClientReplyWindowError,
    UnsupportedFormError,
)
from app.domain.confirmation_discovery import (
    DiscoveryOutcome,
    DiscoverySkip,
    candidate_years,
    earliest_arrived_year,
    earliest_eligible_year,
)
from app.domain.milestones import MilestoneValidationError, milestone_dates
from app.domain.prior_filing import FormCode
from app.domain.send_modes import SendModesValidationError

# The advisory lock's high word — "CDSW" (confirmation-discovery sweep) — so a
# discovery key can never collide with a different feature's xact lock (the
# reminder sweep's space is its own constant, and the two never share).
_DISCOVERY_LOCK_SPACE = 0x43445357


def _discovery_lock_key(tenant_id: UUID) -> int:
    """The per-tenant advisory key serializing overlapping discovery cycles."""
    return (_DISCOVERY_LOCK_SPACE << 32) | int.from_bytes(tenant_id.bytes[:4], "big")


@dataclass(frozen=True)
class CompanyReport:
    """One company's outcome: ids, the decided period, and the detail.

    ``request_id`` accompanies ``generated``; ``reason`` accompanies
    ``refused`` (the trigger's own wire vocabulary); ``skip`` accompanies
    ``skipped``. ``year`` is the candidate period the decision is about — the
    attempted period, or the arrived period already asked.
    """

    company_id: UUID
    year: int
    outcome: DiscoveryOutcome
    request_id: UUID | None = None
    reason: str | None = None
    skip: DiscoverySkip | None = None


@dataclass(frozen=True)
class DiscoveryCycleReport:
    """One cycle's account of itself, per examined company, in company order.

    Only companies with at least one arrived candidate year appear — a
    company whose asks all lie in the future owed nothing and is not
    "examined". ``config_error`` is set when a stored configuration block
    could not be read: the cycle generated nothing and examined nothing, and
    the route maps the value to the ``tenant_config_invalid`` refusal.
    """

    now: datetime
    outcomes: tuple[CompanyReport, ...]
    config_error: str | None = None

    @property
    def examined(self) -> int:
        return len(self.outcomes)

    @property
    def generated(self) -> int:
        return sum(1 for item in self.outcomes if item.outcome is DiscoveryOutcome.GENERATED)

    @property
    def refused(self) -> int:
        return sum(1 for item in self.outcomes if item.outcome is DiscoveryOutcome.REFUSED)

    @property
    def skipped(self) -> int:
        return sum(1 for item in self.outcomes if item.outcome is DiscoveryOutcome.SKIPPED)


async def sweep(
    session: AsyncSession,
    store: DocumentStore,
    scanner: Scanner,
    *,
    tenant_id: UUID,
    now: datetime | None = None,
) -> DiscoveryCycleReport:
    """Run one discovery cycle for the tenant and report what it decided.

    Never raises for one company: every due company gets an outcome, and a
    configuration block nobody can read stops the cycle before it starts,
    named in the report rather than thrown. ``now`` pins the clock (the
    background loop and the API route both pass it in tests).
    """
    at = now if now is not None else datetime.now(UTC)
    as_of = at.date()
    # Serialize overlapping cycles per tenant before any read or write: the
    # module docstring states the duplicated-generation failure mode and the
    # lock-key residual. Transaction-scoped, so a rolled-back cycle (the
    # test harness) releases it with its transaction.
    await session.execute(select(func.pg_advisory_xact_lock(_discovery_lock_key(tenant_id))))
    try:
        milestones = await tenant_config.get_milestones(session, tenant_id=tenant_id)
    except MilestoneValidationError as exc:
        # The cycle cannot derive ask dates from a block nobody can read, and
        # guessing the offsets would ask clients on the strength of numbers
        # the tenant did not choose. Nothing is written; the route names the
        # refusal.
        return DiscoveryCycleReport(now=at, outcomes=(), config_error=str(exc))

    companies = list(
        (
            await session.scalars(
                select(Company)
                .where(Company.tenant_id == tenant_id)
                .order_by(Company.created_at, Company.id)
            )
        ).all()
    )
    if not companies:
        return DiscoveryCycleReport(now=at, outcomes=())

    requested = await _requested_periods(
        session, tenant_id=tenant_id, company_ids=[company.id for company in companies]
    )

    reports: list[CompanyReport] = []
    for company in companies:
        asked_years = {year for (company_id, year) in requested if company_id == company.id}
        dates = {
            year: milestone_dates(
                company.anniversary_date, year, milestones=milestones
            ).data_confirmation
            for year in candidate_years(as_of)
        }
        due = earliest_eligible_year(dates, asked_years, as_of)
        if due is None:
            arrived = earliest_arrived_year(dates, as_of)
            if arrived is not None and arrived in asked_years:
                # The earliest arrived period has a request row, whatever its
                # status: the panel's own done-signal (R2).
                reports.append(
                    CompanyReport(
                        company_id=company.id,
                        year=arrived,
                        outcome=DiscoveryOutcome.SKIPPED,
                        skip=DiscoverySkip.ALREADY_REQUESTED,
                    )
                )
            continue
        report, config_error = await _attempt(
            session,
            store,
            scanner,
            company_id=company.id,
            anniversary=company.anniversary_date,
            year=due,
            tenant_id=tenant_id,
            as_of=as_of,
        )
        reports.append(report)
        if config_error is not None:
            # A block the trigger itself cannot read is not per-company: the
            # remaining companies would meet the same block in the same
            # trigger, so the cycle stops naming it. Outcomes already
            # gathered stay in the report — they are the cycle's facts.
            return DiscoveryCycleReport(
                now=at, outcomes=tuple(reports), config_error=config_error
            )
    return DiscoveryCycleReport(now=at, outcomes=tuple(reports))


async def _attempt(
    session: AsyncSession,
    store: DocumentStore,
    scanner: Scanner,
    *,
    company_id: UUID,
    anniversary: date,
    year: int,
    tenant_id: UUID,
    as_of: date,
) -> tuple[CompanyReport, str | None]:
    """One due company's generation, the trigger's refusals caught and named.

    The typed-exception → wire-string mapping is ``app.api.confirmation``'s
    own, restated: a refusal is the trigger's vocabulary and writes nothing.
    A config block that fails the trigger's own read mid-cycle is the one
    failure that is not per-company: the report entry is returned together
    with the block's violations, and ``sweep`` ends the cycle in a named
    ``config_error`` report (like an unreadable milestone block does).
    """
    try:
        row = await confirmation.generate(
            session,
            store,
            scanner,
            tenant_id=tenant_id,
            company_id=company_id,
            actor_user_id=None,
            form_type=FormCode.NAR1,
            target_date=anniversary_in_year(anniversary, year),
            as_of=as_of,
        )
    except confirmation.AutomationDeniedError as exc:
        reason = f"automation_denied: {exc.reason.value}"
    except CompanyNotFoundError:
        # The population read and this attempt share one transaction, but a
        # concurrent commit can retire the company in the moment between
        # them: the contract's "never a 500 for one company" means the cycle
        # names the refusal instead of raising the trigger's own exception.
        reason = "company_not_found"
    except confirmation.MailNotConfiguredError:
        reason = "mail_not_configured"
    except confirmation.NoClientContactError:
        reason = "no_client_contact"
    except confirmation.ClientContactNotFoundError:
        reason = "client_contact_not_found"
    except confirmation.DraftConflictError:
        reason = "draft_conflict"
    except UnsupportedFormError:
        reason = "unsupported_form"
    except DueDateRuleNotEstablishedError:
        reason = "nar1_rule_not_established"
    except NoClientReplyWindowError:
        reason = "no_client_reply_window"
    except CompanyNameUnavailableError:
        reason = "company_name_unavailable"
    except (MilestoneValidationError, SendModesValidationError) as exc:
        return (
            CompanyReport(company_id=company_id, year=year, outcome=DiscoveryOutcome.REFUSED),
            str(exc),
        )
    else:
        return (
            CompanyReport(
                company_id=company_id,
                year=year,
                outcome=DiscoveryOutcome.GENERATED,
                request_id=row.id,
            ),
            None,
        )
    return (
        CompanyReport(
            company_id=company_id, year=year, outcome=DiscoveryOutcome.REFUSED, reason=reason
        ),
        None,
    )


async def _requested_periods(
    session: AsyncSession, *, tenant_id: UUID, company_ids: list[UUID]
) -> set[tuple[UUID, int]]:
    """Which (company, year) periods already have any confirmation request.

    The request's ``target_date`` *is* the made-up date, so its year is the
    anniversary year — the same key the panel's own ``_generated_periods``
    read derives (R2: one vocabulary for "asked", pinned equal by the
    alignment test rather than by shared code, R9).
    """
    rows = await session.execute(
        select(ConfirmationRequest.company_id, ConfirmationRequest.target_date).where(
            ConfirmationRequest.tenant_id == tenant_id,
            ConfirmationRequest.company_id.in_(company_ids),
        )
    )
    return {(company_id, target_date.year) for company_id, target_date in rows.all()}


__all__ = [
    "CompanyReport",
    "DiscoveryCycleReport",
    "sweep",
]
