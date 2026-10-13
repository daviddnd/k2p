"""Per-company audit compliance report — pure composition rules (scope §8;
DoD 19; contract §5.1 Audit row; ruling R6).

Single owner of:

- the **report shape** (``build_report``) — the deterministic dict the route
  returns: a fixed ``report_version``, the generation instant, the company
  identity block, the tenant-chain verdict block, and the company's entries
  in chain order, each as stored (``occurred_at``/``actor``/``action``/
  ``entity``/``details``) plus its report area;
- the **area table** (``ACTION_AREA``) — the pure data classifying every
  ``AuditAction`` member into exactly one of DoD 18's five report areas
  (client communications / event lifecycle actions / data changes / AI
  suggestions / approvals). The table is asserted against the live enum in
  ``tests/test_audit_report_domain.py``: an action added without an area
  fails the completeness net, never a silent catch-all;
- the **chain verdict shape** (``ChainVerdict``) — ruling R6's honesty rule:
  ``verify_chain`` runs over the tenant's *full* chain (``prev_hash`` links
  span companies, so a company-filtered subset cannot re-verify on its
  own). The report embeds that tenant-wide verdict and says so in the
  chain block's ``scope`` note, so a reader never mistakes the company
  filter for an independently verifiable subset (Review Focus 4).

Pure and framework-free per ADR-0006: ``generated_at`` is an input, never
read from a clock here; nothing in this module touches I/O.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol
from uuid import UUID

from app.domain.audit import AuditAction, jsonable

REPORT_VERSION = 1

# Ruling R6, stated once where the report carries it: the chain was verified
# over the tenant's full audit chain — a company-filtered subset cannot be
# re-verified on its own because prev_hash links span companies.
CHAIN_SCOPE_NOTE = (
    "verified over the tenant's full audit chain; a company-filtered subset "
    "cannot be re-verified on its own because prev_hash links span companies"
)


class ReportArea(StrEnum):
    """DoD 18's five report areas (scope §2.13's audit-trail capture list)."""

    CLIENT_COMMUNICATIONS = "client_communications"
    EVENT_LIFECYCLE = "event_lifecycle"
    DATA_CHANGES = "data_changes"
    AI_SUGGESTIONS = "ai_suggestions"
    APPROVALS = "approvals"


ACTION_AREA: dict[AuditAction, ReportArea] = {
    # --- client communications: the exchange with the client -------------
    AuditAction.CONFIRMATION_REQUEST_DRAFTED: ReportArea.CLIENT_COMMUNICATIONS,
    AuditAction.CONFIRMATION_REQUEST_RELEASED: ReportArea.CLIENT_COMMUNICATIONS,
    AuditAction.RECONFIRMATION_ENQUEUED: ReportArea.CLIENT_COMMUNICATIONS,
    AuditAction.RECONFIRMATION_RELEASED: ReportArea.CLIENT_COMMUNICATIONS,
    AuditAction.REMINDER_ENQUEUED: ReportArea.CLIENT_COMMUNICATIONS,
    AuditAction.REMINDERS_EXHAUSTED: ReportArea.CLIENT_COMMUNICATIONS,
    AuditAction.MAIL_MESSAGE_INGESTED: ReportArea.CLIENT_COMMUNICATIONS,
    AuditAction.MAIL_MESSAGE_QUARANTINED: ReportArea.CLIENT_COMMUNICATIONS,
    AuditAction.MAIL_QUARANTINE_RESOLVED: ReportArea.CLIENT_COMMUNICATIONS,
    AuditAction.MAIL_QUARANTINE_DISMISSED: ReportArea.CLIENT_COMMUNICATIONS,
    AuditAction.MAIL_MESSAGE_SENT: ReportArea.CLIENT_COMMUNICATIONS,
    AuditAction.MAIL_SEND_FAILED: ReportArea.CLIENT_COMMUNICATIONS,
    # --- the attribution case an inbound message opened (ADR-0019 §1): the
    # --- exchange's own record, beside the ingestion and quarantine entries --
    AuditAction.ATTRIBUTION_CASE_OPENED: ReportArea.CLIENT_COMMUNICATIONS,
    AuditAction.ATTRIBUTION_ASK_ENQUEUED: ReportArea.CLIENT_COMMUNICATIONS,
    AuditAction.ATTRIBUTION_CONFIRMED: ReportArea.CLIENT_COMMUNICATIONS,
    AuditAction.ATTRIBUTION_CASE_CLOSED: ReportArea.CLIENT_COMMUNICATIONS,
    AuditAction.ATTRIBUTION_CASE_RESOLVED: ReportArea.CLIENT_COMMUNICATIONS,
    AuditAction.ATTRIBUTION_CASE_DISMISSED: ReportArea.CLIENT_COMMUNICATIONS,
    # --- the multi-company split one inbound message opened (ADR-0019 §3):
    # --- the exchange's own record too — the parent is the message's row,
    # --- and the items are its per-company letters, not AI suggestions ---
    AuditAction.SPLIT_PARENT_CREATED: ReportArea.CLIENT_COMMUNICATIONS,
    AuditAction.SPLIT_PARENT_STAFF_ONLY: ReportArea.CLIENT_COMMUNICATIONS,
    AuditAction.SPLIT_PARENT_FAILED: ReportArea.CLIENT_COMMUNICATIONS,
    AuditAction.SPLIT_ITEM_CREATED: ReportArea.CLIENT_COMMUNICATIONS,
    AuditAction.SPLIT_PARENT_DONE: ReportArea.CLIENT_COMMUNICATIONS,
    # --- event lifecycle: the change-event state machine -----------------
    AuditAction.CHANGE_EVENT_RECORDED: ReportArea.EVENT_LIFECYCLE,
    AuditAction.CHANGE_EVENT_TRANSITION_RECORDED: ReportArea.EVENT_LIFECYCLE,
    # --- AI suggestions: gateway calls and their parsed outcomes ---------
    AuditAction.LLM_CALL: ReportArea.AI_SUGGESTIONS,
    AuditAction.REPLY_PARSED: ReportArea.AI_SUGGESTIONS,
    # --- approvals: staff review decisions over extractions, drafts,
    # --- signatures and exports (scope §2.13's reviewer-approval capture) -
    AuditAction.IMPORT_RECORD_CONFIRMED: ReportArea.APPROVALS,
    AuditAction.IMPORT_RECORD_REJECTED: ReportArea.APPROVALS,
    AuditAction.DRAFT_GENERATED: ReportArea.APPROVALS,
    AuditAction.DRAFT_TRANSITION_RECORDED: ReportArea.APPROVALS,
    AuditAction.DRAFT_INVALIDATED: ReportArea.APPROVALS,
    AuditAction.DRAFT_SIGNATURE_PACKAGE_GENERATED: ReportArea.APPROVALS,
    AuditAction.DRAFT_EXPORTED: ReportArea.APPROVALS,
    # --- data changes: every other write — master data, configuration,
    # --- tenancy, documents, packages (§2.13's who/what/when floor) ------
    AuditAction.TENANT_CREATED: ReportArea.DATA_CHANGES,
    AuditAction.USER_CREATED: ReportArea.DATA_CHANGES,
    AuditAction.STAFF_LOGIN: ReportArea.DATA_CHANGES,
    AuditAction.COMPANY_CREATED: ReportArea.DATA_CHANGES,
    AuditAction.COMPANY_STATICS_CORRECTED: ReportArea.DATA_CHANGES,
    AuditAction.COMPANY_FACT_RECORDED: ReportArea.DATA_CHANGES,
    AuditAction.COMPANIES_IMPORTED: ReportArea.DATA_CHANGES,
    AuditAction.PERSON_CREATED: ReportArea.DATA_CHANGES,
    AuditAction.PERSON_UPDATED: ReportArea.DATA_CHANGES,
    AuditAction.CONTACT_CHANNEL_ADDED: ReportArea.DATA_CHANGES,
    AuditAction.CONTACT_CHANNEL_UPDATED: ReportArea.DATA_CHANGES,
    AuditAction.COMPANY_CONTACT_LINKED: ReportArea.DATA_CHANGES,
    AuditAction.COMPANY_CONTACT_UNLINKED: ReportArea.DATA_CHANGES,
    AuditAction.NAMING_TEMPLATES_UPDATED: ReportArea.DATA_CHANGES,
    AuditAction.TENANT_MILESTONES_UPDATED: ReportArea.DATA_CHANGES,
    AuditAction.TENANT_SEND_MODES_UPDATED: ReportArea.DATA_CHANGES,
    AuditAction.COMPANY_AI_DISABLED_UPDATED: ReportArea.DATA_CHANGES,
    AuditAction.SERVICE_PACKAGE_CREATED: ReportArea.DATA_CHANGES,
    AuditAction.SERVICE_PACKAGE_UPDATED: ReportArea.DATA_CHANGES,
    AuditAction.SERVICE_PACKAGE_DELETED: ReportArea.DATA_CHANGES,
    AuditAction.SERVICE_ASSIGNMENT_CREATED: ReportArea.DATA_CHANGES,
    AuditAction.SERVICE_ASSIGNMENT_UPDATED: ReportArea.DATA_CHANGES,
    AuditAction.SERVICE_RENEWAL_RECORDED: ReportArea.DATA_CHANGES,
    AuditAction.DOCUMENT_REGISTERED: ReportArea.DATA_CHANGES,
    AuditAction.DOCUMENT_DOWNLOADED: ReportArea.DATA_CHANGES,
    AuditAction.DOCUMENT_SCANNED: ReportArea.DATA_CHANGES,
    AuditAction.DOCUMENT_RELEASE_REFUSED: ReportArea.DATA_CHANGES,
    AuditAction.PRIOR_FILING_EXTRACTED: ReportArea.DATA_CHANGES,
    AuditAction.OFFICER_APPOINTMENT_CREATED: ReportArea.DATA_CHANGES,
    AuditAction.OFFICER_APPOINTMENT_UPDATED: ReportArea.DATA_CHANGES,
    AuditAction.OFFICER_APPOINTMENT_DELETED: ReportArea.DATA_CHANGES,
    AuditAction.SHAREHOLDING_CREATED: ReportArea.DATA_CHANGES,
    AuditAction.SHAREHOLDING_UPDATED: ReportArea.DATA_CHANGES,
    AuditAction.SHAREHOLDING_DELETED: ReportArea.DATA_CHANGES,
    AuditAction.MAIL_CONNECTION_CREATED: ReportArea.DATA_CHANGES,
    AuditAction.MAIL_CONNECTION_REMOVED: ReportArea.DATA_CHANGES,
    AuditAction.CALENDAR_FEED_CREATED: ReportArea.DATA_CHANGES,
    AuditAction.CALENDAR_FEED_REVOKED: ReportArea.DATA_CHANGES,
    AuditAction.CALENDAR_PROVIDER_CONNECTED: ReportArea.DATA_CHANGES,
    AuditAction.CALENDAR_PROVIDER_DISCONNECTED: ReportArea.DATA_CHANGES,
    AuditAction.CONTROL_PLANE_ACTIVATED: ReportArea.DATA_CHANGES,
    AuditAction.CONTROL_PLANE_DEACTIVATED: ReportArea.DATA_CHANGES,
    AuditAction.COMMAND_EXECUTED: ReportArea.DATA_CHANGES,
    AuditAction.COMMAND_FAILED: ReportArea.DATA_CHANGES,
    AuditAction.COMMAND_REJECTED: ReportArea.DATA_CHANGES,
    AuditAction.PACK_INSTALLED: ReportArea.DATA_CHANGES,
    AuditAction.PACK_REFRESH_REFUSED: ReportArea.DATA_CHANGES,
    AuditAction.PACKS_FROZEN: ReportArea.DATA_CHANGES,
    AuditAction.PACKS_UNFROZEN: ReportArea.DATA_CHANGES,
    # --- the deployment's own housekeeping: the scheduled / on-demand
    # --- integrity verification writes one entry per completed check ---
    AuditAction.INTEGRITY_CHECK: ReportArea.DATA_CHANGES,
    # --- the per-tenant encryption key's creation (slice D1): a data change
    # --- recorded once, with the key's id alone — never key material ------
    AuditAction.ENCRYPTION_KEY_CREATED: ReportArea.DATA_CHANGES,
    # --- staff-MFA enrollment changes (phase 7): the sealed-secret column,
    # --- its timestamps, and the operator's recovery clear — never the
    # --- TOTP secret itself, never a challenge code -----------------------
    AuditAction.MFA_ENROLLED: ReportArea.DATA_CHANGES,
    AuditAction.MFA_CLEARED: ReportArea.DATA_CHANGES,
    # --- the operator's pre-use acknowledgment of the risk-disclosure
    # --- statement (the 2026-10-04 rulings): the consent record itself —
    # --- the confirmed statement version, never paraphrased copy -----------
    AuditAction.RISK_ACKNOWLEDGMENT_ACCEPTED: ReportArea.DATA_CHANGES,
}


@dataclass(frozen=True)
class ChainVerdict:
    """The tenant-chain verification result the report embeds (ruling R6).

    ``problems`` is ``verify_chain``'s output verbatim over the tenant's
    full chain; ``checked_entries`` is how many entries that chain held, so
    the report names the population the verdict covers.
    """

    checked_entries: int
    problems: tuple[str, ...] = ()

    @property
    def verified(self) -> bool:
        return not self.problems


class ReportEntrySource(Protocol):
    """The fields the composer reads from a stored audit entry.

    Structural per ADR-0006: the ORM row satisfies this without the domain
    layer importing the adapter. ``entries`` arrive chain-ordered and
    company-filtered — this module composes, it does not query.
    """

    seq: int
    occurred_at: datetime
    actor_user_id: UUID | None
    action: str
    entity_type: str | None
    entity_id: UUID | None

    @property
    def details(self) -> Mapping[str, Any]: ...


def _entry_dict(entry: ReportEntrySource) -> dict[str, Any]:
    # AuditAction(action) raises ValueError on a value outside the closed
    # vocabulary — a stored row can never carry one, and a caller-side bug
    # fails loud here rather than misclassifying into a catch-all.
    area = ACTION_AREA[AuditAction(entry.action)]
    return {
        "seq": entry.seq,
        "occurred_at": jsonable(entry.occurred_at),
        "actor": None if entry.actor_user_id is None else str(entry.actor_user_id),
        "action": entry.action,
        "area": area.value,
        "entity_type": entry.entity_type,
        "entity_id": None if entry.entity_id is None else str(entry.entity_id),
        "details": jsonable(entry.details),
    }


def build_report(
    company_block: Mapping[str, Any],
    entries: Sequence[ReportEntrySource],
    chain_verdict: ChainVerdict,
    *,
    generated_at: datetime,
) -> dict[str, Any]:
    """The deterministic report dict; same inputs, same output.

    ``company_block`` is the identity block the application layer composed
    from master data (name, BR number, CR number); ``entries`` are the
    company's audit rows in chain order; ``chain_verdict`` is the tenant-wide
    verification result carried per ruling R6 — never a re-verification of
    the filtered subset.
    """
    return {
        "report_version": REPORT_VERSION,
        "generated_at": jsonable(generated_at),
        "company": dict(company_block),
        "chain": {
            "verified": chain_verdict.verified,
            "checked_entries": chain_verdict.checked_entries,
            "problems": list(chain_verdict.problems),
            "scope": CHAIN_SCOPE_NOTE,
        },
        "entries": [_entry_dict(entry) for entry in entries],
    }
