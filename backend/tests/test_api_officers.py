"""API-layer tests for the officer/shareholding routes (contract §5.1; §2.4).

These run without a database: the auth seam, RBAC context and session are
overridden and the application service is monkeypatched, so only fail-closed
authentication, the grant-gated authorization checks, request validation and
the typed-error → HTTP mappings are exercised. The records themselves — the
half-open windows, the overlap refusal, the tenancy keys, the audit chain —
live in ``test_officers_scenarios_db.py``.
"""

from collections.abc import AsyncIterator
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.persistence.contacts import Person
from app.adapters.persistence.officers import OfficerAppointment, Shareholding
from app.adapters.persistence.session import get_session
from app.api.deps import ActorRef, get_actor_context, get_current_actor
from app.application import contacts, officers
from app.application.company_master import CompanyNotFoundError
from app.domain.rbac import ActorContext, Permission, Role
from app.main import create_app

COMPANY_ID = uuid4()
OTHER_COMPANY_ID = uuid4()
APPOINTMENT_ID = uuid4()
SHAREHOLDING_ID = uuid4()
PERSON_ID = uuid4()
USER_ID = uuid4()
TENANT_ID = uuid4()

ADMIN = ActorRef(user_id=USER_ID, tenant_id=TENANT_ID)
ADMIN_CONTEXT = ActorContext(
    is_active=True, role=Role.ADMINISTRATOR, permissions=frozenset(), company_ids=frozenset()
)
GENERAL_WITH_GRANT = ActorContext(
    is_active=True,
    role=Role.GENERAL,
    permissions=frozenset(Permission),
    company_ids=frozenset({COMPANY_ID}),
)
GENERAL_WITHOUT_GRANT = ActorContext(
    is_active=True,
    role=Role.GENERAL,
    permissions=frozenset(Permission),
    company_ids=frozenset({OTHER_COMPANY_ID}),
)
INACTIVE = ActorContext(
    is_active=False, role=Role.ADMINISTRATOR, permissions=frozenset(), company_ids=frozenset()
)

OFFICERS_URL = f"/api/v1/companies/{COMPANY_ID}/officers"
SHAREHOLDINGS_URL = f"/api/v1/companies/{COMPANY_ID}/shareholdings"
APPOINTMENT_URL = f"/api/v1/officers/{APPOINTMENT_ID}"
SHAREHOLDING_URL = f"/api/v1/shareholdings/{SHAREHOLDING_ID}"
PERSON_URL = f"/api/v1/persons/{PERSON_ID}"

ADDRESS = {
    "flat": "12A",
    "building": "Bright Tower",
    "street": "1 Queen's Road Central",
    "district": "Central",
}

APPOINTMENT_BODY: dict[str, Any] = {
    "person": {"person_type": "natural_person", "name_zh": "陳大文", "email": "a@b.com"},
    "role": "director",
    "capacity": ["director"],
    "valid_from": "2026-09-01",
    "correspondence_address": ADDRESS,
}

SHAREHOLDING_BODY: dict[str, Any] = {
    "person": {"person_type": "natural_person", "name_en": "CHAN Tai Man"},
    "class_name": "Ordinary",
    "shares_held": 1000,
    "percentage_held": "10.00",
    "amount_paid": "1000.00",
    "amount_unpaid": "0.00",
    "address": ADDRESS,
    "valid_from": "2026-09-01",
}


def build_client(actor: ActorRef | None = None, context: ActorContext | None = None) -> TestClient:
    app: FastAPI = create_app()
    if actor is not None:
        app.dependency_overrides[get_current_actor] = lambda: actor
    if context is not None:
        app.dependency_overrides[get_actor_context] = lambda: context

    async def no_session() -> AsyncIterator[AsyncSession]:
        yield None  # type: ignore[misc]  # the service is monkeypatched in these tests

    app.dependency_overrides[get_session] = no_session
    return TestClient(app)


def stored_person(**overrides: object) -> Person:
    """A person row as the service returns it (never persisted here)."""
    values: dict[str, Any] = {
        "id": PERSON_ID,
        "tenant_id": TENANT_ID,
        "person_type": "natural_person",
        "name_zh": "陳大文",
        "name_en": None,
        "surname_en": None,
        "other_names_en": None,
        "previous_names": [],
        "aliases": [],
        "email": "chan@brightlantern.example.hk",
        "br_number": None,
        "hkid_partial": "A1234",
        "passport_partial": None,
        "passport_issuing_country_region": None,
        "tcsp_licence": None,
    }
    values.update(overrides)
    return Person(**values)  # type: ignore[arg-type]


