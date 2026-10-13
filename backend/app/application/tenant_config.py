"""Tenant-configuration orchestration (data-model §2.9 ``TenantConfig``).

The single read/write path for per-tenant configuration: the naming/storage
templates (contract §5.3; ADR-0013 item 1) and the milestone block (scope §2.4,
§2.15; the founder's ruling of 2026-09-24).

- ``get_tenant_config`` is the **configuration screen's** read: every block plus
  when the row was last written, or the shipped defaults of all of them when the
  tenant has no row yet (fail-safe default: the shipped templates, milestones
  and send modes are always valid and attributable). It does not fail for a stored
  block this build cannot read — it substitutes that block's shipped defaults
  and **says which block was unreadable and why** (``unreadable_blocks``), so
  the screen that can repair the row is not taken down by the row it has to
  repair. The fallback is loud, never silent: a reader that ignores the field
  would be reading numbers nobody chose, which is why the field is part of the
  view rather than a log line.
- ``get_naming_templates`` is the document store's narrow read — the templates
  alone, because that is what a storage key needs and a milestone block has no
  business failing a document write.
- ``get_milestones`` is the same narrow read for the consumers that derive dates
  from the block (the §5.1.9 panel, the confirmation trigger): the milestone
  block alone, and it **fails closed** with the block's own validation error,
  because a date the product cannot derive must stop the read that would print
  it rather than fall back to numbers nobody configured.
- ``get_send_modes`` is that read for the block that decides **whether a message
  leaves without a person** (``app.domain.send_modes``, the founder's ruling of
  2026-09-24): the release paths read it, and it fails closed for the same
  reason one step stronger — silently substituting the shipped modes could
  substitute ``automatic``.
- ``put_tenant_config`` validates every block the request carries through its own
  single domain rule and upserts the tenant's row: one request, one row, one
  transaction, and one audit entry per block the request actually carried — each
  with its own before/after, because those blocks are replaced whole
  (``send_modes`` is optional: a request that omits it leaves the stored block
  untouched).

Validation is full-replace for the blocks the request carries — if it carries
them, they *are* the tenant's configuration, each validated whole, and a
violation raises with every violation listed; nothing is written. A stored block
is re-validated on read (defense in depth, the naming-template precedent). What a
violated block then does depends on the reader, and deliberately: the narrow
reads (``get_naming_templates``, ``get_milestones``, ``get_send_modes``) fail
closed — an invalid template must not key a document, an invalid offset must not
date a milestone, an invalid mode must not decide a release — while the
configuration screen's read substitutes the shipped defaults and names the
problem, because that screen is the repair path and a read that cannot load the
templates cannot assemble the full-replace ``PUT``. A PUT does not validate what
it replaces: overwriting a corrupted row is how a human repairs it, and the audit
entry carries what was there. **The optional ``send_modes`` block is the
exception**: a request that omits it keeps the stored block rather than resetting
it, and the keep-branch meets an unreadable stored block the way ``GET`` does —
the shipped defaults stand in for it in the returned view and the view names the
block and its violations in ``unreadable_blocks`` — instead of raising the
block's validation error (the route's 422-as-request-error). Tolerating it is
what makes the omission a repair: an unreadable block is only ever replaced by a
request that **carries** a valid one, and a client built before the block existed
could not otherwise repair the row (follow-up (2), closed 2026-10-05; contract
§5.3 states the behaviour).

Template changes apply to new objects only — WORM originals are never renamed
or rewritten (ADR-0009). Services flush but never commit — the API session
dependency commits at the request edge. The write path emits its audit events
on the same session via ``app.application.audit.record_event``.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.persistence.tenant_config import TenantConfig
from app.application import audit
from app.domain.audit import AuditAction
from app.domain.milestones import (
    Milestones,
    MilestoneValidationError,
    default_milestones,
    validate_milestones,
)
from app.domain.naming_templates import (
    NamingTemplates,
    TemplateValidationError,
    default_naming_templates,
    validate_naming_templates,
)
from app.domain.send_modes import (
    SendModes,
    SendModesValidationError,
    default_send_modes,
    validate_send_modes,
)


class ConfigBlock(StrEnum):
    """The three blocks the stored configuration holds, as a block is named.

    The vocabulary exists so "which block could not be read" is a closed set
    rather than a string a caller compares by hand: the configuration screen
    (and the client of ``GET /tenant-config``) needs to know *what* was
    unreadable before it can say what a repair has to fix.
    """

    NAMING_TEMPLATES = "naming_templates"
    MILESTONES = "milestones"
    SEND_MODES = "send_modes"


@dataclass(frozen=True)
class UnreadableBlock:
    """A stored block this build could not read, and the rule's own violations.

    ``violations`` are the block's domain rule's messages — field names and
    rules, never client data — and they are what the repair has to satisfy:
    the ``PUT`` validates its own request, so the list is the specification of
    a valid replacement, not only a diagnosis.
    """

    block: ConfigBlock
    violations: tuple[str, ...]


@dataclass(frozen=True)
class NamingTemplatesView:
    """The document store's narrow read: the effective templates plus when they
    were set.

    ``updated_at`` is ``None`` when the tenant has no stored row and the
    shipped defaults are in effect.
    """

    naming_templates: NamingTemplates
    updated_at: datetime | None


@dataclass(frozen=True)
class TenantConfigView:
    """The whole tenant configuration: every block, and when it was written.

    ``updated_at`` is ``None`` when the tenant has no stored row and the shipped
    defaults of every block are in effect.

    ``unreadable_blocks`` is empty whenever the stored blocks all validated.
    A non-empty entry means the block it names did **not**: the value in this
    view for that block is the **shipped default**, not what the tenant stored,
    and the violations are why. The pairing is the point — a silent fallback
    would hide a real integrity problem behind a working screen — so a caller
    that needs the block itself must treat a non-empty list as its own
    condition, or use the narrow reads (``get_naming_templates``,
    ``get_milestones``, ``get_send_modes``), which fail closed instead.
    """

    naming_templates: NamingTemplates
    milestones: Milestones
    send_modes: SendModes
    updated_at: datetime | None
    unreadable_blocks: tuple[UnreadableBlock, ...] = ()


async def _get_row(session: AsyncSession, *, tenant_id: UUID) -> TenantConfig | None:
    row = await session.scalar(select(TenantConfig).where(TenantConfig.tenant_id == tenant_id))
    return row


def _templates_from(row: TenantConfig) -> NamingTemplates:
    """Re-validate the stored pair on read (defense in depth).

    The column is only ever written post-validation, but a hand-edited row
    fails closed here rather than handing an invalid template to the
    document store.
    """
    stored = row.naming_templates
    # Missing keys are treated as empty templates, which fail validation
    # cleanly (a structurally-corrupted row must not raise a bare KeyError).
    return validate_naming_templates(
        document=str(stored.get("document") or ""), generated=str(stored.get("generated") or "")
    )


def _milestones_from(row: TenantConfig) -> Milestones:
    """Re-validate the stored block on read, for the same reason.

    A row whose block is missing or malformed fails as a clean validation
    error naming every field it could not read, never as a ``KeyError`` or as
    arithmetic on a string.
    """
    return validate_milestones(row.milestones or {})


def _send_modes_from(row: TenantConfig) -> SendModes:
    """The stored send-mode block, re-validated on read (defense in depth).

    A block a later schema change invalidated, or a hand-edited row, fails
    here as the rule's own violations — and one violation in particular matters
    more than the rest: a stored ``automatic`` for the government destination
    is refused by the same rule that refuses the write, so a bypassed CHECK or
    a hand-edited row cannot arm it.
    """
    return validate_send_modes(row.send_modes or {})


def _templates_or_default(row: TenantConfig) -> tuple[NamingTemplates, UnreadableBlock | None]:
    """The stored template pair, or the shipped defaults plus what was wrong.

    The configuration screen's half of the tolerant read: it may show defaults
    for a block the row cannot prove, but it must say so, so that the reader
    knows the values are not the tenant's.
    """
    try:
        return _templates_from(row), None
    except TemplateValidationError as exc:
        return (
            default_naming_templates(),
            UnreadableBlock(block=ConfigBlock.NAMING_TEMPLATES, violations=exc.errors),
        )


def _milestones_or_default(row: TenantConfig) -> tuple[Milestones, UnreadableBlock | None]:
    """The stored milestone block, or the shipped defaults plus what was wrong."""
    try:
        return _milestones_from(row), None
    except MilestoneValidationError as exc:
        return (
            default_milestones(),
            UnreadableBlock(block=ConfigBlock.MILESTONES, violations=exc.errors),
        )


def _send_modes_or_default(row: TenantConfig) -> tuple[SendModes, UnreadableBlock | None]:
    """The stored send-mode block, or the shipped defaults plus what was wrong."""
    try:
        return _send_modes_from(row), None
    except SendModesValidationError as exc:
        return (
            default_send_modes(),
            UnreadableBlock(block=ConfigBlock.SEND_MODES, violations=exc.errors),
        )


async def get_tenant_config(session: AsyncSession, *, tenant_id: UUID) -> TenantConfigView:
    """The configuration screen's read: the effective configuration, and what
    of it could not be read.

    **This is the one read that does not fail closed on a corrupted block**, and
    deliberately: it is the read the repair is assembled from. A full-replace
    ``PUT`` carries the blocks it is assembled from, so a read that refused while
    the row was unreadable would leave the operator able to see the problem and
    unable to repair the blocks it can carry (the optional ``send_modes`` block's
    own exception is at the top of this module). The substitution is stated, not
    hidden — the unreadable block's shipped default travels with the block it
    stands in for and with the violations the replacement must satisfy
    (``unreadable_blocks``).

    The reads whose output is *used* rather than displayed keep the strict
    behaviour, and they are the reason this one can relax: ``get_milestones``,
    ``get_naming_templates`` and ``get_send_modes`` refuse a stored block they
    cannot validate, so no date is derived, no object keyed and no release
    decided from a block nobody can read.
    """
    row = await _get_row(session, tenant_id=tenant_id)
    if row is None:
        return TenantConfigView(
            naming_templates=default_naming_templates(),
            milestones=default_milestones(),
            send_modes=default_send_modes(),
            updated_at=None,
        )
    templates, template_problem = _templates_or_default(row)
    milestones, milestone_problem = _milestones_or_default(row)
    send_modes, modes_problem = _send_modes_or_default(row)
    problems = tuple(
        problem
        for problem in (template_problem, milestone_problem, modes_problem)
        if problem is not None
    )
    return TenantConfigView(
        naming_templates=templates,
        milestones=milestones,
        send_modes=send_modes,
        updated_at=row.updated_at,
        unreadable_blocks=problems,
    )


async def get_naming_templates(session: AsyncSession, *, tenant_id: UUID) -> NamingTemplatesView:
    """The document store's narrow read (stored templates or shipped defaults).

    Fails closed: a stored pair the §5.3 rule refuses raises here rather than
    letting a document store key objects under a template nobody can read (the
    module docstring's rule, and the reason a document write reads this and not
    the configuration screen's view).
    """
    row = await _get_row(session, tenant_id=tenant_id)
    if row is None:
        return NamingTemplatesView(naming_templates=default_naming_templates(), updated_at=None)
    return NamingTemplatesView(naming_templates=_templates_from(row), updated_at=row.updated_at)


async def get_milestones(session: AsyncSession, *, tenant_id: UUID) -> Milestones:
    """The milestone block alone, for the consumers that derive dates from it.

    The §5.1.9 panel and the confirmation trigger need the block's numbers and
    nothing else, so they read the block and not the whole configuration: a
    stored *template* pair nobody can read has no business taking a panel read
    down, and a stored *milestone* block nobody can read must stop the read
    rather than be replaced with numbers the tenant did not choose. Fails closed
    with ``MilestoneValidationError``, whose violations name every field that
    failed (the callers answer ``409 tenant_config_invalid: <violations>``).
    """
    row = await _get_row(session, tenant_id=tenant_id)
    if row is None:
        return default_milestones()
    return _milestones_from(row)


async def get_send_modes(session: AsyncSession, *, tenant_id: UUID) -> SendModes:
    """The send-mode block alone, for the paths that release a message.

    The same narrow read the milestone block gets, and fail-closed for the same
    reason one step stronger: these values decide **whether a message leaves
    the deployment without a person**, so a block nobody can read must stop the
    release rather than be replaced with modes the tenant did not choose —
    silently falling back to ``automatic`` would send client mail on the
    strength of a row nobody can parse. Fails closed with
    ``SendModesValidationError`` (the callers answer
    ``409 tenant_config_invalid: <violations>``, the milestone block's own
    refusal).
    """
    row = await _get_row(session, tenant_id=tenant_id)
    if row is None:
        return default_send_modes()
    return _send_modes_from(row)


async def put_tenant_config(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    actor_user_id: UUID | None,
    document: str,
    generated: str,
    milestones: Mapping[str, Any],
    send_modes: Mapping[str, Any] | None = None,
) -> TenantConfigView:
    """Validate and store the tenant's configuration (full replace, per block).

    Each block is validated by its own single domain rule: the templates per
    §5.3 (``generated`` must carry ``{Version}``, both must carry
    ``{CompanyCode}`` or ``{CR}``), the milestone block per
    ``app.domain.milestones`` (coherent ordering, bounds, a cadence strictly
    increasing, and a reply offset that leaves the client their minimum reply
    window before CR's statutory deadline), and the send modes per
    ``app.domain.send_modes`` (the closed pair of modes, and the government
    destination's pinned value). Raises the block's own validation error with
    every violation; nothing is written on failure.

    **An absent ``send_modes`` keeps the stored block** rather than resetting it
    to the shipped defaults — the one deliberate exception to full-replace, and
    the reason this block is optional while the other two are not: a client
    built before this block existed (the milestone screen is one) sends the two
    blocks it knows about, and treating that as "reset the send modes" would
    silently re-arm automatic client sending that an Administrator had turned
    off. An explicit block is replaced whole, like the others.
    """
    templates = validate_naming_templates(document=document, generated=generated)
    block = validate_milestones(milestones)
    row = await _get_row(session, tenant_id=tenant_id)
    before_templates = (
        None
        if row is None
        else {
            "document": row.naming_templates["document"],
            "generated": row.naming_templates["generated"],
        }
    )
    before_milestones = None if row is None else dict(row.milestones)
    if send_modes is not None:
        modes = validate_send_modes(send_modes)
        before_modes: dict[str, Any] | None = (
            None if row is None else dict(row.send_modes)
        )
        kept_unreadable: UnreadableBlock | None = None
    elif row is None:
        modes = default_send_modes()
        before_modes = None
        kept_unreadable = None
    else:
        # The keep-branch tolerance (the module docstring's rule): an unreadable
        # stored block is tolerated the way ``GET`` tolerates it — shipped
        # defaults plus an ``unreadable_blocks`` entry — instead of raising the
        # block's validation error, because the omission cannot repair the
        # block by resetting it and a client built before the block existed
        # has no other way to repair the row.
        modes, kept_unreadable = _send_modes_or_default(row)
        before_modes = None  # untouched: this PUT did not carry the block
    if row is None:
        row = TenantConfig(
            tenant_id=tenant_id,
            naming_templates={"document": templates.document, "generated": templates.generated},
            milestones=block.as_dict(),
            send_modes=modes.as_dict(),
        )
        session.add(row)
    else:
        row.naming_templates = {"document": templates.document, "generated": templates.generated}
        row.milestones = block.as_dict()
        if send_modes is not None:
            row.send_modes = modes.as_dict()
    await session.flush()
    # The server-side onupdate expires updated_at on the update path; refresh
    # so the response carries the stored timestamp rather than triggering a
    # lazy load.
    await session.refresh(row)
    await audit.record_event(
        session,
        tenant_id=tenant_id,
        action=AuditAction.NAMING_TEMPLATES_UPDATED,
        actor_user_id=actor_user_id,
        entity_type="tenant_config",
        # None = the shipped §5.3 defaults were in effect (no stored row).
        details={
            "before": before_templates,
            "after": {"document": templates.document, "generated": templates.generated},
        },
    )
    await audit.record_event(
        session,
        tenant_id=tenant_id,
        action=AuditAction.TENANT_MILESTONES_UPDATED,
        actor_user_id=actor_user_id,
        entity_type="tenant_config",
        # None = the shipped milestones were in effect (no stored row). The
        # before side is the stored block as it was, unvalidated: a PUT is also
        # how a corrupted row is replaced, and the entry says what was there.
        details={"before": before_milestones, "after": block.as_dict()},
    )
    if send_modes is not None:
        # One entry per block this PUT actually replaced; a request that did
        # not carry the block did not touch it, and an entry claiming it did
        # would be the chain's one lie.
        await audit.record_event(
            session,
            tenant_id=tenant_id,
            action=AuditAction.TENANT_SEND_MODES_UPDATED,
            actor_user_id=actor_user_id,
            entity_type="tenant_config",
            details={"before": before_modes, "after": modes.as_dict()},
        )
    return TenantConfigView(
        naming_templates=templates,
        milestones=block,
        send_modes=modes,
        updated_at=row.updated_at,
        unreadable_blocks=(kept_unreadable,) if kept_unreadable is not None else (),
    )
