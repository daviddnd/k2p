"""Identity and access schema (scope §2.14, data-model §2.9).

Tenants own users; a user carries a role and (for General accounts) the four
per-account permission flags, defaulting to denied. Per-company access for
General accounts is granted through ``company_access_grants``.

``companies`` holds the static (non-time-bound) profile attributes
(data-model §2.3); the effective-dated fact tables live in
``company_master.py``.

``attribution_cases`` (ADR-0019 §1, migration ``0033``) sits here too: it is
the table the attribution registrations/confirmations slice's adapter reads
(``app.adapters.persistence.attribution``), and every module that touches it
imports the model from this one home.
"""

from datetime import date, datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.adapters.persistence.base import Base

_ROLE_CHECK = "role IN ('administrator', 'general')"


class Tenant(Base):
    __tablename__ = "tenants"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class User(Base):
    """A TCSP staff login. External contacts are never users (scope §2.14)."""

    __tablename__ = "users"
    __table_args__ = (
        UniqueConstraint("tenant_id", "email"),
        # Composite-FK target so company_access_grants can pin user and
        # tenant together.
        UniqueConstraint("id", "tenant_id"),
        CheckConstraint(_ROLE_CHECK, name="users_role_check"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"), index=True)
    email: Mapped[str] = mapped_column(String(320))
    display_name: Mapped[str] = mapped_column(String(200))
    role: Mapped[str] = mapped_column(String(20))
    # Per-account permission grants; fail-safe default is denied.
    review_events: Mapped[bool] = mapped_column(server_default="false")
    approve_events: Mapped[bool] = mapped_column(server_default="false")
    review_drafts: Mapped[bool] = mapped_column(server_default="false")
    approve_drafts: Mapped[bool] = mapped_column(server_default="false")
    is_active: Mapped[bool] = mapped_column(server_default="true")
    # Nullable: an identity record without credentials cannot log in (fail
    # closed). Set at creation; scrypt format in app.domain.auth.
    password_hash: Mapped[str | None] = mapped_column(String(255))
    # The TOTP enrollment (phase 7, migration 0031). ``totp_secret`` is the
    # D1 AES-GCM envelope sealed under KeyPurpose.totp_secret — plaintext
    # never lives in this column; NULL = not enrolled.
    # ``totp_last_step`` is the replay-containment watermark
    # app.domain.mfa.verify_totp returns; NULL until the first verify.
    totp_secret: Mapped[str | None] = mapped_column(Text)
    totp_enrolled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    totp_last_step: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Company(Base):
    """A company under this TCSP's management.

    Static (non-time-bound) profile attributes per data-model §2.3; time-bound
    facts live in the effective-dated tables in
    ``app.adapters.persistence.company_master``.
    """

    __tablename__ = "companies"
    __table_args__ = (
        # Composite-FK target so company_access_grants and the fact tables
        # can pin company and tenant together.
        UniqueConstraint("id", "tenant_id"),
        # One row per real company in a tenant's portfolio; the BRN is the
        # CSV-import match anchor. Application services normalize the format
        # (8 digits, zero-padded) before write so the constraint bites.
        UniqueConstraint("tenant_id", "br_number"),
        CheckConstraint(
            "type IN ('private', 'public', 'company_limited_by_guarantee')",
            name="companies_type_check",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"), index=True)
    # Nullable: NAR1 spec 2/2025 has no CR-number box; BRN is the on-form
    # identifier (data-model §2.3).
    cr_number: Mapped[str | None] = mapped_column(String(20))
    br_number: Mapped[str] = mapped_column(String(20))
    date_of_incorporation: Mapped[date] = mapped_column(Date)
    # NAR1 made-up-date anchor (data-model §2.3).
    anniversary_date: Mapped[date] = mapped_column(Date)
    type: Mapped[str] = mapped_column(String(30))
    # Per-company AI guardrail: blocks all LLM calls for the company (§7) —
    # the TCSP's objection-handling valve (ADR-0019 §2). The Q1.8 opt-in
    # register retired with the per-company consent gate (migration
    # ``0037_retire_ai_opt_in_register``): the columns, CHECKs and the
    # tenant-pinning FK are gone.
    ai_disabled: Mapped[bool] = mapped_column(server_default="false")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class CompanyAccessGrant(Base):
    """A General account's access to one company (scope §2.14 per-company scoping).

    One ``tenant_id`` per row: the composite foreign keys force the granted
    user and company into that same tenant, so a cross-tenant grant is
    uninsertable at the database level.
    """

    __tablename__ = "company_access_grants"
    __table_args__ = (
        ForeignKeyConstraint(["user_id", "tenant_id"], ["users.id", "users.tenant_id"]),
        ForeignKeyConstraint(["company_id", "tenant_id"], ["companies.id", "companies.tenant_id"]),
    )

    user_id: Mapped[UUID] = mapped_column(primary_key=True)
    company_id: Mapped[UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AuthSession(Base):
    """An issued staff login session.

    Only the SHA-256 digest of the opaque token is stored; the raw token is
    returned to the client once at login. One ``tenant_id`` per row: the
    composite foreign key pins user and tenant together, so a cross-tenant
    session is uninsertable at the database level (same pattern as
    ``company_access_grants``). Expiry is enforced at read time; there is no
    revocation endpoint in this slice — a session ends at ``expires_at`` or
    when the user is deactivated (RBAC denies inactive accounts).
    """

    __tablename__ = "auth_sessions"
    __table_args__ = (
        ForeignKeyConstraint(["user_id", "tenant_id"], ["users.id", "users.tenant_id"]),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column()
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"), index=True)
    token_digest: Mapped[str] = mapped_column(String(64), unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # False marks a session issued by password alone for an enrolled
    # Administrator — refused by the request edge until the TOTP challenge
    # passes. Server default true: sessions predating MFA stay valid (the
    # backfill guess migration 0031 states as one).
    mfa_verified: Mapped[bool] = mapped_column(server_default="true")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AttributionCase(Base):
    """One original message awaiting attribution, or resolved (ADR-0019 §1;
    migrations ``0033``/``0034``).

    The registration/confirmation flow's record, extended by the
    clarification loop (workstream ②): a threaded message whose sender is
    not a registered channel of the chain's company opens one row, and so
    does an unthreaded ambiguous or first-contact message — the latter two
    with **no company** (ruling D2), taking their company at resolution or
    closure. ``mailbox_message_id`` is unique per tenant — one case per
    original message, ever, the ``quarantined_messages`` discipline — and
    both anchor facts the thread is matched by live here (the provider
    ``thread_id`` and the original's RFC 5322 ``Message-ID``, either of
    which identifies the conversation). ``ask_kind``/``ask_sent_at`` are the
    ask side: the kind the decision chose, and NULL sent-time while an ask
    is still owed or was suppressed (ruling R3); a suppressed case stays
    open and visible.

    ``status`` and ``ask_kind`` are ``app.domain.attribution``'s enums; the
    DB CHECKs are the backstop, and they carry the vocabularies verbatim. The
    closure stamps are paired with their statuses the ``quarantined_messages``
    way — ``closed`` carries ``closed_at``, ``dismissed`` carries
    ``dismissed_at``, and no other status carries either — so a status can
    never be read against a stamp from another state.

    The two ``communication`` columns record what the case became: the
    confirming reply and the original's processed exchange, each pinned to
    this row's tenant by a composite foreign key (F1). The ``(id,
    tenant_id)`` unique key below is the composite target a request-less
    ``mail_outbox`` row's ``attribution_case_id`` points at (the
    ``companies``/``users`` pattern).
    """

    __tablename__ = "attribution_cases"
    __table_args__ = (
        # Composite-FK target so mail_outbox can pin case and tenant together.
        UniqueConstraint("id", "tenant_id"),
        ForeignKeyConstraint(
            ["company_id", "tenant_id"], ["companies.id", "companies.tenant_id"]
        ),
        ForeignKeyConstraint(
            ["resolved_communication_id", "tenant_id"],
            ["communications.id", "communications.tenant_id"],
        ),
        ForeignKeyConstraint(
            ["confirmation_communication_id", "tenant_id"],
            ["communications.id", "communications.tenant_id"],
        ),
        CheckConstraint(
            "status IN ('awaiting_registered_channel', 'awaiting_confirmation', "
            "'awaiting_clarification', 'resolved', 'closed', 'dismissed')",
            name="attribution_cases_status_check",
        ),
        CheckConstraint(
            "ask_kind IS NULL OR ask_kind IN ('registered_channel', 'confirmation', "
            "'clarification')",
            name="attribution_cases_ask_kind_check",
        ),
        # The status and its stamps agree (the 0016/0017 rule): the three
        # awaits and the resolved case carry neither stamp, and each closure
        # carries exactly the one its status names.
        CheckConstraint(
            "status IN ('awaiting_registered_channel', 'awaiting_confirmation', "
            "'awaiting_clarification', 'resolved')"
            " AND closed_at IS NULL AND dismissed_at IS NULL"
            " OR status = 'closed' AND closed_at IS NOT NULL AND dismissed_at IS NULL"
            " OR status = 'dismissed' AND dismissed_at IS NOT NULL AND closed_at IS NULL",
            name="attribution_cases_closed_at_check",
        ),
        # One case per provider message id per tenant, ever (a plain unique
        # index: the message id is NOT NULL, so there is no nullable case to
        # exclude).
        Index(
            "uq_attribution_cases_message_id_per_tenant",
            "tenant_id",
            "mailbox_message_id",
            unique=True,
        ),
        # The thread anchor's read, company-less since 0034 (ruling D2): the
        # suppression and processing queries filter by tenant, openness, and
        # one anchor — a company-less case's read uses this index too.
        Index("ix_attribution_cases_thread", "tenant_id", "status", "thread_id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"))
    # NULL while the case is company-less (the clarification loop's A/B
    # classes, ruling D2); the composite FK above is not checked for a NULL
    # (MATCH SIMPLE), and the company arrives at resolution/closure.
    company_id: Mapped[UUID | None] = mapped_column()
    # The two anchors the thread is matched by: the provider's thread id, and
    # the original message's RFC 5322 ``Message-ID`` (what a reply's
    # ``In-Reply-To``/``References`` carries). Either may be absent; matching
    # is equality, so a NULL anchor matches nothing.
    thread_id: Mapped[str | None] = mapped_column(String(128))
    original_rfc_message_id: Mapped[str | None] = mapped_column(String(320))
    # The provider's message id: what the row is keyed by, ever.
    mailbox_message_id: Mapped[str] = mapped_column(String(320))
    # The bare address, as ``communications`` stores it.
    sender_address: Mapped[str] = mapped_column(String(320))
    # app.domain.attribution.AttributionAskKind: the kind of ask this case
    # carries. NULL when the case carries none (the column is nullable); a
    # case whose ask was suppressed still has its kind.
    ask_kind: Mapped[str | None] = mapped_column(String(32))
    # When the ask left the deployment; NULL while it is still owed (or was
    # suppressed by an open asked case on the same anchor — ruling R3).
    ask_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # app.domain.attribution.AttributionCaseStatus; DB CHECK is the backstop.
    # 32, not the brief's 24: the longest status value
    # (``awaiting_registered_channel``) is 27 characters, and this is the
    # family's padded width (``ask_kind`` above) so the schema can store its
    # own vocabulary.
    status: Mapped[str] = mapped_column(String(32))
    # The exchange the original message became (R6: set with ``resolved``).
    resolved_communication_id: Mapped[UUID | None] = mapped_column()
    # The confirming reply's exchange (R5), recorded when the act lands.
    confirmation_communication_id: Mapped[UUID | None] = mapped_column()
    # Our clock, like every stamp here — the closure instants R7 and the
    # staff dismissal name, never a provider's receipt time.
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    dismissed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.clock_timestamp()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class SplitParent(Base):
    """One inbound message attributed to several of the sender's own
    companies (workstream ③, ADR-0019 §3; migration ``0036``).

    The split's parent row: one per provider message id (**unique per
    tenant** — one parent per message, ever, the quarantine/case
    discipline), carrying the envelope facts, the receipt time, and the WORM
    raw document the content lives on (F1 tenant pinning). Deliberately no
    ``company_id``: the parent is no company's record — the involved set
    lives on the work items, not here (ruling D6), and no ``Communication``
    invariant is touched. The status is the split's own closed vocabulary,
    the DB CHECK the backstop: ``split_pending`` (division owed),
    ``split_done`` (work items exist), ``staff_only`` (the pre-flight or a
    grammar violation parked it), ``failed``.
    """

    __tablename__ = "split_parents"
    __table_args__ = (
        ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        ForeignKeyConstraint(
            ["raw_document_id", "tenant_id"], ["documents.id", "documents.tenant_id"]
        ),
        # Composite-FK target for the work items' parent leg (the F1 pattern).
        UniqueConstraint("id", "tenant_id"),
        CheckConstraint(
            "status IN ('split_pending', 'split_done', 'staff_only', 'failed')",
            name="split_parents_status_check",
        ),
        Index(
            "uq_split_parents_message_id_per_tenant",
            "tenant_id",
            "mailbox_message_id",
            unique=True,
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(index=True)
    mailbox_message_id: Mapped[str] = mapped_column(String(320))
    sender_address: Mapped[str] = mapped_column(String(320))
    subject: Mapped[str] = mapped_column(String(512))
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    raw_document_id: Mapped[UUID] = mapped_column()
    # app.domain.split's vocabulary; the DB CHECK is the backstop.
    status: Mapped[str] = mapped_column(String(20))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.clock_timestamp()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class SplitWorkItem(Base):
    """One (parent, company) slice of a split message (workstream ③,
    migration ``0036``).

    The item IS its company's record: ``company_id`` NOT NULL under the
    composite foreign key into ``companies`` (F1 tenant pinning), so the
    per-company RBAC reads and the §5.1 gate's per-company leg work exactly
    as they do for any company record. The per-company restored slice is its
    own stored document (composite FK, WORM); the item's status
    (``awaiting_parse | parsed | failed``) is the per-company flow's own
    vocabulary, and the reply-parse work-item leg points back here. One item
    per (parent, company), ever — the unique constraint's own word.
    """

    __tablename__ = "split_work_items"
    __table_args__ = (
        ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        ForeignKeyConstraint(
            ["parent_id", "tenant_id"], ["split_parents.id", "split_parents.tenant_id"]
        ),
        ForeignKeyConstraint(
            ["company_id", "tenant_id"], ["companies.id", "companies.tenant_id"]
        ),
        ForeignKeyConstraint(
            ["slice_document_id", "tenant_id"], ["documents.id", "documents.tenant_id"]
        ),
        # Composite-FK target for the reply-parse work-item leg (the F1
        # pattern).
        UniqueConstraint("id", "tenant_id"),
        UniqueConstraint(
            "tenant_id", "parent_id", "company_id", name="uq_split_work_items_parent_company"
        ),
        CheckConstraint(
            "status IN ('awaiting_parse', 'parsed', 'failed')",
            name="split_work_items_status_check",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(index=True)
    parent_id: Mapped[UUID] = mapped_column()
    company_id: Mapped[UUID] = mapped_column()
    slice_document_id: Mapped[UUID] = mapped_column()
    # app.domain.split's item vocabulary; the DB CHECK is the backstop.
    status: Mapped[str] = mapped_column(String(20))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.clock_timestamp()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
