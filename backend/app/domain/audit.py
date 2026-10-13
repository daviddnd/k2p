"""Audit-log domain rules (ADR-0009; scope §2.13, §8).

Single owner of:

- the **action vocabulary** (``AuditAction``) — deliberately a domain enum,
  not a database CHECK: the vocabulary grows with every slice, and the
  append-only table must never be rewritten, only added to;
- the **hash-chain computation** (``compute_entry_hash``) — each entry's
  hash covers its full content plus the previous entry's hash, so any
  post-hoc edit, deletion, or reorder is detectable (scope §8);
- the **chain verifier** (``verify_chain``) — the pure check behind the
  scheduled integrity job (ADR-0009 / ADR-0011) and the scenario tests.

Chains are per tenant: a tenant's first entry has ``prev_hash = NULL`` and
every later entry links to its predecessor. The application layer
serializes appends per tenant before computing the link.

Pure and framework-free per ADR-0006: the import-linter contract forbids
framework and I/O imports in this layer.
"""

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from decimal import Decimal
from enum import Enum, StrEnum
from typing import Any, Protocol
from uuid import UUID


class AuditAction(StrEnum):
    """Every action that writes an audit entry (scope §2.13).

    New slices extend this vocabulary as they land; values are stored
    verbatim in ``audit_log.action``.
    """

    TENANT_CREATED = "tenant_created"
    USER_CREATED = "user_created"
    STAFF_LOGIN = "staff_login"
    COMPANY_CREATED = "company_created"
    COMPANY_STATICS_CORRECTED = "company_statics_corrected"
    COMPANY_FACT_RECORDED = "company_fact_recorded"
    COMPANIES_IMPORTED = "companies_imported"
    PERSON_CREATED = "person_created"
    PERSON_UPDATED = "person_updated"
    CONTACT_CHANNEL_ADDED = "contact_channel_added"
    CONTACT_CHANNEL_UPDATED = "contact_channel_updated"
    COMPANY_CONTACT_LINKED = "company_contact_linked"
    COMPANY_CONTACT_UNLINKED = "company_contact_unlinked"
    NAMING_TEMPLATES_UPDATED = "naming_templates_updated"
    TENANT_MILESTONES_UPDATED = "tenant_milestones_updated"
    TENANT_SEND_MODES_UPDATED = "tenant_send_modes_updated"
    SERVICE_PACKAGE_CREATED = "service_package_created"
    SERVICE_PACKAGE_UPDATED = "service_package_updated"
    SERVICE_PACKAGE_DELETED = "service_package_deleted"
    SERVICE_ASSIGNMENT_CREATED = "service_assignment_created"
    SERVICE_ASSIGNMENT_UPDATED = "service_assignment_updated"
    DOCUMENT_REGISTERED = "document_registered"
    DOCUMENT_DOWNLOADED = "document_downloaded"
    PRIOR_FILING_EXTRACTED = "prior_filing_extracted"
    IMPORT_RECORD_CONFIRMED = "import_record_confirmed"
    IMPORT_RECORD_REJECTED = "import_record_rejected"
    CONTROL_PLANE_ACTIVATED = "control_plane_activated"
    CONTROL_PLANE_DEACTIVATED = "control_plane_deactivated"
    COMMAND_EXECUTED = "command_executed"
    COMMAND_FAILED = "command_failed"
    COMMAND_REJECTED = "command_rejected"
    CALENDAR_FEED_CREATED = "calendar_feed_created"
    CALENDAR_FEED_REVOKED = "calendar_feed_revoked"
    CALENDAR_PROVIDER_CONNECTED = "calendar_provider_connected"
    CALENDAR_PROVIDER_DISCONNECTED = "calendar_provider_disconnected"
    PACK_INSTALLED = "pack_installed"
    PACK_REFRESH_REFUSED = "pack_refresh_refused"
    PACKS_FROZEN = "packs_frozen"
    PACKS_UNFROZEN = "packs_unfrozen"
    MAIL_CONNECTION_CREATED = "mail_connection_created"
    MAIL_CONNECTION_REMOVED = "mail_connection_removed"
    MAIL_MESSAGE_INGESTED = "mail_message_ingested"
    MAIL_MESSAGE_QUARANTINED = "mail_message_quarantined"
    MAIL_QUARANTINE_RESOLVED = "mail_quarantine_resolved"
    MAIL_QUARANTINE_DISMISSED = "mail_quarantine_dismissed"
    DOCUMENT_SCANNED = "document_scanned"
    CONFIRMATION_REQUEST_DRAFTED = "confirmation_request_drafted"
    CONFIRMATION_REQUEST_RELEASED = "confirmation_request_released"
    MAIL_MESSAGE_SENT = "mail_message_sent"
    MAIL_SEND_FAILED = "mail_send_failed"
    SERVICE_RENEWAL_RECORDED = "service_renewal_recorded"
    DOCUMENT_RELEASE_REFUSED = "document_release_refused"
    CHANGE_EVENT_RECORDED = "change_event_recorded"
    CHANGE_EVENT_TRANSITION_RECORDED = "change_event_transition_recorded"
    OFFICER_APPOINTMENT_CREATED = "officer_appointment_created"
    OFFICER_APPOINTMENT_UPDATED = "officer_appointment_updated"
    OFFICER_APPOINTMENT_DELETED = "officer_appointment_deleted"
    SHAREHOLDING_CREATED = "shareholding_created"
    SHAREHOLDING_UPDATED = "shareholding_updated"
    SHAREHOLDING_DELETED = "shareholding_deleted"
    DRAFT_GENERATED = "draft_generated"
    DRAFT_TRANSITION_RECORDED = "draft_transition_recorded"
    DRAFT_INVALIDATED = "draft_invalidated"
    DRAFT_SIGNATURE_PACKAGE_GENERATED = "draft_signature_package_generated"
    DRAFT_EXPORTED = "draft_exported"
    REMINDER_ENQUEUED = "reminder_enqueued"
    REMINDERS_EXHAUSTED = "reminders_exhausted"
    COMPANY_AI_DISABLED_UPDATED = "company_ai_disabled_updated"
    LLM_CALL = "llm_call"
    REPLY_PARSED = "reply_parsed"
    RECONFIRMATION_ENQUEUED = "reconfirmation_enqueued"
    RECONFIRMATION_RELEASED = "reconfirmation_released"
    INTEGRITY_CHECK = "integrity_check"
    ENCRYPTION_KEY_CREATED = "encryption_key_created"
    MFA_ENROLLED = "mfa_enrolled"
    MFA_CLEARED = "mfa_cleared"
    RISK_ACKNOWLEDGMENT_ACCEPTED = "risk_acknowledgment_accepted"
    ATTRIBUTION_CASE_OPENED = "attribution_case_opened"
    ATTRIBUTION_ASK_ENQUEUED = "attribution_ask_enqueued"
    ATTRIBUTION_CONFIRMED = "attribution_confirmed"
    ATTRIBUTION_CASE_CLOSED = "attribution_case_closed"
    ATTRIBUTION_CASE_RESOLVED = "attribution_case_resolved"
    ATTRIBUTION_CASE_DISMISSED = "attribution_case_dismissed"
    # The multi-company split (workstream ③, ADR-0019 §3; ruling D10): the
    # parent's own events write the company column NULL (the parent is no
    # company's record), the item events carry the item company's id.
    SPLIT_PARENT_CREATED = "split_parent_created"
    SPLIT_PARENT_STAFF_ONLY = "split_parent_staff_only"
    SPLIT_PARENT_FAILED = "split_parent_failed"
    SPLIT_PARENT_DONE = "split_parent_done"
    SPLIT_ITEM_CREATED = "split_item_created"


