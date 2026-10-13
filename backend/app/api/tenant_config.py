"""Tenant-configuration endpoints (data-model §5.1 ``GET/PUT /tenant-config``;
§2.9 ``TenantConfig``; contract §5.3).

Implements the naming/storage-template piece of tenant configuration
(scope §2.15; ADR-0013 item 1) and the milestone panel (scope §2.4; the
founder's ruling of 2026-09-24: every client-facing date is a configured number
of days before the company's anniversary date). Both routes are
Administrator-only via ``require_admin``: the scope assigns configuration to
the Administrator, and tenant-level settings have no per-company grant
semantics (fail-closed default). The body carries no ``tenant_id`` — the config
belongs to the actor's own tenant, so cross-tenant access is impossible by
construction.

PUT carries the configuration and replaces the blocks the body carries — the
naming templates and the milestones, plus the optional send-mode block when it
is sent; each block is validated by its own single domain rule
(``app.domain.naming_templates``, ``app.domain.milestones``,
``app.domain.send_modes``) before anything is written — 422 listing every
violation of the first block that fails, nothing written. A tenant without a
stored row reads the shipped defaults of every block. Template changes apply to
new objects only (ADR-0009 WORM). PUT emits its audit events in the same
transaction (ADR-0009), one per block the request carried.

**A stored block this build cannot read is named, not hidden — and not allowed
to take a read down.** The panel and the confirmation trigger re-validate the
block they derive dates from and answer **409 ``tenant_config_invalid:
<violations>``** when it fails (they read it through
``tenant_config.get_milestones``, the narrow read that fails closed). This
route is the exception, and it is the one that has to be: `GET` is how the
configuration a full-replace `PUT` carries is assembled, so refusing would
leave the operator able to see the problem and unable to repair it. It answers
the effective configuration with the **shipped defaults standing in for the
unreadable block**, names the block in ``unreadable_blocks`` and lists the
violations the replacement has to satisfy. The fallback is loud by
construction — a client that read the numbers and not the field would be
reading numbers the tenant did not choose — and the `PUT` still validates its
own request and replaces the blocks it carries without reading the stored row,
which is what makes the repair a single request for those blocks. The optional
`send_modes` block is the exception: a request that omits it keeps what is
stored, so a stored block nobody can read is tolerated the way `GET` tolerates
it — the response carries the shipped defaults for the block and names it in
`unreadable_blocks` — never reset (an omission cannot repair a block by
resetting it), and never a 422-as-request-error: repairing the block itself
still takes a request that carries a valid one.
"""

from collections.abc import Sequence
from datetime import datetime
from typing import Annotated, Protocol

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.persistence.session import get_session
from app.api.deps import ActorRef, get_current_actor, require_admin
from app.application import tenant_config
from app.domain.milestones import MilestoneValidationError
from app.domain.naming_templates import TemplateValidationError
from app.domain.rbac import ActorContext
from app.domain.send_modes import SendModesValidationError

router = APIRouter(prefix="/api/v1", tags=["tenant-config"])

# The refusal for a stored configuration this build cannot read. It is a
# conflict with what the deployment holds rather than a client error: the
# request is fine, the row is not.
TENANT_CONFIG_INVALID = "tenant_config_invalid"


class StoredBlockError(Protocol):
    """A configuration block's own validation error, as this layer reads it.

    Each block's rule raises its own type (``MilestoneValidationError``,
    ``SendModesValidationError``…); what they share is an ``errors`` sequence of
    violations, field by field, and that is all the refusal needs. Structural
    rather than a union, so a block added later joins the mapping without
    editing it.
    """

    @property
    def errors(self) -> Sequence[str]: ...


def invalid_stored_config(error: "StoredBlockError") -> HTTPException:
    """The typed refusal for a stored configuration block that does not validate.

    One mapping for the reads that *derive* from a block and must therefore fail
    closed — the §5.1.9 panel, the confirmation trigger and the release paths —
    because they fail for one reason and the caller's remedy is the same: a PUT
    with a valid block (which does not read the stored one), or an operator's
    repair of the row. The configuration read (`GET /tenant-config`) is
    deliberately not one of them: it is where that PUT is assembled, and it
    names the unreadable block in its response instead (see the module
    docstring). The violations travel in the detail: they are field names and
    rules, never client data.
    """
    return HTTPException(
        status_code=409,
        detail=f"{TENANT_CONFIG_INVALID}: {'; '.join(error.errors)}",
    )


class NamingTemplatesPayload(BaseModel):
    """The §5.3 template pair; PUT replaces both (full-replace semantics)."""

    document: str = Field(min_length=1)
    generated: str = Field(min_length=1)


class MilestonesPayload(BaseModel):
    """The §2.9 milestone block; PUT replaces it whole (full-replace semantics).

    The declared types are this model's; the *rules* — coherent ordering,
    bounds, a strictly increasing cadence, the anchor vocabulary, and the reply
    offset's bound against the statutory window — are the single domain
    validation (``app.domain.milestones``), so a violation is answered with
    every violation listed rather than as this layer's own body.
    """

    continuation_request_days_before: int
    data_confirmation_days_before: int
    client_reply_days_before: int
    signature_post_days_before: int
    minimum_reply_window_days: int
    reminder_cadence_days: list[int]
    anchor: str


