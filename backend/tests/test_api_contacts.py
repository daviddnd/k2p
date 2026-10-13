"""API-layer tests for the external-contact endpoints.

These run without a database: the auth seam, RBAC context, and session are
overridden, and the application services are monkeypatched, so only
fail-closed authentication, the single Administrator-only authorization
check (``require_admin`` on every route — Flow 1 step 3 is an administrative
function), request validation, and the typed-error → HTTP mappings are
exercised. Real flows against PostgreSQL live in
``test_contacts_scenarios_db.py``.
"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.persistence.contacts import CompanyExternalContact, ContactChannel, Person
from app.adapters.persistence.session import get_session
from app.api.deps import ActorRef, get_actor_context, get_current_actor
from app.application import contacts
from app.application.company_master import CompanyNotFoundError
from app.domain.contacts import ChannelStatus, ChannelType, InvalidChannelIdentifierError
from app.domain.rbac import ActorContext, Permission, Role
from app.main import create_app

ADMIN = ActorRef(user_id=uuid4(), tenant_id=uuid4())
PERSON_ID = uuid4()
CHANNEL_ID = uuid4()
COMPANY_ID = uuid4()

ADMIN_CONTEXT = ActorContext(
    is_active=True, role=Role.ADMINISTRATOR, permissions=frozenset(), company_ids=frozenset()
)
GENERAL_CONTEXT = ActorContext(
    is_active=True,
    role=Role.GENERAL,
    permissions=frozenset(Permission),
    company_ids=frozenset({COMPANY_ID}),  # even a grant for the path company
)
INACTIVE_ADMIN_CONTEXT = ActorContext(
    is_active=False, role=Role.ADMINISTRATOR, permissions=frozenset(), company_ids=frozenset()
)


def build_client(actor: ActorRef | None = None, context: ActorContext | None = None) -> TestClient:
    app: FastAPI = create_app()
    if actor is not None:
        app.dependency_overrides[get_current_actor] = lambda: actor
    if context is not None:
        app.dependency_overrides[get_actor_context] = lambda: context

    async def no_session() -> AsyncIterator[AsyncSession]:
        yield None  # type: ignore[misc]  # services are monkeypatched in these tests

    app.dependency_overrides[get_session] = no_session
    return TestClient(app)


CREATE_PERSON_PAYLOAD = {"name_en": "CHAN TAI MAN", "email": "chantm@example.com"}
ADD_CHANNEL_PAYLOAD = {"channel_type": "email", "identifier": "chantm@example.com"}
UPDATE_CHANNEL_PAYLOAD = {"status": "bounced"}
ASSOCIATE_PAYLOAD = {"person_id": str(PERSON_ID)}

ROUTES = [
    ("POST", "/api/v1/persons", CREATE_PERSON_PAYLOAD),
    ("GET", "/api/v1/persons", None),
    ("GET", f"/api/v1/persons/{PERSON_ID}", None),
    ("PATCH", f"/api/v1/persons/{PERSON_ID}", {"name_zh": "陳大文"}),
    ("POST", f"/api/v1/persons/{PERSON_ID}/contact-channels", ADD_CHANNEL_PAYLOAD),
    (
        "PATCH",
        f"/api/v1/persons/{PERSON_ID}/contact-channels/{CHANNEL_ID}",
        UPDATE_CHANNEL_PAYLOAD,
    ),
    ("GET", f"/api/v1/companies/{COMPANY_ID}/contacts", None),
    ("POST", f"/api/v1/companies/{COMPANY_ID}/contacts", ASSOCIATE_PAYLOAD),
    ("DELETE", f"/api/v1/companies/{COMPANY_ID}/contacts/{PERSON_ID}", None),
]


def _request(client: TestClient, method: str, url: str, payload: dict | None) -> int:
    if method == "GET":
        return client.get(url).status_code
    if method == "POST":
        return client.post(url, json=payload).status_code
    if method == "DELETE":
        return client.delete(url).status_code
    return client.patch(url, json=payload).status_code


# --- Fail closed; every route Administrator-only ---


def test_unauthenticated_fails_closed_on_all_routes() -> None:
    client = build_client()
    for method, url, payload in ROUTES:
        assert _request(client, method, url, payload) == 401, (method, url)


def test_general_is_forbidden_on_all_routes_even_with_company_grant() -> None:
    client = build_client(
        actor=ActorRef(user_id=uuid4(), tenant_id=ADMIN.tenant_id), context=GENERAL_CONTEXT
    )
    for method, url, payload in ROUTES:
        assert _request(client, method, url, payload) == 403, (method, url)


def test_inactive_administrator_is_forbidden() -> None:
    client = build_client(actor=ADMIN, context=INACTIVE_ADMIN_CONTEXT)
    for method, url, payload in ROUTES:
        assert _request(client, method, url, payload) == 403, (method, url)


# --- Request validation (422 before any service call) ---


def test_create_person_requires_at_least_one_name() -> None:
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)
    assert client.post("/api/v1/persons", json={"email": "a@b.com"}).status_code == 422
    assert client.post("/api/v1/persons", json={"name_en": ""}).status_code == 422
    assert (
        client.post("/api/v1/persons", json={"name_en": "A B", "email": "not-an-email"}).status_code
        == 422
    )


def test_update_person_requires_at_least_one_field() -> None:
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)
    assert client.patch(f"/api/v1/persons/{PERSON_ID}", json={}).status_code == 422


def test_add_channel_rejects_unknown_type_and_empty_identifier() -> None:
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)
    url = f"/api/v1/persons/{PERSON_ID}/contact-channels"
    assert client.post(url, json={"channel_type": "fax", "identifier": "123"}).status_code == 422
    assert client.post(url, json={"channel_type": "email", "identifier": ""}).status_code == 422


def test_update_channel_requires_at_least_one_field() -> None:
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)
    url = f"/api/v1/persons/{PERSON_ID}/contact-channels/{CHANNEL_ID}"
    assert client.patch(url, json={}).status_code == 422
    assert client.patch(url, json={"status": "lost"}).status_code == 422


def test_ids_must_be_uuids() -> None:
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)
    assert client.get("/api/v1/persons/not-a-uuid").status_code == 422
    assert (
        client.post(
            f"/api/v1/companies/{COMPANY_ID}/contacts", json={"person_id": "not-a-uuid"}
        ).status_code
        == 422
    )
    assert client.get("/api/v1/companies/not-a-uuid/contacts").status_code == 422


# --- Happy paths and error mappings (services monkeypatched) ---


def make_person(**overrides: object) -> Person:
    values: dict[str, object] = {
        "id": PERSON_ID,
        "tenant_id": ADMIN.tenant_id,
        "name_en": "CHAN TAI MAN",
        "name_zh": None,
        "email": "chantm@example.com",
        "is_external_contact": True,
    }
    values.update(overrides)
    return Person(**values)  # type: ignore[arg-type]


def make_channel(**overrides: object) -> ContactChannel:
    values: dict[str, object] = {
        "id": CHANNEL_ID,
        "person_id": PERSON_ID,
        "tenant_id": ADMIN.tenant_id,
        "channel_type": ChannelType.EMAIL.value,
        "identifier": "chantm@example.com",
        "status": ChannelStatus.ACTIVE.value,
        "verified_at": None,
    }
    values.update(overrides)
    return ContactChannel(**values)  # type: ignore[arg-type]


def test_create_person_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_create(session: object, **kwargs: object) -> Person:
        return make_person()

    monkeypatch.setattr(contacts, "create_person", fake_create)
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)

    response = client.post("/api/v1/persons", json=CREATE_PERSON_PAYLOAD)

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["id"] == str(PERSON_ID)
    assert body["is_external_contact"] is True
    assert body["channels"] == []


def test_create_person_answers_the_domains_named_reasons(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The create route maps the person write path's refusal the way the PATCH
    route does: a name the domain refuses is a 422 with its reason, never an
    unhandled error."""

    async def refusing_create(session: object, **kwargs: object) -> Person:
        raise contacts.PersonInvalidError(["person_name_required"])

    monkeypatch.setattr(contacts, "create_person", refusing_create)
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)

    response = client.post("/api/v1/persons", json=CREATE_PERSON_PAYLOAD)

    assert response.status_code == 422, response.text
    assert response.json()["detail"] == "person_name_required"