def jsonable(value: Any) -> Any:
    """Convert domain value types to their canonical JSON form.

    Applied to ``details``/``llm_call`` before storage *and* before
    hashing, so the hashed content is exactly the stored content. UUIDs
    and enums become strings, dates/datetimes ISO strings, Decimals
    strings; mappings and sequences are converted recursively.
    """
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(item) for item in value]
    return value


def compute_entry_hash(
    *,
    tenant_id: UUID,
    occurred_at: datetime,
    actor_user_id: UUID | None,
    company_id: UUID | None,
    action: str,
    entity_type: str | None,
    entity_id: UUID | None,
    details: Any,
    llm_call: Any,
    prev_hash: str | None,
) -> str:
    """The SHA-256 of the entry's canonical serialization (hex digest).

    The hash covers every content field plus ``prev_hash`` — the chain
    link — so altering any field, breaking a link, or reordering entries
    is detectable by recomputation. Canonicalization is a fixed-key JSON
    dump with sorted keys and no insignificant whitespace; ``details`` and
    ``llm_call`` must already be in ``jsonable`` form (the application
    layer stores exactly what it hashes).
    """
    canonical = json.dumps(
        {
            "action": action,
            "actor_user_id": actor_user_id,
            "company_id": company_id,
            "details": details,
            "entity_id": entity_id,
            "entity_type": entity_type,
            "llm_call": llm_call,
            "occurred_at": occurred_at,
            "prev_hash": prev_hash,
            "tenant_id": tenant_id,
        },
        default=jsonable,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class ChainEntry(Protocol):
    """The fields the chain verifier reads from a stored entry.

    Structural per ADR-0006: the ORM row satisfies this without the
    domain layer importing the adapter.
    """

    tenant_id: UUID
    occurred_at: datetime
    actor_user_id: UUID | None
    company_id: UUID | None
    action: str
    entity_type: str | None
    entity_id: UUID | None
    details: Any
    llm_call: Any
    prev_hash: str | None
    entry_hash: str


def verify_chain(entries: Sequence[ChainEntry]) -> list[str]:
    """Verify one tenant's chain in chain order; every violation found.

    An empty list means the chain is intact. ``entries`` must be ordered
    by insertion (``seq``); the first entry must have ``prev_hash``
    ``None`` (tenant genesis) and every later entry must link to its
    predecessor and hash to its stored ``entry_hash``.
    """
    problems: list[str] = []
    previous_hash: str | None = None
    for index, entry in enumerate(entries):
        if entry.prev_hash != previous_hash:
            problems.append(f"entry {index}: prev_hash does not link to the previous entry")
        recomputed = compute_entry_hash(
            tenant_id=entry.tenant_id,
            occurred_at=entry.occurred_at,
            actor_user_id=entry.actor_user_id,
            company_id=entry.company_id,
            action=entry.action,
            entity_type=entry.entity_type,
            entity_id=entry.entity_id,
            details=entry.details,
            llm_call=entry.llm_call,
            prev_hash=entry.prev_hash,
        )
        if recomputed != entry.entry_hash:
            problems.append(f"entry {index}: entry_hash does not match its contents")
        previous_hash = entry.entry_hash
    return problems