def stored_appointment(**overrides: object) -> OfficerAppointment:
    values: dict[str, Any] = {
        "id": APPOINTMENT_ID,
        "tenant_id": TENANT_ID,
        "company_id": COMPANY_ID,
        "person_id": PERSON_ID,
        "role": "director",
        "capacity": ["director"],
        "alternate_to": None,
        "correspondence_address": ADDRESS,
        "registered_office_address": None,
        "valid_from": date(2026, 9, 1),
        "valid_to": date(2026, 12, 1),
        "created_by": USER_ID,
        "created_at": datetime(2026, 9, 1, 8, 0, tzinfo=UTC),
        "updated_at": datetime(2026, 9, 1, 8, 0, tzinfo=UTC),
    }
    values.update(overrides)
    return OfficerAppointment(**values)  # type: ignore[arg-type]


def stored_shareholding(**overrides: object) -> Shareholding:
    values: dict[str, Any] = {
        "id": SHAREHOLDING_ID,
        "tenant_id": TENANT_ID,
        "company_id": COMPANY_ID,
        "person_id": PERSON_ID,
        "class_name": "Ordinary",
        "shares_held": 1000,
        "jointly_held": None,
        "remarks": None,
        "percentage_held": Decimal("10.00"),
        "allotment_date": date(2020, 1, 1),
        "amount_paid": Decimal("1000.00"),
        "amount_unpaid": Decimal("0.00"),
        "address": ADDRESS,
        "valid_from": date(2026, 9, 1),
        "valid_to": None,
        "created_by": USER_ID,
        "created_at": datetime(2026, 9, 1, 8, 0, tzinfo=UTC),
        "updated_at": datetime(2026, 9, 1, 8, 0, tzinfo=UTC),
    }
    values.update(overrides)
    return Shareholding(**values)  # type: ignore[arg-type]


def appointment_view(**overrides: object) -> officers.AppointmentWithPerson:
    return officers.AppointmentWithPerson(
        appointment=stored_appointment(**overrides), person=stored_person()
    )


def shareholding_view(**overrides: object) -> officers.ShareholdingWithPerson:
    return officers.ShareholdingWithPerson(
        shareholding=stored_shareholding(**overrides), person=stored_person()
    )


def _raise(error: Exception):
    async def fake(*args: object, **kwargs: object) -> object:
        raise error

    return fake


def _return(result: object):
    async def fake(*args: object, **kwargs: object) -> object:
        return result

    return fake


def _record_calls(store: dict[str, object], result: object):
    async def fake(*args: object, **kwargs: object) -> object:
        store.update(kwargs)
        return result

    return fake


# --- Fail closed on every route ---


def test_unauthenticated_fails_closed_on_every_route() -> None:
    client = build_client()
    assert client.get(OFFICERS_URL).status_code == 401
    assert client.post(OFFICERS_URL, json=APPOINTMENT_BODY).status_code == 401
    assert client.patch(APPOINTMENT_URL, json={"role": "director"}).status_code == 401
    assert client.delete(APPOINTMENT_URL).status_code == 401
    assert client.get(SHAREHOLDINGS_URL).status_code == 401
    assert client.post(SHAREHOLDINGS_URL, json=SHAREHOLDING_BODY).status_code == 401
    assert client.patch(SHAREHOLDING_URL, json={"shares_held": 1}).status_code == 401
    assert client.delete(SHAREHOLDING_URL).status_code == 401


def test_the_company_scoped_routes_refuse_an_actor_without_the_grant() -> None:
    """The path's company is asked of the single grant decision before
    anything else; master-data maintenance carries no permission flag."""
    client = build_client(actor=ADMIN, context=GENERAL_WITHOUT_GRANT)
    assert client.get(OFFICERS_URL).status_code == 403
    assert client.post(OFFICERS_URL, json=APPOINTMENT_BODY).status_code == 403
    assert client.get(SHAREHOLDINGS_URL).status_code == 403
    assert client.post(SHAREHOLDINGS_URL, json=SHAREHOLDING_BODY).status_code == 403


def test_an_inactive_account_is_refused() -> None:
    client = build_client(actor=ADMIN, context=INACTIVE)
    assert client.get(OFFICERS_URL).status_code == 403


