"""DB-backed change-event scenarios (opt-in; needs a migrated PostgreSQL).

CE1's record end to end against a real database: manual entry, the §4.1
lifecycle over the real routes with the real RBAC loader, the version-row
mechanics (a status moves in place; an edit appends a version), the tenancy
keys, the audit chain that carries every mutation, and the refusals — the
unwired actions, the reserved type, a rejected payload, the linkage rules and
the lost version race. M5b's task 5 adds the wired reconfirmation exchange:
``send_reconfirmation`` composes-stores-releases the deterministic letter and
moves the event (automatic and held client modes, and the no-thread refusal),
and ``client_reply`` arrives as the system row no staff account can press.

The two claims that need a database rather than a fake are here:

- **scope §2.8's one-session rule** (requirement 9): a General account holding
  both event permissions and the company's access grant walks
  ``pending → under_review → reviewed → confirmed`` in one session, through
  the real ``load_actor_context`` — no context override, so what is proven is
  the product's own authorization path and not the test's.
- **the chain**: every entry and every transition appends one
  ``change_event_*`` entry with the from→to statuses and the resulting
  version, and never the payload's content.

The reconfirmation seeds below pin bilingual copy verbatim — the
Traditional-Chinese strings carry fullwidth CJK punctuation, which ruff's
RUF001 reads as ambiguous look-alikes. The rule guards code, not a letter a
client reads (the test_reconfirmation_scenarios_db.py precedent).

Skipped unless ``AICS_TEST_DATABASE_URL`` points at a database migrated to
head. Every test runs inside a transaction that is rolled back, so the review
database stays clean.
"""
# ruff: noqa: RUF001

import os
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta
from email import message_from_bytes
from email.policy import default as default_policy
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event as sqlalchemy_event
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.adapters.persistence.audit import AuditLog
from app.adapters.persistence.change_events import ChangeEvent
from app.adapters.persistence.communications import Communication
from app.adapters.persistence.company_master import CompanyNameRecord
from app.adapters.persistence.confirmation import ConfirmationRequest
from app.adapters.persistence.documents import Document
from app.adapters.persistence.mail_outbox import MailOutbox
from app.adapters.persistence.models import Company, CompanyAccessGrant, Tenant, User
from app.adapters.persistence.prior_filing import ExtractionAssessment
from app.adapters.persistence.session import get_session
from app.adapters.persistence.tenant_config import TenantConfig
from app.api.deps import ActorRef, get_current_actor
from app.api.documents import get_storage
from app.application import change_events, outbox
from app.core.config import settings
from app.domain.audit import AuditAction
from app.domain.change_events import (
    ChangeEventSourceType,
    ChangeEventStatus,
    ChangeEventType,
)
from app.domain.compliance import change_window_deadline
from app.domain.mail_send import HELD_NOT_BEFORE, OutboxKind, OutboxStatus
from app.domain.milestones import default_milestones
from app.domain.naming_templates import DEFAULT_DOCUMENT_TEMPLATE, DEFAULT_GENERATED_TEMPLATE
from app.main import create_app

DATABASE_URL = os.environ.get("AICS_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    DATABASE_URL is None, reason="set AICS_TEST_DATABASE_URL to a migrated PostgreSQL"
)

engine = create_async_engine(DATABASE_URL or "postgresql+psycopg://localhost/unused")

COMPANY = {
    "br_number": "12345678",
    "cr_number": "1234567",
    "date_of_incorporation": "2010-03-01",
    "anniversary_date": "2027-03-01",
    "type": "private",
}

APPOINTMENT_PAYLOAD = {
    "appointment": {
        "officer": {
            "role": "director",
            "name_zh": "陳大文",
            "name_en": "CHAN Tai Man",
            "hkid_partial": "A1234",
            "email": "chan@brightlantern.example.hk",
            "appointment_date": "2026-09-01",
            "existing_director_at_appointment": False,
        }
    }
}

ENTRY_BODY = {
    "type": "director_appointment",
    "effective_date": "2026-09-01",
    "effective_date_text": "appointed on 1 September 2026",
    "informed_date": "2026-09-02",
    "payload": APPOINTMENT_PAYLOAD,
}


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    """A session inside an outer transaction, rolled back after each test.

    ``create_savepoint`` matches the deployment's session dependency, which
    commits at the request edge: a savepoint makes "a commit released this"
    the same statement here as in a deployment.
    """
    async with engine.connect() as connection:
        await connection.begin()
        async with AsyncSession(
            bind=connection, expire_on_commit=False, join_transaction_mode="create_savepoint"
        ) as session:
            yield session
        await connection.rollback()


def make_client(
    session: AsyncSession, actor: ActorRef, store: "FakeStore | None" = None
) -> TestClient:
    """The real app over the test session, with the real RBAC loader.

    No ``get_actor_context`` override: the actor's role, permission flags and
    company grants are read from the database by the product's own path, which
    is what makes the one-session rule a real assertion. The storage seam is
    overridden with an in-memory store (the send_reconfirmation transition
    stores its letter through it; other routes never touch it).
    """
    app: FastAPI = create_app()
    app.dependency_overrides[get_current_actor] = lambda: actor
    app.dependency_overrides[get_storage] = lambda: store if store is not None else FakeStore()

    async def override_session() -> AsyncIterator[AsyncSession]:
        yield session

    app.dependency_overrides[get_session] = override_session
    return TestClient(app)