class SendModesPayload(BaseModel):
    """The §5.3 send-mode block: who releases a message, per destination.

    The declared types are this model's; the *rules* — the closed pair of
    modes, and the government destination's pinned ``review_required`` — are
    the single domain validation (``app.domain.send_modes``), so a write that
    asks for ``automatic`` there is refused with the rule's own message rather
    than this layer's.
    """

    client: str
    government: str


class TenantConfigRequest(BaseModel):
    """The whole per-tenant configuration; PUT replaces each block it carries.

    ``send_modes`` is **optional**, and its absence keeps the stored block:
    a client built before the block existed (the milestone screen is one)
    carries the two blocks it knows about, and reading that as "reset the send
    modes" would silently re-arm automatic client sending that an
    Administrator had turned off. An explicit block is replaced whole.
    """

    naming_templates: NamingTemplatesPayload
    milestones: MilestonesPayload
    send_modes: SendModesPayload | None = None


class UnreadableBlockPayload(BaseModel):
    """A stored block this build could not read, and why.

    ``violations`` are the block's own rule's messages, field by field — the
    same list the panel's `409 tenant_config_invalid` carries — and they are
    the specification of a replacement the `PUT` will accept as much as a
    diagnosis. Never client data: a rule name and a field name.
    """

    block: tenant_config.ConfigBlock
    violations: list[str]


class TenantConfigResponse(BaseModel):
    """The whole configuration, and what of it could not be read.

    ``unreadable_blocks`` is empty whenever the stored blocks all validated. A
    non-empty entry means the named block's value **in this response** is the
    shipped default rather than what the tenant stored, and the entry's
    violations are why — the fallback is stated so a screen cannot present
    numbers or modes nobody chose as the tenant's own. The repair is the
    ordinary `PUT`: it validates its own request and replaces the blocks it
    carries without reading the stored row (the optional `send_modes` block is
    the exception — a request that omits it keeps what is stored), which is
    exactly why this read must still answer.
    """

    naming_templates: NamingTemplatesPayload
    milestones: MilestonesPayload
    send_modes: SendModesPayload
    updated_at: datetime | None
    unreadable_blocks: list[UnreadableBlockPayload]


def _response(view: tenant_config.TenantConfigView) -> TenantConfigResponse:
    return TenantConfigResponse(
        naming_templates=NamingTemplatesPayload(
            document=view.naming_templates.document,
            generated=view.naming_templates.generated,
        ),
        milestones=MilestonesPayload(**view.milestones.as_dict()),
        send_modes=SendModesPayload(**view.send_modes.as_dict()),
        updated_at=view.updated_at,
        unreadable_blocks=[
            UnreadableBlockPayload(block=problem.block, violations=list(problem.violations))
            for problem in view.unreadable_blocks
        ],
    )


@router.get("/tenant-config")
async def get_tenant_config(
    actor: Annotated[ActorRef, Depends(get_current_actor)],
    _: Annotated[ActorContext, Depends(require_admin())],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> TenantConfigResponse:
    """The tenant's effective configuration: every block, stored or shipped.

    A stored block this build cannot read does **not** fail this read: the
    block's shipped default stands in for it and the response says so, naming
    the block and every violation (`unreadable_blocks`) — a hand-edited row, or
    a block a later schema change invalidated. That is the repair path: this
    read is what a full-replace `PUT` is assembled from, so a refusal here
    would leave the operator able to see the problem and unable to repair the
    blocks the request can carry (the optional `send_modes` block is the
    exception — a request that omits it keeps what is stored). The
    fallback is never silent — the field is the difference between "the tenant
    stored this" and "this is the shipped default" — and `updated_at` stays the
    stored row's timestamp, because the row exists.
    """
    view = await tenant_config.get_tenant_config(session, tenant_id=actor.tenant_id)
    return _response(view)


@router.put("/tenant-config")
async def put_tenant_config(
    payload: TenantConfigRequest,
    actor: Annotated[ActorRef, Depends(get_current_actor)],
    _: Annotated[ActorContext, Depends(require_admin())],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> TenantConfigResponse:
    """Validate and store the tenant's whole configuration; 422 listing every
    §5.3 template violation or every milestone-block violation when a block is
    invalid, nothing written either way."""
    try:
        view = await tenant_config.put_tenant_config(
            session,
            tenant_id=actor.tenant_id,
            actor_user_id=actor.user_id,
            document=payload.naming_templates.document,
            generated=payload.naming_templates.generated,
            milestones=payload.milestones.model_dump(),
            send_modes=(
                None if payload.send_modes is None else payload.send_modes.model_dump()
            ),
        )
    except TemplateValidationError as exc:
        raise HTTPException(status_code=422, detail=list(exc.errors)) from None
    except MilestoneValidationError as exc:
        raise HTTPException(status_code=422, detail=list(exc.errors)) from None
    except SendModesValidationError as exc:
        raise HTTPException(status_code=422, detail=list(exc.errors)) from None
    return _response(view)