def test_the_id_scoped_routes_refuse_an_actor_without_the_grant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The id-scoped routes have no company in the path, so the application
    layer resolves the row's company and asks the same single decision; the
    route answers its refusal."""
    monkeypatch.setattr(officers, "update_appointment", _raise(officers.OfficerForbiddenError()))
    monkeypatch.setattr(officers, "delete_appointment", _raise(officers.OfficerForbiddenError()))
    monkeypatch.setattr(officers, "update_shareholding", _raise(officers.OfficerForbiddenError()))
    monkeypatch.setattr(officers, "delete_shareholding", _raise(officers.OfficerForbiddenError()))
    client = build_client(actor=ADMIN, context=GENERAL_WITH_GRANT)

    assert client.patch(APPOINTMENT_URL, json={"role": "director"}).status_code == 403
    assert client.delete(APPOINTMENT_URL).status_code == 403
    assert client.patch(SHAREHOLDING_URL, json={"shares_held": 1}).status_code == 403
    assert client.delete(SHAREHOLDING_URL).status_code == 403


def test_patching_a_person_stays_administrator_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Persons are tenant-level and the identity correction is an
    administrative function — a granted General account is still refused,
    whatever the service would do."""
    monkeypatch.setattr(contacts, "update_person", _raise(RuntimeError("must not be reached")))
    client = build_client(actor=ADMIN, context=GENERAL_WITH_GRANT)

    assert client.patch(PERSON_URL, json={"surname_en": "CHAN"}).status_code == 403


# --- Refusals: the mapped typed errors ---


def test_unknown_or_cross_tenant_ids_are_404(monkeypatch: pytest.MonkeyPatch) -> None:
    """One answer for an unknown id and another tenant's, deliberately."""
    monkeypatch.setattr(officers, "update_appointment", _raise(officers.AppointmentNotFoundError()))
    monkeypatch.setattr(officers, "delete_appointment", _raise(officers.AppointmentNotFoundError()))
    monkeypatch.setattr(
        officers, "update_shareholding", _raise(officers.ShareholdingNotFoundError())
    )
    monkeypatch.setattr(
        officers, "delete_shareholding", _raise(officers.ShareholdingNotFoundError())
    )
    monkeypatch.setattr(
        officers, "list_appointments", _raise(CompanyNotFoundError())
    )
    monkeypatch.setattr(
        officers, "list_shareholdings", _raise(CompanyNotFoundError())
    )
    monkeypatch.setattr(officers, "create_appointment", _raise(CompanyNotFoundError()))
    monkeypatch.setattr(officers, "create_shareholding", _raise(CompanyNotFoundError()))
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)

    assert client.get(OFFICERS_URL).status_code == 404
    assert client.post(OFFICERS_URL, json=APPOINTMENT_BODY).status_code == 404
    assert client.patch(APPOINTMENT_URL, json={"role": "director"}).status_code == 404
    assert client.delete(APPOINTMENT_URL).status_code == 404
    assert client.get(SHAREHOLDINGS_URL).status_code == 404
    assert client.post(SHAREHOLDINGS_URL, json=SHAREHOLDING_BODY).status_code == 404
    assert client.patch(SHAREHOLDING_URL, json={"shares_held": 1}).status_code == 404
    assert client.delete(SHAREHOLDING_URL).status_code == 404


def test_an_unknown_person_id_is_a_404(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(officers, "create_appointment", _raise(contacts.PersonNotFoundError()))
    monkeypatch.setattr(officers, "create_shareholding", _raise(contacts.PersonNotFoundError()))
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)

    body = {**APPOINTMENT_BODY, "person": None, "person_id": str(PERSON_ID)}
    response = client.post(OFFICERS_URL, json=body)
    assert response.status_code == 404
    assert response.json()["detail"] == "person not found"
    body = {**SHAREHOLDING_BODY, "person": None, "person_id": str(PERSON_ID)}
    assert client.post(SHAREHOLDINGS_URL, json=body).status_code == 404