class FakeStore:
    """In-memory ``DocumentStore``: keys → bytes, deterministic URLs."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    async def put(self, key: str, data: bytes, content_type: str) -> None:
        self.objects[key] = data

    async def get_bytes(self, key: str) -> bytes:
        return self.objects[key]

    async def presign_get(self, key: str, *, expires_in: int) -> str:
        return f"https://storage.invalid/{key}?ttl={expires_in}"


@pytest.fixture
def store() -> FakeStore:
    return FakeStore()


async def seed_tenant(session: AsyncSession) -> tuple[Tenant, User]:
    """A tenant with a second, granted General account and one company.

    The Administrator is the actor every test uses unless it is proving the
    one-session rule; the General account is the one holding the two event
    permissions (``review_events``, ``approve_events``) and the company's
    access grant.
    """
    tenant = Tenant(name=f"tenant-{uuid4()}")
    session.add(tenant)
    await session.flush()
    admin = User(
        tenant_id=tenant.id,
        email=f"{uuid4()}@example.com",
        display_name="Admin",
        role="administrator",
    )
    general = User(
        tenant_id=tenant.id,
        email=f"{uuid4()}@example.com",
        display_name="General",
        role="general",
        review_events=True,
        approve_events=True,
    )
    session.add_all([admin, general])
    await session.flush()
    return tenant, admin


def company_body(br_number: str = COMPANY["br_number"]) -> dict[str, str]:
    return {**COMPANY, "br_number": br_number}


async def deployment(
    session: AsyncSession, *, grant_general: bool = True
) -> tuple[TestClient, Tenant, User, User, str]:
    """A tenant, its two accounts, one company, and a client as the Administrator.

    ``grant_general`` gives the General account the company's access grant, so
    the one-session test can drop the Administrator entirely; the other tests
    leave it on and act as the Administrator.
    """
    tenant, admin = await seed_tenant(session)
    client = make_client(session, ActorRef(user_id=admin.id, tenant_id=tenant.id))
    response = client.post("/api/v1/companies", json=company_body())
    assert response.status_code == 201, response.text
    company_id = str(response.json()["id"])
    general = (
        await session.scalars(
            select(User).where(User.tenant_id == tenant.id, User.role == "general")
        )
    ).one()
    if grant_general:
        session.add(
            CompanyAccessGrant(user_id=general.id, company_id=UUID(company_id), tenant_id=tenant.id)
        )
        await session.flush()
    return client, tenant, admin, general, company_id


def enter_event(client: TestClient, company_id: str, **overrides: object) -> dict:
    """Enter one event by hand and return it, failing loudly on a refusal."""
    body = {**ENTRY_BODY, **overrides}
    response = client.post(f"/api/v1/companies/{company_id}/change-events", json=body)
    assert response.status_code == 201, response.text
    return response.json()


def action(client: TestClient, event_id: str, name: str, **body: object) -> object:
    return client.post(
        f"/api/v1/change-events/{event_id}/transitions", json={"action": name, **body}
    )


async def stored_versions(session: AsyncSession, event_id: str) -> list[ChangeEvent]:
    rows = await session.scalars(
        select(ChangeEvent)
        .where(ChangeEvent.id == UUID(event_id))
        .order_by(ChangeEvent.version_no)
    )
    return list(rows.all())


async def audit_entries(session: AsyncSession, tenant_id: UUID) -> list[AuditLog]:
    return list(
        (
            await session.scalars(
                select(AuditLog).where(AuditLog.tenant_id == tenant_id).order_by(AuditLog.seq)
            )
        ).all()
    )


async def change_event_entries(session: AsyncSession, tenant_id: UUID) -> list[AuditLog]:
    return [
        entry
        for entry in await audit_entries(session, tenant_id)
        if entry.action
        in (
            AuditAction.CHANGE_EVENT_RECORDED.value,
            AuditAction.CHANGE_EVENT_TRANSITION_RECORDED.value,
        )
    ]


# --- Manual entry (§2.7) ----------------------------------------------------


async def test_manual_entry_lands_pending_version_one_and_audits_it(
    session: AsyncSession,
) -> None:
    """Scope §2.7's fallback: staff type in what a client reported, the event
    enters ``pending`` at version 1 as ``manual`` with no source record, and
    the chain carries the coordinates (never the payload)."""
    client, tenant, admin, _, company_id = await deployment(session)

    event = enter_event(client, company_id)

    assert event["status"] == ChangeEventStatus.PENDING.value
    assert event["version_no"] == 1
    assert event["source_type"] == "manual"
    assert event["source_id"] is None
    assert event["created_by"] == str(admin.id)
    assert event["history"] == []
    assert event["payload"] == APPOINTMENT_PAYLOAD
    entries = await change_event_entries(session, tenant.id)
    assert len(entries) == 1
    assert entries[0].action == AuditAction.CHANGE_EVENT_RECORDED.value
    assert entries[0].details == {
        "type": "director_appointment",
        "source_type": "manual",
        "version_no": 1,
        "before": None,
        "after": "pending",
    }
    assert entries[0].actor_user_id == admin.id
    assert entries[0].entity_id == UUID(event["id"])
    # The payload's home is the version row: the chain carries no content.
    assert "陳大文" not in str(entries[0].details)


async def test_the_reserved_type_is_refused_and_nothing_is_stored(
    session: AsyncSession,
) -> None:
    """§2.6: ``company_name_change`` waits for Wave 2/NNC2 — refused, never
    stored, and the refusal names the type."""
    client, tenant, _, _, company_id = await deployment(session)

    response = client.post(
        f"/api/v1/companies/{company_id}/change-events",
        json={**ENTRY_BODY, "type": "company_name_change"},
    )

    assert response.status_code == 422, response.text
    assert response.json()["detail"] == "reserved_event_type"
    assert await change_event_entries(session, tenant.id) == []


@pytest.mark.parametrize(
    "payload",
    [
        # The derived count is refused by name (contract §3).
        {
            "appointment": {
                "officer": {
                    "role": "director",
                    "name_en": "CHAN Tai Man",
                    "appointment_date": "2026-09-01",
                    "continuation_sheet_count": 2,
                }
            }
        },
        # A role that does not belong to the type.
        {
            "appointment": {
                "officer": {
                    "role": "company_secretary",
                    "name_en": "CHAN Tai Man",
                    "appointment_date": "2026-09-01",
                }
            }
        },
        # An address the four-box rule refuses.
        {
            "appointment": {
                "officer": {
                    "role": "director",
                    "name_en": "CHAN Tai Man",
                    "appointment_date": "2026-09-01",
                    "correspondence_address": {"flat": "12A"},
                }
            }
        },
        # A genuinely unknown key stays a violation (the key set is closed, and
        # recognizing the capacity group did not open it).
        {
            "appointment": {
                "officer": {
                    "role": "director",
                    "name_en": "CHAN Tai Man",
                    "appointment_date": "2026-09-01",
                    "alternate": "WONG Ka Ming",
                }
            }
        },
        # NAR1's word for the office is not a capacity box ND2A prints.
        {
            "appointment": {
                "officer": {
                    "role": "director",
                    "name_en": "CHAN Tai Man",
                    "appointment_date": "2026-09-01",
                    "capacity": ["reserve_director"],
                }
            }
        },
        # The alternate box without the name it substitutes for.
        {
            "appointment": {
                "officer": {
                    "role": "director",
                    "name_en": "CHAN Tai Man",
                    "appointment_date": "2026-09-01",
                    "capacity": ["alternate_director"],
                }
            }
        },
    ],
)
async def test_a_payload_the_grammar_refuses_is_a_422_with_its_violations(
    session: AsyncSession, payload: dict
) -> None:
    client, tenant, _, _, company_id = await deployment(session)

    response = client.post(
        f"/api/v1/companies/{company_id}/change-events",
        json={**ENTRY_BODY, "payload": payload},
    )

    assert response.status_code == 422, response.text
    assert response.json()["detail"].startswith("change_event_invalid: ")
    assert await change_event_entries(session, tenant.id) == []


async def test_the_informed_date_rule_holds_over_the_route(session: AsyncSession) -> None:
    """§2.6's sanity rule, on the wire: the TCSP cannot have learned of a
    change before it took effect."""
    client, _, _, _, company_id = await deployment(session)
    response = client.post(
        f"/api/v1/companies/{company_id}/change-events",
        json={**ENTRY_BODY, "informed_date": "2026-08-31"},
    )

    assert response.status_code == 422, response.text
    assert "informed_date" in response.json()["detail"]


# --- The 15-day window over real events (DoD §9 item 23, CE2) ---------------


async def test_the_event_reads_carry_the_computed_15_day_window(
    session: AsyncSession,
) -> None:
    """Item 23's second half: the statutory countdown runs over a real event.
    The clock starts on ``effective_date`` — ``informed_date`` is when the TCSP
    heard, never the window's input — and the bands are the calendar's own:
    amber inside 15 days, red inside 7 or past the deadline."""
    today = date.today()
    client, _, _, _, company_id = await deployment(session)

    late = enter_event(
        client,
        company_id,
        effective_date=(today - timedelta(days=20)).isoformat(),
        informed_date=(today - timedelta(days=19)).isoformat(),
    )
    approaching = enter_event(
        client,
        company_id,
        effective_date=(today - timedelta(days=5)).isoformat(),
        informed_date=(today - timedelta(days=1)).isoformat(),
    )
    future = enter_event(
        client,
        company_id,
        effective_date=(today + timedelta(days=30)).isoformat(),
        informed_date=None,
    )

    # The write's echo carries the same window the read computes.
    assert late["change_window"] == {
        "deadline": change_window_deadline(today - timedelta(days=20)).isoformat(),
        "days_remaining": -5,
        "is_late": True,
        "risk": "red",
        "alert": "alert_7d",
    }
    assert approaching["change_window"] == {
        "deadline": change_window_deadline(today - timedelta(days=5)).isoformat(),
        "days_remaining": 10,
        "is_late": False,
        "risk": "amber",
        "alert": "alert_15d",
    }
    assert future["change_window"] == {
        "deadline": change_window_deadline(today + timedelta(days=30)).isoformat(),
        "days_remaining": 45,
        "is_late": False,
        "risk": "green",
        "alert": None,
    }

    listed = client.get(f"/api/v1/companies/{company_id}/change-events")
    assert listed.status_code == 200, listed.text
    by_id = {event["id"]: event for event in listed.json()}
    assert by_id[late["id"]]["change_window"] == late["change_window"]
    assert by_id[approaching["id"]]["change_window"] == approaching["change_window"]
    assert by_id[future["id"]]["change_window"] == future["change_window"]


# --- The cross-company review queue (DoD §9 item 10, CE2) -------------------

QUEUE_URL = "/api/v1/change-events"


async def test_the_review_queue_lists_awaiting_review_events_soonest_deadline_first(
    session: AsyncSession,
) -> None:
    """DoD §9 item 10's queue: every event whose current version awaits staff
    review — ``pending`` or ``under_review`` — across the companies the actor
    may see, the soonest statutory deadline first, each item carrying its
    company and its computed window. A ``reviewed`` event is the approval
    gate's (scope §2.3's separate widget), and it is not in the queue."""
    client, _, _, _, company_id = await deployment(session)
    named = client.patch(
        f"/api/v1/companies/{company_id}",
        json={"effective_date": "2019-01-01", "name": {"name_en": "BRIGHT LANTERN LIMITED"}},
    )
    assert named.status_code == 200, named.text
    today = date.today()

    pending = enter_event(
        client,
        company_id,
        effective_date=(today - timedelta(days=3)).isoformat(),
        informed_date=(today - timedelta(days=2)).isoformat(),
    )
    under_review = enter_event(
        client,
        company_id,
        effective_date=(today - timedelta(days=10)).isoformat(),
        informed_date=(today - timedelta(days=9)).isoformat(),
    )
    assert action(client, under_review["id"], "mark_under_review").status_code == 200
    reviewed = enter_event(
        client,
        company_id,
        effective_date=(today - timedelta(days=8)).isoformat(),
        informed_date=(today - timedelta(days=7)).isoformat(),
    )
    assert action(client, reviewed["id"], "mark_under_review").status_code == 200
    assert action(client, reviewed["id"], "mark_reviewed").status_code == 200

    response = client.get(QUEUE_URL)
    assert response.status_code == 200, response.text
    items = response.json()
    assert [item["id"] for item in items] == [under_review["id"], pending["id"]]
    assert [item["status"] for item in items] == ["under_review", "pending"]
    first = items[0]
    assert first["company_id"] == company_id
    assert first["br_number"] == COMPANY["br_number"]
    assert first["company_name"] == "BRIGHT LANTERN LIMITED"
    assert first["version_no"] == 1
    assert first["change_window"] == {
        "deadline": change_window_deadline(today - timedelta(days=10)).isoformat(),
        "days_remaining": 5,
        "is_late": False,
        "risk": "red",
        "alert": "alert_7d",
    }
    # The queue lists events, not actions: the transitions read stays the sole
    # allowed-actions authority.
    assert "actions" not in first
    assert "history" not in first
    assert reviewed["id"] not in {item["id"] for item in items}


async def test_the_review_queue_is_grant_scoped_like_the_dashboard(
    session: AsyncSession,
) -> None:
    """An Administrator sees the tenant's awaiting-review events; a General
    account exactly the companies its access grants cover — the dashboard's
    own rule, applied to the queue."""
    client, tenant, _, general, granted_company_id = await deployment(session)
    other = client.post("/api/v1/companies", json=company_body("87654321"))
    assert other.status_code == 201, other.text
    other_company_id = other.json()["id"]
    today = date.today()

    mine = enter_event(
        client,
        granted_company_id,
        effective_date=(today - timedelta(days=1)).isoformat(),
        informed_date=(today - timedelta(days=1)).isoformat(),
    )
    theirs = enter_event(
        client,
        other_company_id,
        effective_date=(today - timedelta(days=7)).isoformat(),
        informed_date=(today - timedelta(days=6)).isoformat(),
    )

    assert [item["id"] for item in client.get(QUEUE_URL).json()] == [
        theirs["id"],
        mine["id"],
    ]
    general_client = make_client(session, ActorRef(user_id=general.id, tenant_id=tenant.id))
    assert [item["id"] for item in general_client.get(QUEUE_URL).json()] == [mine["id"]]


async def test_the_queue_tracks_the_current_version_and_drops_decided_events(
    session: AsyncSession,
) -> None:
    """A revision appends a version and the earlier row keeps its historical
    status, so a queue that filtered rows rather than current versions would
    list the same event twice and keep listing a decided one. The queue must
    show the event once, at its current version, and drop it once review
    ends."""
    client, _, _, _, company_id = await deployment(session)
    today = date.today()
    event = enter_event(
        client,
        company_id,
        effective_date=(today - timedelta(days=4)).isoformat(),
        informed_date=(today - timedelta(days=3)).isoformat(),
    )

    edited = action(client, event["id"], "edit", effective_date_text="the letter said 3 September")
    assert edited.status_code == 200, edited.text
    assert edited.json()["version_no"] == 2

    items = client.get(QUEUE_URL).json()
    assert [item["id"] for item in items] == [event["id"]]
    assert items[0]["version_no"] == 2
    rows = await stored_versions(session, event["id"])
    assert [(row.version_no, row.status) for row in rows] == [(1, "pending"), (2, "pending")]

    assert action(client, event["id"], "mark_under_review").status_code == 200
    assert action(client, event["id"], "mark_reviewed").status_code == 200
    assert action(client, event["id"], "confirm").status_code == 200
    assert client.get(QUEUE_URL).json() == []
    rows = await stored_versions(session, event["id"])
    assert [(row.version_no, row.status) for row in rows] == [(1, "pending"), (2, "confirmed")]


# --- The capacity group: ND2A's alternate director (final review, finding 1) --


async def test_an_alternate_director_appointment_enters_and_stores_the_capacity_group(
    session: AsyncSession,
) -> None:
    """ND2A's alternate director, over the manual path.

    The registry sources the form's capacity checkbox 3
    (``alternate_director`` 候補董事) and its ``代替 Alternate to`` box from
    ``change_event.payload.appointment.officer.capacity[]`` /
    ``.alternate_to``, so an alternate directorship is enterable and the payload
    round-trips verbatim — before this, the grammar answered the same payload
    with an unknown-key violation. `role` stays §2.4's ``director``: ND2A's
    ``alternate_director`` is a capacity, and NAR1's ``reserve_director`` is not
    a box this form prints."""
    client, tenant, _, _, company_id = await deployment(session)
    payload = {
        "appointment": {
            "officer": {
                "role": "director",
                "capacity": ["director", "alternate_director"],
                "alternate_to": "WONG Ka Ming",
                "name_en": "CHAN Tai Man",
                "appointment_date": "2026-09-01",
            }
        }
    }

    event = enter_event(client, company_id, payload=payload)

    assert event["payload"] == payload
    assert event["status"] == "pending"
    assert (await stored_versions(session, event["id"]))[0].payload == payload
    assert len(await change_event_entries(session, tenant.id)) == 1


async def test_a_cessation_carries_the_capacity_group_too(session: AsyncSession) -> None:
    """The cessation block prints the same three boxes and the registry sources
    its ``alternate_to`` from the cessation shape: an alternate director ceasing
    is an ordinary entry, not a shape the grammar only tolerates on appointment."""
    client, _, _, _, company_id = await deployment(session)
    payload = {
        "cessation": {
            "officer": {
                "role": "director",
                "capacity": ["alternate_director"],
                "alternate_to": "WONG Ka Ming",
                "name_en": "CHAN Tai Man",
                "cessation_reason": "resignation_others",
                "cessation_date": "2026-09-10",
            }
        }
    }

    event = enter_event(
        client, company_id, type="director_cessation", payload=payload
    )

    assert event["payload"] == payload


# --- The §1 protected class is refused, not stored (ruling 2026-09-25) ------


async def test_a_protected_class_entry_is_refused_and_nothing_is_stored(
    session: AsyncSession,
) -> None:
    """§1's protected class (full HKID/passport, usual residential address) is
    refused by name until field-level encryption exists (§8 scope, Phase 7):
    the answer names the fields to drop, and **no row and no chain entry
    exists** afterwards — the refusal is not a write with a bad status."""
    client, tenant, _, _, company_id = await deployment(session)
    payload = {
        "appointment": {
            "officer": {
                "role": "director",
                "name_en": "CHAN Tai Man",
                "appointment_date": "2026-09-01",
                "hkid_full": "A123456",
                "hkid_check_digit": "7",
            }
        }
    }

    response = client.post(
        f"/api/v1/companies/{company_id}/change-events",
        json={**ENTRY_BODY, "payload": payload},
    )

    assert response.status_code == 422, response.text
    assert response.json()["detail"] == (
        "protected_class_unavailable: payload.appointment.officer.hkid_full, "
        "payload.appointment.officer.hkid_check_digit"
    )
    assert list((await session.scalars(select(ChangeEvent))).all()) == []
    assert await change_event_entries(session, tenant.id) == []


async def test_an_edit_cannot_add_a_protected_class_field(session: AsyncSession) -> None:
    """The refusal holds for a revision too: an edit that would introduce the
    class leaves the event exactly as it was — no new version, no entry."""
    client, tenant, _, _, company_id = await deployment(session)
    event = enter_event(client, company_id)
    payload = {
        "appointment": {
            "officer": {
                "role": "director",
                "name_en": "CHAN Tai Man",
                "appointment_date": "2026-09-01",
                "usual_residential_address": {
                    "flat": "9B",
                    "building": "Home Court",
                    "street": "3 Garden Road",
                    "district": "Central",
                },
            }
        }
    }

    response = action(client, event["id"], "edit", payload=payload)

    assert response.status_code == 422, response.text
    assert response.json()["detail"] == (
        "protected_class_unavailable: "
        "payload.appointment.officer.usual_residential_address"
    )
    assert len(await stored_versions(session, event["id"])) == 1
    assert len(await change_event_entries(session, tenant.id)) == 1


async def test_the_partial_identification_fields_still_enter(session: AsyncSession) -> None:
    """The public-page values the ND2A's public pages carry are unaffected: a
    real event stays fully enterable without the protected class."""
    client, _, _, _, company_id = await deployment(session)
    payload = {
        "appointment": {
            "officer": {
                "role": "director",
                "name_en": "CHAN Tai Man",
                "appointment_date": "2026-09-01",
                "hkid_partial": "A1234",
                "passport_partial": "K123456",
                "passport_issuing_country_region": "HKSAR",
            }
        }
    }

    event = enter_event(client, company_id, payload=payload)

    assert event["payload"] == payload
    assert event["status"] == "pending"


# --- The lifecycle (scope §2.8, requirement 9) ------------------------------


async def test_a_general_account_with_both_permissions_walks_the_lifecycle(
    session: AsyncSession,
) -> None:
    """Scope §2.8's rule, through the product's own authorization path.

    A General account holding ``review_events`` and ``approve_events`` and the
    company's access grant moves one event ``pending → under_review →
    reviewed → confirmed`` in one session — no context override, so the flags
    and the grant are read from the database by ``load_actor_context``. Each
    step appends its entry with the statuses it moved between, the reviewer
    and approver stamps land on the row, and every step is an update of the
    same version: status changes are not revisions.
    """
    _, tenant, _, general, company_id = await deployment(session)
    client = make_client(session, ActorRef(user_id=general.id, tenant_id=tenant.id))
    event = enter_event(client, company_id)
    event_id = event["id"]

    under_review = action(client, event_id, "mark_under_review")
    reviewed = action(client, event_id, "mark_reviewed")
    confirmed = action(client, event_id, "confirm")

    for response, status in (
        (under_review, "under_review"),
        (reviewed, "reviewed"),
        (confirmed, "confirmed"),
    ):
        assert response.status_code == 200, response.text
        assert response.json()["status"] == status
        assert response.json()["version_no"] == 1
    body = confirmed.json()
    assert body["reviewed_by"] == str(general.id)
    assert body["approved_by"] == str(general.id)
    assert body["reviewed_at"] is not None and body["approved_at"] is not None
    assert body["history"] == []  # a status move writes no new version
    assert len(await stored_versions(session, event_id)) == 1
    entries = await change_event_entries(session, tenant.id)
    assert [(entry.details["before"], entry.details["after"]) for entry in entries] == [
        (None, "pending"),
        ("pending", "under_review"),
        ("under_review", "reviewed"),
        ("reviewed", "confirmed"),
    ]
    assert [entry.details["version_no"] for entry in entries] == [1, 1, 1, 1]
    assert [entry.actor_user_id for entry in entries] == [general.id] * 4


async def test_a_reviewer_cannot_confirm_and_an_approver_cannot_review(
    session: AsyncSession,
) -> None:
    """The two legs are real: the same event refuses a reviewer the approver's
    action and an approver the reviewer's — 403, and nothing written."""
    _, tenant, admin, general, company_id = await deployment(session)
    admin_client = make_client(session, ActorRef(admin.id, tenant.id))
    event = enter_event(admin_client, company_id)
    assert action(admin_client, event["id"], "mark_under_review").status_code == 200
    assert action(admin_client, event["id"], "mark_reviewed").status_code == 200
    before = len(await change_event_entries(session, tenant.id))

    # The General account holds both flags here, so narrow it to one flag each
    # way round — the flags are the product's, read from the row.
    general_row = await session.get(User, general.id)
    assert general_row is not None
    general_row.approve_events = False
    await session.flush()
    reviewer = make_client(session, ActorRef(general.id, tenant.id))
    refused_confirm = action(reviewer, event["id"], "confirm")
    assert refused_confirm.status_code == 403, refused_confirm.text

    general_row.approve_events = True
    general_row.review_events = False
    await session.flush()
    approver = make_client(session, ActorRef(general.id, tenant.id))
    # `revert` is the review leg's action at `reviewed`, so an approver-only
    # account is refused it for authority, not for the status.
    refused_review = action(approver, event["id"], "revert")
    assert refused_review.status_code == 403, refused_review.text

    assert len(await change_event_entries(session, tenant.id)) == before
    assert (await stored_versions(session, event["id"]))[0].status == "reviewed"


