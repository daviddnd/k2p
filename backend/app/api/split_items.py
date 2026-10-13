"""The split-items review surface (workstream ③, ADR-0019 §3; ruling D8).

The staff read over the multi-company split's records: every route is
Administrator-only (``require_admin``), tenant-scoped, and **coordinates
only** — the attribution-cases custody bar verbatim. The list names each
parent (the sender, the receipt time, the status, the envelope's provider
message id) and its work items (the company, the item's status, the stored
slice's document id), never a subject and never a body: the parent's raw and
the items' slices are mail, read through the records registry's own
company-grant routes where a company applies, not here. The parent's own row
names no company — the involved set appears only as the items' company
coordinates.

The one filter is the parent's own status; the vocabulary is the record's
closed set, and an unknown value is a 422, never an empty list.
"""

from datetime import datetime
from enum import StrEnum
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.persistence.models import SplitParent, SplitWorkItem
from app.adapters.persistence.session import get_session
from app.api.deps import ActorRef, get_current_actor, require_admin
from app.application import split
from app.domain.rbac import ActorContext

router = APIRouter(prefix="/api/v1/mail/split-items", tags=["mail"])


class SplitItemsListFilter(StrEnum):
    """The list's ``status`` filter: the parent's own closed vocabulary.

    ``all`` is the absence of the filter — this surface is a review read, not
    a work queue, so its default shows every parent, the settled beside the
    parked.
    """

    SPLIT_PENDING = "split_pending"
    SPLIT_DONE = "split_done"
    STAFF_ONLY = "staff_only"
    FAILED = "failed"
    ALL = "all"


class SplitWorkItemEntryResponse(BaseModel):
    """One work item as staff read it — coordinates only.

    ``company_id`` is the item's company (the involved set appears only
    here); ``slice_document_id`` follows into the records registry's
    company-grant read, which is where the slice itself is ever served.
    """

    id: UUID
    company_id: UUID
    status: str
    slice_document_id: UUID
    created_at: datetime


class SplitParentEntryResponse(BaseModel):
    """One split parent as staff read it — coordinates only.

    ``status`` is the parent's own vocabulary (``split_pending |
    split_done | staff_only | failed``); ``reason`` is not stored on the row
    (the park and failure codes live in the audit entry the status wrote) —
    this surface names what exists. Deliberately no subject: a subject is
    the client's own words, and the custody bar admits none here.
    """

    id: UUID
    mailbox_message_id: str
    sender_address: str
    received_at: datetime
    status: str
    items: list[SplitWorkItemEntryResponse]
    created_at: datetime


def _entry(parent: SplitParent, items: list[SplitWorkItem]) -> SplitParentEntryResponse:
    return SplitParentEntryResponse(
        id=parent.id,
        mailbox_message_id=parent.mailbox_message_id,
        sender_address=parent.sender_address,
        received_at=parent.received_at,
        status=parent.status,
        items=[
            SplitWorkItemEntryResponse(
                id=item.id,
                company_id=item.company_id,
                status=item.status,
                slice_document_id=item.slice_document_id,
                created_at=item.created_at,
            )
            for item in items
        ],
        created_at=parent.created_at,
    )


@router.get("")
async def list_split_items(
    actor: Annotated[ActorRef, Depends(get_current_actor)],
    _: Annotated[ActorContext, Depends(require_admin())],
    session: Annotated[AsyncSession, Depends(get_session)],
    status: Annotated[SplitItemsListFilter, Query()] = SplitItemsListFilter.ALL,
) -> list[SplitParentEntryResponse]:
    """The tenant's split parents, newest first, each with its work items.

    Metadata and coordinates only; Administrator-only, the quarantine and
    attribution lists' posture: the records are opened by the cycle against
    the tenant-wide mailbox, so reading them is the same seat's work.
    """
    parents = await split.list_split_parents(
        session,
        tenant_id=actor.tenant_id,
        status=(
            None
            if status is SplitItemsListFilter.ALL
            else split.SplitParentStatus(status.value)
        ),
    )
    return [
        _entry(
            parent,
            await split.items_for_parent(
                session, tenant_id=actor.tenant_id, parent_id=parent.id
            ),
        )
        for parent in parents
    ]


@router.get("/{parent_id}")
async def get_split_item(
    parent_id: UUID,
    actor: Annotated[ActorRef, Depends(get_current_actor)],
    _: Annotated[ActorContext, Depends(require_admin())],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> SplitParentEntryResponse:
    """One split parent and its work items — 404 for an unknown id **and**
    for another tenant's (deliberately one answer)."""
    parent = await split.get_split_parent(
        session, tenant_id=actor.tenant_id, parent_id=parent_id
    )
    if parent is None:
        raise HTTPException(status_code=404, detail="split parent not found")
    return _entry(
        parent,
        await split.items_for_parent(
            session, tenant_id=actor.tenant_id, parent_id=parent.id
        ),
    )