def test_the_domains_named_reasons_are_answered_verbatim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Contract §5.1: the 422 carries the domain's own reason — one request,
    one answer, and the reason is what a caller fixes."""
    monkeypatch.setattr(
        officers,
        "create_appointment",
        _raise(officers.OfficerInvalidError(["alternate_to_required", "address_required"])),
    )
    monkeypatch.setattr(
        officers,
        "create_shareholding",
        _raise(officers.OfficerInvalidError(["shareholding_invalid: shares_held"])),
    )
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)

    response = client.post(OFFICERS_URL, json=APPOINTMENT_BODY)
    assert response.status_code == 422
    assert response.json()["detail"] == "alternate_to_required; address_required"
    response = client.post(SHAREHOLDINGS_URL, json=SHAREHOLDING_BODY)
    assert response.status_code == 422
    assert response.json()["detail"] == "shareholding_invalid: shares_held"


def test_the_person_half_of_an_inline_create_answers_the_person_reasons(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An inline person is the contacts write path's, and its named reasons
    travel through the officer route unchanged (one vocabulary, two routes)."""
    monkeypatch.setattr(
        officers,
        "create_appointment",
        _raise(contacts.PersonInvalidError(["person_name_required"])),
    )
    monkeypatch.setattr(
        contacts,
        "update_person",
        _raise(contacts.PersonInvalidError(["name_history_invalid"])),
    )
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)

    response = client.post(OFFICERS_URL, json=APPOINTMENT_BODY)
    assert response.status_code == 422
    assert response.json()["detail"] == "person_name_required"
    response = client.patch(PERSON_URL, json={"previous_names": [{}]})
    assert response.status_code == 422
    assert response.json()["detail"] == "name_history_invalid"


def test_an_overlapping_window_is_a_409(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(officers, "create_appointment", _raise(officers.OfficerOverlapError()))
    monkeypatch.setattr(officers, "update_appointment", _raise(officers.OfficerOverlapError()))
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)

    response = client.post(OFFICERS_URL, json=APPOINTMENT_BODY)
    assert response.status_code == 409
    assert response.json()["detail"] == "officer_appointment_overlap"
    response = client.patch(APPOINTMENT_URL, json={"valid_to": "2026-10-01"})
    assert response.status_code == 409


# --- Request validation ---