def test_list_and_get_persons_with_channels(monkeypatch: pytest.MonkeyPatch) -> None:
    channel = make_channel()
    row = contacts.PersonWithChannels(person=make_person(), channels=[channel])

    async def fake_list(session: object, **kwargs: object) -> list[contacts.PersonWithChannels]:
        return [row]

    async def fake_get(session: object, **kwargs: object) -> contacts.PersonWithChannels:
        return row

    monkeypatch.setattr(contacts, "list_persons", fake_list)
    monkeypatch.setattr(contacts, "get_person", fake_get)
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)

    listing = client.get("/api/v1/persons")
    assert listing.status_code == 200
    (person_body,) = listing.json()
    assert person_body["channels"][0]["identifier"] == "chantm@example.com"

    detail = client.get(f"/api/v1/persons/{PERSON_ID}")
    assert detail.status_code == 200
    assert detail.json()["channels"][0]["status"] == "active"


def test_get_person_not_found_maps_to_404(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_get(session: object, **kwargs: object) -> contacts.PersonWithChannels:
        raise contacts.PersonNotFoundError

    monkeypatch.setattr(contacts, "get_person", fake_get)
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)
    assert client.get(f"/api/v1/persons/{PERSON_ID}").status_code == 404


def test_add_channel_duplicate_maps_to_409(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_add(session: object, **kwargs: object) -> ContactChannel:
        raise contacts.ChannelIdentifierInUseError

    monkeypatch.setattr(contacts, "add_contact_channel", fake_add)
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)
    response = client.post(
        f"/api/v1/persons/{PERSON_ID}/contact-channels", json=ADD_CHANNEL_PAYLOAD
    )
    assert response.status_code == 409