async def test_an_event_cannot_walk_backwards_out_of_its_lifecycle(
    session: AsyncSession,
) -> None:
    """The guard's edge direction, over the routes: a ``pending`` event does
    not admit ``confirm``, and a ``confirmed`` one does not admit
    ``mark_under_review`` — the named 409, and the row stays where it was."""
    client, tenant, _, _, company_id = await deployment(session)
    event = enter_event(client, company_id)
    event_id = event["id"]

    early = action(client, event_id, "confirm")
    assert early.status_code == 409, early.text
    assert early.json()["detail"] == "transition_not_allowed"

    for name in ("mark_under_review", "mark_reviewed", "confirm"):
        assert action(client, event_id, name).status_code == 200
    late = action(client, event_id, "mark_under_review")

    assert late.status_code == 409, late.text
    assert late.json()["detail"] == "transition_not_allowed"
    assert (await stored_versions(session, event_id))[0].status == "confirmed"
    # One entry per accepted action; a refused one writes nothing.
    assert len(await change_event_entries(session, tenant.id)) == 4


# --- Revisions (§2.6's version rows) ----------------------------------------


async def test_an_edit_appends_the_next_version_and_keeps_the_lineage(
    session: AsyncSession,
) -> None:
    """An edit is a new version: same event id, ``version_no + 1``, the status
    unchanged, the earlier version intact in the history, and the chain
    carrying which version the revision became."""
    client, tenant, _, _, company_id = await deployment(session)
    event = enter_event(client, company_id)
    event_id = event["id"]
    assert action(client, event_id, "mark_under_review").status_code == 200

    edited_payload = {
        "appointment": {
            "officer": {
                "role": "director",
                "name_zh": "陳大文",
                "appointment_date": "2026-09-15",
            }
        }
    }
    response = action(
        client,
        event_id,
        "edit",
        payload=edited_payload,
        effective_date="2026-09-15",
        informed_date="2026-09-16",
        rationale="the letter said the 15th",
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["id"] == event_id  # the event's identity survives the revision
    assert body["version_no"] == 2
    assert body["status"] == "under_review"
    assert body["payload"] == edited_payload
    assert body["effective_date"] == "2026-09-15"
    assert body["informed_date"] == "2026-09-16"
    assert [version["version_no"] for version in body["history"]] == [1]
    assert body["history"][0]["payload"] == APPOINTMENT_PAYLOAD
    versions = await stored_versions(session, event_id)
    assert [(row.version_no, row.status) for row in versions] == [
        (1, "under_review"),
        (2, "under_review"),
    ]
    entries = await change_event_entries(session, tenant.id)
    assert entries[-1].details == {
        "action": "edit",
        "before": "under_review",
        "after": "under_review",
        "version_no": 2,
        "rationale": "the letter said the 15th",
    }


async def test_the_company_read_returns_current_versions_and_histories(
    session: AsyncSession,
) -> None:
    client, _, _, _, company_id = await deployment(session)
    first = enter_event(client, company_id)
    second = enter_event(client, company_id, type="other", payload={"summary": "auditor changed"})
    assert action(client, first["id"], "edit", informed_date="2026-09-20").status_code == 200

    response = client.get(f"/api/v1/companies/{company_id}/change-events")

    assert response.status_code == 200, response.text
    by_id = {event["id"]: event for event in response.json()}
    assert set(by_id) == {first["id"], second["id"]}
    assert by_id[first["id"]]["version_no"] == 2
    assert [version["version_no"] for version in by_id[first["id"]]["history"]] == [1]
    assert by_id[second["id"]]["version_no"] == 1
    assert by_id[second["id"]]["history"] == []
    # The current version is the highest, and the edit did not change the type.
    assert by_id[first["id"]]["type"] == "director_appointment"


async def test_an_edit_that_changes_nothing_is_refused(session: AsyncSession) -> None:
    """A revision must revise: an edit body with no fields would append a
    version that says exactly what the last one says."""
    client, tenant, _, _, company_id = await deployment(session)
    event = enter_event(client, company_id)

    response = action(client, event["id"], "edit")

    assert response.status_code == 422, response.text
    assert response.json()["detail"] == "change_event_invalid: edit: nothing to change"
    assert len(await stored_versions(session, event["id"])) == 1
    assert len(await change_event_entries(session, tenant.id)) == 1


async def test_a_field_that_cannot_be_cleared_is_refused(session: AsyncSession) -> None:
    """``effective_date`` is the substance of an event: an explicit null is a
    violation, not a clear (the two date-text fields are the clearable ones)."""
    client, _, _, _, company_id = await deployment(session)
    event = enter_event(client, company_id)

    response = action(client, event["id"], "edit", effective_date=None)

    assert response.status_code == 422, response.text
    assert "effective_date: cannot be cleared" in response.json()["detail"]


async def test_a_lost_version_race_is_refused_rather_than_raised(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The one race the lock pair cannot rule out: a writer whose snapshot
    predates a committed revision would try to insert a version number already
    taken. The primary key refuses it and the executor answers
    ``change_event_conflict`` — a 409 the caller can retry, never a 500. The
    stale snapshot is simulated (the lock makes the real race unreachable)."""
    client, tenant, _, _, company_id = await deployment(session)
    event = enter_event(client, company_id)
    event_id = event["id"]
    stale = (await stored_versions(session, event_id))[0]

    async def stale_current(*args: object, **kwargs: object) -> ChangeEvent:
        return stale

    # A revision has landed since the (simulated) stale read was taken.
    assert action(client, event_id, "edit", informed_date="2026-09-20").status_code == 200
    before = len(await change_event_entries(session, tenant.id))
    monkeypatch.setattr(change_events, "_current_version", stale_current)

    response = action(client, event_id, "edit", informed_date="2026-09-21")

    assert response.status_code == 409, response.text
    assert response.json()["detail"] == "change_event_conflict"
    assert len(await stored_versions(session, event_id)) == 2
    assert len(await change_event_entries(session, tenant.id)) == before


async def test_every_writer_takes_the_events_lock(session: AsyncSession) -> None:
    """The lock is the whole concurrency story: the write path's read of the
    event's rows carries ``FOR UPDATE``, so two writers cannot both read the
    same current version."""
    client, _, _, _, company_id = await deployment(session)
    event = enter_event(client, company_id)
    statements: list[str] = []

    def capture(
        conn: object,
        cursor: object,
        statement: str,
        parameters: object,
        context: object,
        executemany: bool,
    ) -> None:
        statements.append(statement)

    sqlalchemy_event.listen(engine.sync_engine, "before_cursor_execute", capture)
    try:
        response = action(client, event["id"], "mark_under_review")
    finally:
        sqlalchemy_event.remove(engine.sync_engine, "before_cursor_execute", capture)

    assert response.status_code == 200, response.text
    locking = [
        statement
        for statement in statements
        if "change_events" in statement and "FOR UPDATE" in statement
    ]
    assert locking, statements


# --- The ends: delete and the staff-recorded withdrawal ----------------------


async def test_a_delete_is_soft_and_the_row_survives(session: AsyncSession) -> None:
    """§2.8: nothing is hard-deleted. The current version's status becomes
    ``deleted``, the row and its payload stay, and the read still lists it
    with its status visible."""
    client, tenant, _, _, company_id = await deployment(session)
    event = enter_event(client, company_id)

    response = action(client, event["id"], "delete")

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "deleted"
    rows = await stored_versions(session, event["id"])
    assert len(rows) == 1
    assert rows[0].status == "deleted"
    assert rows[0].payload == APPOINTMENT_PAYLOAD
    listed = client.get(f"/api/v1/companies/{company_id}/change-events").json()
    assert [(item["id"], item["status"]) for item in listed] == [(event["id"], "deleted")]
    # A deleted event is an end: no action is admitted from it.
    assert action(client, event["id"], "mark_under_review").status_code == 409
    assert len(await change_event_entries(session, tenant.id)) == 2


async def test_a_rejection_requires_a_rationale_and_closes_the_event(
    session: AsyncSession,
) -> None:
    """§4.1's ``reject``: ``approve_events``, a rationale that must arrive, and
    the ``deleted`` status it leaves — with the reason on the row and in the
    chain, so a reader finds why an event was thrown away."""
    client, tenant, _, _, company_id = await deployment(session)
    event = enter_event(client, company_id)
    assert action(client, event["id"], "mark_under_review").status_code == 200

    refused = action(client, event["id"], "reject")
    blank = action(client, event["id"], "reject", rationale="   ")

    assert refused.status_code == 422, refused.text
    assert refused.json()["detail"] == "rationale_required"
    assert blank.status_code == 422
    assert (await stored_versions(session, event["id"]))[0].status == "under_review"

    rejected = action(client, event["id"], "reject", rationale="the client told us it was wrong")

    assert rejected.status_code == 200, rejected.text
    assert rejected.json()["status"] == "deleted"
    assert rejected.json()["rationale"] == "the client told us it was wrong"
    row = (await stored_versions(session, event["id"]))[0]
    assert row.reviewed_by is None  # the review was rejected, so no review stamp
    assert (await change_event_entries(session, tenant.id))[-1].details["rationale"] == (
        "the client told us it was wrong"
    )


async def test_a_staff_recorded_withdrawal_needs_the_clients_words(
    session: AsyncSession,
) -> None:
    """Scope §2.7's phone/in-person case: the client cannot reply by email, so
    the staff member records the withdrawal — and the rationale is where the
    channel and what the client said live. Without one, refused."""
    client, tenant, _, _, company_id = await deployment(session)
    event = enter_event(client, company_id)

    refused = action(client, event["id"], "client_withdraw")
    assert refused.status_code == 422, refused.text
    assert refused.json()["detail"] == "rationale_required"

    withdrawn = action(
        client,
        event["id"],
        "client_withdraw",
        rationale="client rang on 25 September: please withdraw, the director changed his mind",
    )

    assert withdrawn.status_code == 200, withdrawn.text
    assert withdrawn.json()["status"] == "withdrawn"
    assert withdrawn.json()["rationale"].startswith("client rang on 25 September")
    assert (await change_event_entries(session, tenant.id))[-1].details == {
        "action": "client_withdraw",
        "before": "pending",
        "after": "withdrawn",
        "version_no": 1,
        "rationale": withdrawn.json()["rationale"],
    }
    assert action(client, event["id"], "mark_under_review").status_code == 409


async def test_a_confirm_then_revert_takes_the_approval_back(session: AsyncSession) -> None:
    """A status and its stamps tell one story: reverting a confirmed event to
    ``reviewed`` clears the approval it no longer holds, and confirming again
    records a fresh one."""
    client, _, _, _, company_id = await deployment(session)
    event = enter_event(client, company_id)
    for name in ("mark_under_review", "mark_reviewed", "confirm"):
        assert action(client, event["id"], name).status_code == 200

    reverted = action(client, event["id"], "revert")

    assert reverted.status_code == 200, reverted.text
    body = reverted.json()
    assert body["status"] == "reviewed"
    assert body["approved_by"] is None and body["approved_at"] is None
    assert body["reviewed_by"] is not None  # the review still stands
    again = action(client, event["id"], "confirm")
    assert again.status_code == 200
    assert again.json()["approved_by"] is not None


# --- The refusals the build owes a name (requirement 7) ---------------------


@pytest.mark.parametrize(
    ("name", "reason"),
    [
        ("override", "reconfirmation_not_wired"),
        ("supersede", "supersede_not_wired"),
    ],
)
async def test_an_unwired_action_is_a_named_409_and_changes_nothing(
    session: AsyncSession, name: str, reason: str
) -> None:
    """Never silently accepted, never half-performed: the event stays where it
    is and the chain gains nothing. The answer is the same for an
    Administrator as for anyone — the action has no side effects to perform,
    whatever the actor's authority. M5b wired the reconfirmation exchange, so
    ``reconfirmation_not_wired`` names ``override`` alone; P6-4 wired
    ``submit`` as the system row the export performs (its own test below)."""
    client, tenant, _, _, company_id = await deployment(session)
    event = enter_event(client, company_id)
    before = await change_event_entries(session, tenant.id)

    response = action(client, event["id"], name, rationale="please")

    assert response.status_code == 409, response.text
    assert response.json()["detail"] == reason
    assert (await stored_versions(session, event["id"]))[0].status == "pending"
    assert await change_event_entries(session, tenant.id) == before


async def test_a_staff_post_of_submit_is_the_system_row_refusal_and_changes_nothing(
    session: AsyncSession,
) -> None:
    """P6-4: ``submit`` is wired — as §4.1's **system** row, which the draft's
    export performs. A staff POST answers the system-row refusal (409
    ``transition_not_allowed``, the ``apply`` precedent — ``apply`` at
    ``confirmed`` answers it too, its own system row), the event stays, and
    the chain gains nothing. The real move is the export's own
    (``test_drafts_scenarios_db.py``'s lifecycle pins it)."""
    client, tenant, _, _, company_id = await deployment(session)
    event = enter_event(client, company_id)
    for name in ("mark_under_review", "mark_reviewed", "confirm"):
        assert action(client, event["id"], name).status_code == 200
    applied = action(client, event["id"], "apply")
    assert applied.status_code == 409  # the system-row refusal through the door
    assert applied.json()["detail"] == "transition_not_allowed"

    before = await change_event_entries(session, tenant.id)
    response = action(client, event["id"], "submit")
    assert response.status_code == 409
    assert response.json()["detail"] == "transition_not_allowed"
    assert (await stored_versions(session, event["id"]))[0].status == "confirmed"
    assert await change_event_entries(session, tenant.id) == before


async def test_the_systems_apply_is_not_a_staff_action(session: AsyncSession) -> None:
    """``apply`` is wired since slice F2 — as §4.1's **system** row, which draft
    generation performs. It is still nobody's to press: the transitions read
    never offers it, and a staff POST answers the executor's system-row refusal
    (``transition_not_allowed``), moving nothing."""
    client, tenant, _, _, company_id = await deployment(session)
    event = enter_event(client, company_id)
    confirmed = None
    for name in ("mark_under_review", "mark_reviewed", "confirm"):
        confirmed = action(client, event["id"], name)
        assert confirmed.status_code == 200, confirmed.text
    assert confirmed is not None
    assert confirmed.json()["status"] == "confirmed"
    before = await change_event_entries(session, tenant.id)

    response = action(client, event["id"], "apply")

    assert response.status_code == 409, response.text
    assert response.json()["detail"] == "transition_not_allowed"
    assert (await stored_versions(session, event["id"]))[0].status == "confirmed"
    assert await change_event_entries(session, tenant.id) == before
    read = client.get(f"/api/v1/change-events/{event['id']}/transitions").json()
    assert "apply" not in [item["action"] for item in read["actions"]]


async def test_an_unknown_action_is_a_422_from_the_vocabulary(
    session: AsyncSession,
) -> None:
    client, _, _, _, company_id = await deployment(session)
    event = enter_event(client, company_id)

    response = action(client, event["id"], "frobnicate")

    assert response.status_code == 422


async def test_an_edit_cannot_carry_the_linkage(session: AsyncSession) -> None:
    """The linkage is how the event got here, not what it says: an edit field
    the body does not offer is a 422 rather than an ignored key."""
    client, _, _, _, company_id = await deployment(session)
    event = enter_event(client, company_id)

    response = client.post(
        f"/api/v1/change-events/{event['id']}/transitions",
        json={"action": "edit", "related_event_id": str(uuid4())},
    )

    assert response.status_code == 422


# --- The transitions read (the actor's view) --------------------------------


async def test_the_transitions_read_is_the_actors_own(
    session: AsyncSession,
) -> None:
    """One event, three accounts, three answers: the Administrator may do
    everything the table admits, the reviewer the review half, the approver
    the approval half — and M5b's wired ``send_reconfirmation`` is a reviewer's
    offer at ``pending``, while the system rows (``client_reply``) are in
    nobody's list."""
    _, tenant, admin, general, company_id = await deployment(session)
    event = enter_event(make_client(session, ActorRef(admin.id, tenant.id)), company_id)
    event_id = event["id"]
    general_row = await session.get(User, general.id)
    assert general_row is not None

    admin_actions = read_actions(session, admin, tenant, event_id)
    general_row.review_events, general_row.approve_events = True, False
    await session.flush()
    reviewer_actions = read_actions(session, general, tenant, event_id)
    general_row.review_events, general_row.approve_events = False, True
    await session.flush()
    approver_actions = read_actions(session, general, tenant, event_id)

    assert [item["action"] for item in admin_actions] == [
        "mark_under_review",
        "send_reconfirmation",
        "edit",
        "delete",
        "client_withdraw",
    ]
    assert [item["action"] for item in reviewer_actions] == [
        "mark_under_review",
        "send_reconfirmation",
        "edit",
        "delete",
        "client_withdraw",
    ]
    assert approver_actions == []
    offered = {item["action"] for item in admin_actions}
    assert "client_reply" not in offered
    assert "override" not in offered


def read_actions(
    session: AsyncSession, user: User, tenant: Tenant, event_id: str
) -> list[dict]:
    """The transitions read for one account, through the real RBAC loader."""
    client = make_client(session, ActorRef(user_id=user.id, tenant_id=tenant.id))
    response = client.get(f"/api/v1/change-events/{event_id}/transitions")
    assert response.status_code == 200, response.text
    return response.json()["actions"]


async def test_the_transitions_read_reports_the_state_the_actor_sees(
    session: AsyncSession,
) -> None:
    client, _, _, _, company_id = await deployment(session)
    event = enter_event(client, company_id)
    assert action(client, event["id"], "mark_under_review").status_code == 200
    assert action(client, event["id"], "mark_reviewed").status_code == 200

    response = client.get(f"/api/v1/change-events/{event['id']}/transitions")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["event_id"] == event["id"]
    assert body["status"] == "reviewed"
    assert body["version_no"] == 1
    assert body["actions"] == [
        {"action": "confirm", "to_status": "confirmed", "rationale_required": False},
        {"action": "revert", "to_status": "under_review", "rationale_required": False},
        {"action": "reject", "to_status": "deleted", "rationale_required": True},
        {
            "action": "client_withdraw",
            "to_status": "withdrawn",
            "rationale_required": True,
        },
    ]


# --- The reconfirmation exchange (M5b: the §4.1 rows wired) -------------------


MAILBOX = "ai-filings@tcsp.example"
COMPANY_NAME = "Bright Lantern Limited"
REQUEST_CHECKLIST = [
    {"fact_id": "officers.0.name_en", "question": "Is the director unchanged? / 董事有否變更？"}
]
REQUEST_RECIPIENTS = [{"person_id": None, "address": "director@example.hk"}]
ANCHOR_MESSAGE_ID = "<annual-return-request@tcsp.example>"
AT = datetime(2026, 9, 3, 9, 0, tzinfo=UTC)


async def name_company(session: AsyncSession, tenant_id: UUID, company_id: UUID) -> None:
    """The name record the letter's identification rule reads (scope §7)."""
    session.add(
        CompanyNameRecord(
            tenant_id=tenant_id,
            company_id=company_id,
            effective_date=date(2015, 1, 1),
            source_type="manual",
            name_en=COMPANY_NAME,
            name_zh=None,
            business_name=None,
        )
    )
    await session.flush()


async def seed_request_thread(
    session: AsyncSession, tenant_id: UUID, company_id: UUID
) -> tuple[ConfirmationRequest, Communication]:
    """The sent request, its accepted send (the thread anchor), and the reply
    that proposed the event — the reconfirmation answers on this thread."""
    request_letter = Document(
        tenant_id=tenant_id,
        company_id=company_id,
        kind="generated_draft",
        origin="generated",
        retention_class="client_communication",
        sha256="c" * 64,
        storage_key=f"test/{uuid4()}.eml",
        byte_size=1,
        content_type="message/rfc822",
        scan_status="clean",
    )
    reply_document = Document(
        tenant_id=tenant_id,
        company_id=company_id,
        kind="client_reply",
        origin="email_attachment",
        retention_class="client_communication",
        sha256="d" * 64,
        storage_key=f"test/{uuid4()}.eml",
        byte_size=1,
        content_type="message/rfc822",
        scan_status="clean",
    )
    session.add_all([request_letter, reply_document])
    await session.flush()
    request = ConfirmationRequest(
        tenant_id=tenant_id,
        company_id=company_id,
        form_type="nar1",
        target_date=date(2027, 3, 1),
        reply_by=date(2027, 1, 15),
        status="sent",
        version_no=1,
        document_id=request_letter.id,
        checklist=REQUEST_CHECKLIST,
        recipients=REQUEST_RECIPIENTS,
    )
    session.add(request)
    await session.flush()
    session.add(
        MailOutbox(
            tenant_id=tenant_id,
            company_id=company_id,
            confirmation_request_id=request.id,
            destination="client",
            status="sent",
            kind="confirmation_request",
            message_id=ANCHOR_MESSAGE_ID,
            sent_at=datetime(2026, 9, 1, tzinfo=UTC),
        )
    )
    reply = Communication(
        tenant_id=tenant_id,
        company_id=company_id,
        direction="inbound",
        channel="email",
        kind="reply",
        thread_id=f"thread-{uuid4()}",
        sender_address="director@example.hk",
        confirmation_request_id=request.id,
        received_at=datetime(2026, 9, 2, tzinfo=UTC),
        raw_document_id=reply_document.id,
    )
    session.add(reply)
    await session.flush()
    return request, reply


async def enter_sourced_event(
    session: AsyncSession, tenant_id: UUID, company_id: UUID, communication_id: UUID
) -> ChangeEvent:
    """One pending event the reply proposed — the send_reconfirmation subject."""
    view = await change_events.create(
        session,
        tenant_id=tenant_id,
        company_id=company_id,
        event_type=ChangeEventType.DIRECTOR_APPOINTMENT,
        effective_date=date(2026, 9, 1),
        payload=APPOINTMENT_PAYLOAD,
        as_of=date.today(),
        source_type=ChangeEventSourceType.COMMUNICATION,
        source_id=communication_id,
    )
    return view.current


async def reconfirmation_rows(
    session: AsyncSession, tenant_id: UUID, company_id: UUID
) -> list[MailOutbox]:
    rows = await session.scalars(
        select(MailOutbox).where(
            MailOutbox.tenant_id == tenant_id,
            MailOutbox.company_id == company_id,
            MailOutbox.kind == OutboxKind.RECONFIRMATION.value,
        )
    )
    return list(rows.all())


async def test_send_reconfirmation_composes_stores_releases_and_moves(
    session: AsyncSession, store: "FakeStore", monkeypatch: pytest.MonkeyPatch
) -> None:
    """The review queue's re-ask, wired: the letter composes from the event
    itself (the deterministic bilingual restatement, the company named by name
    and CR number, in the request's thread to the request's recipients), the
    outbox row pends send-on-release under the default client mode, and only
    then does the event move — one audit entry per write, letter before move."""
    monkeypatch.setattr(settings, "mail_address", MAILBOX)
    client, tenant, admin, _, company_id = await deployment(session)
    await name_company(session, tenant.id, UUID(company_id))
    request, reply = await seed_request_thread(session, tenant.id, UUID(company_id))
    event = await enter_sourced_event(session, tenant.id, UUID(company_id), reply.id)
    client = make_client(session, ActorRef(admin.id, tenant.id), store)

    response = action(client, str(event.id), "send_reconfirmation")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "client_reconfirming"
    assert body["version_no"] == 1  # a status move updates in place

    # The stored letter: the records-registry home, the thread, the identification.
    (row,) = await reconfirmation_rows(session, tenant.id, UUID(company_id))
    assert row.status == OutboxStatus.PENDING.value
    assert row.destination == "client"
    assert row.confirmation_request_id == request.id
    assert row.not_before is None  # the default client mode is send-on-release
    assert row.created_by == admin.id  # a staff action, unlike the parse tail's
    (document,) = (
        await session.scalars(select(Document).where(Document.id == row.letter_document_id))
    ).all()
    assert document.kind == "generated_draft"
    assert document.retention_class == "client_communication"
    message = message_from_bytes(store.objects[document.storage_key], policy=default_policy)
    assert str(message["To"]) == "director@example.hk"
    assert str(message["In-Reply-To"]) == ANCHOR_MESSAGE_ID
    assert str(message["Subject"]) == (
        f"[{COMPANY_NAME} — CR {COMPANY['cr_number']}] "
        "Reconfirmation — please confirm the changes you reported"
    )
    text = message.get_body(preferencelist=("plain",)).get_content()
    assert (
        f"Thank you for your reply regarding {COMPANY_NAME} "
        f"(CR Number {COMPANY['cr_number']})" in text
    )
    assert "Appointment of a director / 委任董事" in text
    assert "CHAN Tai Man" in text
    assert "2026-09-01" in text

    # The chain: entry, the letter's two, then the move — coordinates only.
    entries = await change_event_entries(session, tenant.id)
    assert [(entry.action, entry.entity_type) for entry in entries] == [
        (AuditAction.CHANGE_EVENT_RECORDED.value, "change_event"),
        (AuditAction.CHANGE_EVENT_TRANSITION_RECORDED.value, "change_event"),
    ]
    (move,) = [
        entry
        for entry in entries
        if entry.action == AuditAction.CHANGE_EVENT_TRANSITION_RECORDED.value
    ]
    assert move.details["action"] == "send_reconfirmation"
    assert move.details["before"] == "pending"
    assert move.details["after"] == "client_reconfirming"
    assert move.details["version_no"] == 1
    assert move.actor_user_id == admin.id
    reconfirmation_audit = [
        entry
        for entry in await audit_entries(session, tenant.id)
        if entry.action == "reconfirmation_enqueued"
    ]
    assert len(reconfirmation_audit) == 1
    assert reconfirmation_audit[0].details["event_id"] == str(event.id)
    assert reconfirmation_audit[0].details["outbox_id"] == str(row.id)

    # The read behind the buttons: out with the client, the re-ask is gone and
    # the system row is nobody's.
    read = client.get(f"/api/v1/change-events/{event.id}/transitions").json()
    offered = {item["action"] for item in read["actions"]}
    assert read["status"] == "client_reconfirming"
    assert "send_reconfirmation" not in offered
    assert "client_reply" not in offered
    assert "override" not in offered


async def test_send_reconfirmation_holds_under_review_required_until_a_person_releases(
    session: AsyncSession, store: "FakeStore", monkeypatch: pytest.MonkeyPatch
) -> None:
    """The client destination's SendMode governs the staff-pressed letter the
    same way it governs the parse tail's: ``review_required`` parks the row on
    HELD_NOT_BEFORE, and the reminder release's own path (widened, not
    doubled) moves it to the press instant."""
    monkeypatch.setattr(settings, "mail_address", MAILBOX)
    client, tenant, admin, _, company_id = await deployment(session)
    await name_company(session, tenant.id, UUID(company_id))
    session.add(
        TenantConfig(
            tenant_id=tenant.id,
            naming_templates={
                "document": DEFAULT_DOCUMENT_TEMPLATE,
                "generated": DEFAULT_GENERATED_TEMPLATE,
            },
            milestones=default_milestones().as_dict(),
            send_modes={"client": "review_required", "government": "review_required"},
        )
    )
    await session.flush()
    request, reply = await seed_request_thread(session, tenant.id, UUID(company_id))
    event = await enter_sourced_event(session, tenant.id, UUID(company_id), reply.id)
    client = make_client(session, ActorRef(admin.id, tenant.id), store)

    response = action(client, str(event.id), "send_reconfirmation")

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "client_reconfirming"
    (row,) = await reconfirmation_rows(session, tenant.id, UUID(company_id))
    assert row.not_before == HELD_NOT_BEFORE
    due = await outbox.pending_jobs(session, tenant_id=tenant.id, now=datetime.now(UTC))
    assert all(job.id != row.id for job in due)  # owed but not due: no send before a person

    released = await outbox.release_reminders(
        session,
        tenant_id=tenant.id,
        company_id=UUID(company_id),
        request_id=request.id,
        actor_user_id=admin.id,
        now=AT,
    )
    assert [job.id for job in released] == [row.id]
    assert row.not_before == AT
    (released_entry,) = [
        entry
        for entry in await audit_entries(session, tenant.id)
        if entry.action == "reconfirmation_released"
    ]
    assert released_entry.actor_user_id == admin.id
    assert released_entry.details["outbox_id"] == str(row.id)


async def test_send_reconfirmation_on_an_event_with_no_request_thread_refuses(
    session: AsyncSession, store: "FakeStore", monkeypatch: pytest.MonkeyPatch
) -> None:
    """The letter needs the request's recipients and thread (the outbox's
    request reference is NOT NULL): a hand-entered event answers
    ``no_confirmation_request``, and nothing is written — the event stays
    ``pending`` and the chain gains nothing. The status admits the action; the
    event's provenance is what refuses."""
    monkeypatch.setattr(settings, "mail_address", MAILBOX)
    client, tenant, admin, _, company_id = await deployment(session)
    await name_company(session, tenant.id, UUID(company_id))
    event = enter_event(client, company_id)
    before = await change_event_entries(session, tenant.id)
    client = make_client(session, ActorRef(admin.id, tenant.id), store)

    response = action(client, event["id"], "send_reconfirmation")

    assert response.status_code == 422, response.text
    assert response.json()["detail"] == "no_confirmation_request"
    assert (await stored_versions(session, event["id"]))[0].status == "pending"
    assert await reconfirmation_rows(session, tenant.id, UUID(company_id)) == []
    assert await change_event_entries(session, tenant.id) == before


async def test_client_reply_is_the_system_row_no_staff_actor_can_press(
    session: AsyncSession, store: "FakeStore", monkeypatch: pytest.MonkeyPatch
) -> None:
    """``client_reply`` is wired — as the system row the reply parser's landing
    performs. No staff account presses it, Administrator included: the POST
    answers the system-row refusal and nothing moves."""
    monkeypatch.setattr(settings, "mail_address", MAILBOX)
    client, tenant, admin, _, company_id = await deployment(session)
    await name_company(session, tenant.id, UUID(company_id))
    _, reply = await seed_request_thread(session, tenant.id, UUID(company_id))
    event = await enter_sourced_event(session, tenant.id, UUID(company_id), reply.id)
    client = make_client(session, ActorRef(admin.id, tenant.id), store)
    sent = action(client, str(event.id), "send_reconfirmation")
    assert sent.status_code == 200, sent.text
    before = await change_event_entries(session, tenant.id)

    response = action(client, str(event.id), "client_reply")

    assert response.status_code == 409, response.text
    assert response.json()["detail"] == "transition_not_allowed"
    assert (await stored_versions(session, str(event.id)))[0].status == "client_reconfirming"
    assert await change_event_entries(session, tenant.id) == before


# --- The linkage rules (§2.6) ------------------------------------------------


async def test_a_manual_event_can_correct_an_earlier_one_of_its_company(
    session: AsyncSession,
) -> None:
    """Scope §2.7's correction case: a second event points at the first with
    ``relation: corrects``, and the first is left exactly as it was — the
    linkage records the relationship, and no status moves behind the client's
    back (a withdrawal is the ``client_withdraw`` action)."""
    client, _, _, _, company_id = await deployment(session)
    first = enter_event(client, company_id)

    correction = enter_event(
        client,
        company_id,
        related_event_id=first["id"],
        relation="corrects",
        payload={"summary": "the appointment date was wrong"},
        type="other",
    )

    assert correction["related_event_id"] == first["id"]
    assert correction["relation"] == "corrects"
    assert correction["status"] == "pending"
    assert (await stored_versions(session, first["id"]))[0].status == "pending"


async def test_the_linkage_rules_refuse_a_half_stated_reference(
    session: AsyncSession,
) -> None:
    """Four ways a reference is not one: a relation with nothing to refer to, a
    reference with no relation, a target in another company, and a target that
    does not exist — each refused with the field named, and nothing stored."""
    client, tenant, _, _, company_id = await deployment(session)
    first = enter_event(client, company_id)
    other = client.post("/api/v1/companies", json=company_body("87654321"))
    assert other.status_code == 201, other.text
    other_event = enter_event(client, str(other.json()["id"]))

    bodies = [
        {"relation": "corrects"},
        {"related_event_id": first["id"]},
        {"related_event_id": other_event["id"], "relation": "corrects"},
        {"related_event_id": str(uuid4()), "relation": "corrects"},
    ]

    for body in bodies:
        response = client.post(
            f"/api/v1/companies/{company_id}/change-events", json={**ENTRY_BODY, **body}
        )
        assert response.status_code == 422, (body, response.text)
        assert response.json()["detail"].startswith("change_event_invalid: "), body
    assert len(await change_event_entries(session, tenant.id)) == 2  # the two entries only


async def test_a_submitted_event_can_only_be_superseded(session: AsyncSession) -> None:
    """§2.6: once an event is in a submitted filing, the only reference it
    admits is a supersession — a filed fact is not corrected or withdrawn, it
    is replaced. (``submitted`` is unreachable through this slice's actions,
    so the row is put there directly; the rule is what is under test.)"""
    client, _, _, _, company_id = await deployment(session)
    first = enter_event(client, company_id)
    row = (await stored_versions(session, first["id"]))[0]
    row.status = ChangeEventStatus.SUBMITTED.value
    await session.flush()

    refused = client.post(
        f"/api/v1/companies/{company_id}/change-events",
        json={**ENTRY_BODY, "related_event_id": first["id"], "relation": "corrects"},
    )
    superseding = client.post(
        f"/api/v1/companies/{company_id}/change-events",
        json={**ENTRY_BODY, "related_event_id": first["id"], "relation": "supersedes"},
    )

    assert refused.status_code == 422, refused.text
    assert "superseded" in refused.json()["detail"]
    assert superseding.status_code == 201, superseding.text
    assert superseding.json()["relation"] == "supersedes"


# --- Tenancy ----------------------------------------------------------------


async def test_another_tenants_event_is_unknown(session: AsyncSession) -> None:
    """One answer for unknown and cross-tenant ids, on every id-scoped route."""
    client, _, _, _, company_id = await deployment(session)
    event = enter_event(client, company_id)
    other_tenant, other_admin = await seed_tenant(session)
    other_client = make_client(session, ActorRef(user_id=other_admin.id, tenant_id=other_tenant.id))

    transitions = other_client.get(f"/api/v1/change-events/{event['id']}/transitions")
    performed = other_client.post(
        f"/api/v1/change-events/{event['id']}/transitions", json={"action": "confirm"}
    )
    assessments = other_client.get(f"/api/v1/change-events/{event['id']}/assessments")

    assert transitions.status_code == 404
    assert performed.status_code == 404
    assert assessments.status_code == 404
    # And the company-scoped read is a 404 for the other tenant's company too.
    assert other_client.get(f"/api/v1/companies/{company_id}/change-events").status_code == 404
    assert (await stored_versions(session, event["id"]))[0].status == "pending"


async def test_a_cross_tenant_row_is_uninsertable(session: AsyncSession) -> None:
    """The F1 pattern: the company composite foreign key pins the row to its
    own tenant, so a change event cannot describe another tenant's company."""
    _, tenant, admin, _, _ = await deployment(session)
    other_tenant, other_admin = await seed_tenant(session)
    other_company = Company(
        tenant_id=other_tenant.id,
        br_number="99999999",
        date_of_incorporation=date(2011, 6, 1),
        anniversary_date=date(2027, 6, 1),
        type="private",
    )
    session.add(other_company)
    await session.flush()

    with pytest.raises(IntegrityError):
        async with session.begin_nested():
            session.add(
                ChangeEvent(
                    tenant_id=tenant.id,
                    company_id=other_company.id,
                    version_no=1,
                    type="other",
                    status="pending",
                    effective_date=date(2026, 9, 1),
                    payload={"summary": "cross-tenant attempt"},
                    source_type="manual",
                    created_by=admin.id,
                )
            )
            await session.flush()
    # And another tenant's user cannot be the creator of this tenant's row.
    own_company_id = (
        await session.scalars(select(Company.id).where(Company.tenant_id == tenant.id))
    ).one()
    with pytest.raises(IntegrityError):
        async with session.begin_nested():
            session.add(
                ChangeEvent(
                    tenant_id=tenant.id,
                    company_id=own_company_id,
                    version_no=1,
                    type="other",
                    status="pending",
                    effective_date=date(2026, 9, 1),
                    payload={"summary": "another tenant's actor"},
                    source_type="manual",
                    created_by=other_admin.id,
                )
            )
            await session.flush()


async def test_a_company_with_no_events_reads_an_empty_list(session: AsyncSession) -> None:
    client, _, _, _, company_id = await deployment(session)

    response = client.get(f"/api/v1/companies/{company_id}/change-events")

    assert response.status_code == 200
    assert response.json() == []


# --- The assessments read ---------------------------------------------------


async def test_the_assessments_read_is_empty_until_the_parser_exists(
    session: AsyncSession,
) -> None:
    """Truthfully empty: a hand-entered event has no parse behind it, and no
    writer for one exists in this slice."""
    client, _, _, _, company_id = await deployment(session)
    event = enter_event(client, company_id)

    response = client.get(f"/api/v1/change-events/{event['id']}/assessments")

    assert response.status_code == 200, response.text
    assert response.json() == []


async def test_an_assessment_linked_to_the_event_is_returned(
    session: AsyncSession,
) -> None:
    """The read works for the writer that will exist: an assessment with
    ``subject_type=change_event`` comes back with its scores and routing. The
    row is inserted directly — this slice writes none."""
    client, tenant, _, _, company_id = await deployment(session)
    event = enter_event(client, company_id)
    session.add(
        ExtractionAssessment(
            tenant_id=tenant.id,
            subject_type="change_event",
            subject_id=UUID(event["id"]),
            component="reply_parser",
            model="pinned-model/v1",
            field_scores={"officer.role": 0.4},
            signal_log={"fields": {"officer.role": ["value_unvalidated"]}},
            overall_score=0.4,
            routing="client_clarification",
        )
    )
    await session.flush()

    response = client.get(f"/api/v1/change-events/{event['id']}/assessments")

    assert response.status_code == 200, response.text
    body = response.json()
    assert len(body) == 1
    assert body[0]["subject_type"] == "change_event"
    assert body[0]["subject_id"] == event["id"]
    assert body[0]["field_scores"] == {"officer.role": 0.4}
    assert body[0]["overall_score"] == 0.4
    assert body[0]["routing"] == "client_clarification"


async def test_the_reads_write_nothing(session: AsyncSession) -> None:
    """Asking is not a change: the list, the transitions read and the
    assessments read append nothing to the chain and create no row."""
    client, tenant, _, _, company_id = await deployment(session)
    event = enter_event(client, company_id)
    before = await audit_entries(session, tenant.id)

    client.get(f"/api/v1/companies/{company_id}/change-events")
    client.get(f"/api/v1/change-events/{event['id']}/transitions")
    client.get(f"/api/v1/change-events/{event['id']}/assessments")

    assert await audit_entries(session, tenant.id) == before
    assert len(await stored_versions(session, event["id"])) == 1