def test_the_create_body_needs_exactly_one_person_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``person_id`` XOR ``person``: neither is a request with nobody to
    appoint, both is a request that cannot decide who."""
    monkeypatch.setattr(officers, "create_appointment", _return(appointment_view()))
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)

    assert (
        client.post(
            OFFICERS_URL, json={"role": "director", "valid_from": "2026-09-01"}
        ).status_code
        == 422
    )
    both = {
        **APPOINTMENT_BODY,
        "person_id": str(PERSON_ID),
    }
    assert client.post(OFFICERS_URL, json=both).status_code == 422
    assert (
        client.post(
            SHAREHOLDINGS_URL,
            json={"class_name": "Ordinary", "shares_held": 1, "valid_from": "2026-09-01"},
        ).status_code
        == 422
    )


def test_an_unknown_body_field_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """A misspelled field must not read as a record that omitted it."""
    monkeypatch.setattr(officers, "create_appointment", _return(appointment_view()))
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)

    assert (
        client.post(OFFICERS_URL, json={**APPOINTMENT_BODY, "role_name": "director"}).status_code
        == 422
    )
    assert client.patch(APPOINTMENT_URL, json={}).status_code == 422
    assert client.patch(SHAREHOLDING_URL, json={}).status_code == 422


# --- Happy paths (the service monkeypatched) ---


def test_creating_an_appointment_returns_the_read_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store: dict[str, object] = {}
    monkeypatch.setattr(
        officers, "create_appointment", _record_calls(store, appointment_view())
    )
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)

    response = client.post(OFFICERS_URL, json=APPOINTMENT_BODY)

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["role"] == "director"
    assert body["capacity"] == ["director"]
    assert body["valid_from"] == "2026-09-01"
    assert body["valid_to"] == "2026-12-01"
    assert body["correspondence_address"] == ADDRESS
    assert body["registered_office_address"] is None
    assert body["person"]["name_zh"] == "陳大文"
    assert body["person"]["hkid_partial"] == "A1234"
    assert store["company_id"] == COMPANY_ID
    assert store["role"].value == "director"
    assert store["person"] == {
        "person_type": "natural_person",
        "name_zh": "陳大文",
        "name_en": None,
        "surname_en": None,
        "other_names_en": None,
        "previous_names": [],
        "aliases": [],
        "email": "a@b.com",
        "br_number": None,
        "hkid_partial": None,
        "passport_partial": None,
        "passport_issuing_country_region": None,
        "tcsp_licence": None,
    }


def test_the_address_stores_the_four_boxes_for_hk(monkeypatch: pytest.MonkeyPatch) -> None:
    """§2.5: ``country`` is omitted for an HK-only address, never ``null``."""
    store: dict[str, object] = {}
    monkeypatch.setattr(
        officers, "create_appointment", _record_calls(store, appointment_view())
    )
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)

    body = {**APPOINTMENT_BODY, "correspondence_address": {**ADDRESS, "country": None}}
    assert client.post(OFFICERS_URL, json=body).status_code == 201
    assert store["correspondence_address"] == ADDRESS


def test_listing_appointments_passes_as_of_and_carries_persons(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store: dict[str, object] = {}
    monkeypatch.setattr(
        officers, "list_appointments", _record_calls(store, [appointment_view()])
    )
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)

    response = client.get(OFFICERS_URL, params={"as_of": "2026-10-01"})

    assert response.status_code == 200
    assert store["as_of"] == date(2026, 10, 1)
    assert response.json()[0]["person"]["id"] == str(PERSON_ID)
    # Without the parameter the read returns every row (as_of None).
    store.clear()
    assert client.get(OFFICERS_URL).status_code == 200
    assert store["as_of"] is None


def test_updating_an_appointment_states_only_the_fields_sent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store: dict[str, object] = {}
    monkeypatch.setattr(
        officers, "update_appointment", _record_calls(store, appointment_view())
    )
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)

    response = client.patch(
        APPOINTMENT_URL, json={"valid_to": "2026-11-01", "correspondence_address": None}
    )

    assert response.status_code == 200, response.text
    assert store["changes"] == {"valid_to": date(2026, 11, 1), "correspondence_address": None}
    assert store["appointment_id"] == APPOINTMENT_ID


def test_deleting_an_appointment_returns_204(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake(*args: object, **kwargs: object) -> None:
        return None

    monkeypatch.setattr(officers, "delete_appointment", fake)
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)

    assert client.delete(APPOINTMENT_URL).status_code == 204


def test_a_shareholding_round_trips_money_as_strings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store: dict[str, object] = {}
    monkeypatch.setattr(
        officers, "create_shareholding", _record_calls(store, shareholding_view())
    )
    monkeypatch.setattr(
        officers, "list_shareholdings", _record_calls({}, [shareholding_view()])
    )
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)

    response = client.post(SHAREHOLDINGS_URL, json=SHAREHOLDING_BODY)
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["percentage_held"] == "10.00"
    assert body["amount_paid"] == "1000.00"
    assert body["amount_unpaid"] == "0.00"
    assert body["shares_held"] == 1000
    assert body["allotment_date"] == "2020-01-01"
    assert body["address"] == ADDRESS
    assert body["person"]["id"] == str(PERSON_ID)
    # The strings cross unchanged: the application parses them, the route does not.
    assert store["percentage_held"] == "10.00"
    assert store["amount_paid"] == "1000.00"
    assert store["amount_unpaid"] == "0.00"

    listing = client.get(SHAREHOLDINGS_URL)
    assert listing.status_code == 200
    assert listing.json()[0]["amount_paid"] == "1000.00"


def test_updating_a_shareholding_states_only_the_fields_sent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store: dict[str, object] = {}
    monkeypatch.setattr(
        officers, "update_shareholding", _record_calls(store, shareholding_view())
    )
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)

    response = client.patch(SHAREHOLDING_URL, json={"amount_paid": "250.50"})

    assert response.status_code == 200, response.text
    assert store["changes"] == {"amount_paid": "250.50"}
    assert store["shareholding_id"] == SHAREHOLDING_ID


def test_deleting_a_shareholding_returns_204(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake(*args: object, **kwargs: object) -> None:
        return None

    monkeypatch.setattr(officers, "delete_shareholding", fake)
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)

    assert client.delete(SHAREHOLDING_URL).status_code == 204


def test_ids_must_be_uuids() -> None:
    client = build_client(actor=ADMIN, context=ADMIN_CONTEXT)
    assert client.get("/api/v1/officers/not-a-uuid").status_code == 405
    assert client.patch("/api/v1/officers/not-a-uuid", json={"role": "director"}).status_code == 422
    assert client.delete("/api/v1/shareholdings/not-a-uuid").status_code == 422