def test_add_channel_invalid_identifier_maps_to_422(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_add(session: object, **kwargs: object) -> ContactChannel:
        raise InvalidChannelIdentifierError("bad")

    monkeypatch.setattr(contacts, "add_contact_channel", fake_add)
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)
    response = client.post(
        f"/api/v1/persons/{PERSON_ID}/contact-channels", json=ADD_CHANNEL_PAYLOAD
    )
    assert response.status_code == 422


def test_add_channel_unknown_person_maps_to_404(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_add(session: object, **kwargs: object) -> ContactChannel:
        raise contacts.PersonNotFoundError

    monkeypatch.setattr(contacts, "add_contact_channel", fake_add)
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)
    response = client.post(
        f"/api/v1/persons/{PERSON_ID}/contact-channels", json=ADD_CHANNEL_PAYLOAD
    )
    assert response.status_code == 404


def test_update_channel_status_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_update(session: object, **kwargs: object) -> ContactChannel:
        assert kwargs["status"] is ChannelStatus.BOUNCED
        return make_channel(status=ChannelStatus.BOUNCED.value, verified_at=None)

    monkeypatch.setattr(contacts, "update_contact_channel", fake_update)
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)

    response = client.patch(
        f"/api/v1/persons/{PERSON_ID}/contact-channels/{CHANNEL_ID}",
        json=UPDATE_CHANNEL_PAYLOAD,
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "bounced"


def test_update_channel_unknown_channel_maps_to_404(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_update(session: object, **kwargs: object) -> ContactChannel:
        raise contacts.ChannelNotFoundError

    monkeypatch.setattr(contacts, "update_contact_channel", fake_update)
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)
    response = client.patch(
        f"/api/v1/persons/{PERSON_ID}/contact-channels/{CHANNEL_ID}",
        json=UPDATE_CHANNEL_PAYLOAD,
    )
    assert response.status_code == 404


def test_associate_contact_returns_association_and_is_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created_at = datetime(2026, 8, 24, tzinfo=UTC)

    async def fake_associate(session: object, **kwargs: object) -> CompanyExternalContact:
        return CompanyExternalContact(
            company_id=COMPANY_ID,
            person_id=PERSON_ID,
            tenant_id=ADMIN.tenant_id,
            created_at=created_at,
        )

    monkeypatch.setattr(contacts, "associate_contact", fake_associate)
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)

    for _ in range(2):  # new and re-added both return 200 with the association
        response = client.post(f"/api/v1/companies/{COMPANY_ID}/contacts", json=ASSOCIATE_PAYLOAD)
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["company_id"] == str(COMPANY_ID)
        assert body["person_id"] == str(PERSON_ID)


def test_associate_unknown_person_or_company_maps_to_404(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def unknown_person(session: object, **kwargs: object) -> CompanyExternalContact:
        raise contacts.PersonNotFoundError

    async def unknown_company(session: object, **kwargs: object) -> CompanyExternalContact:
        raise CompanyNotFoundError

    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)
    monkeypatch.setattr(contacts, "associate_contact", unknown_person)
    assert (
        client.post(f"/api/v1/companies/{COMPANY_ID}/contacts", json=ASSOCIATE_PAYLOAD).status_code
        == 404
    )
    monkeypatch.setattr(contacts, "associate_contact", unknown_company)
    assert (
        client.post(f"/api/v1/companies/{COMPANY_ID}/contacts", json=ASSOCIATE_PAYLOAD).status_code
        == 404
    )


def test_dissociate_absent_association_maps_to_404(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_dissociate(session: object, **kwargs: object) -> None:
        raise contacts.AssociationNotFoundError

    monkeypatch.setattr(contacts, "dissociate_contact", fake_dissociate)
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)
    assert client.delete(f"/api/v1/companies/{COMPANY_ID}/contacts/{PERSON_ID}").status_code == 404


def test_dissociate_happy_path_returns_204(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_dissociate(session: object, **kwargs: object) -> None:
        return None

    monkeypatch.setattr(contacts, "dissociate_contact", fake_dissociate)
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)
    assert client.delete(f"/api/v1/companies/{COMPANY_ID}/contacts/{PERSON_ID}").status_code == 204
