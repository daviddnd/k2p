"""API tests for the first-run read (hermetic — a fake session stands in).

The desktop launcher polls ``GET /api/v1/bootstrap-state`` to decide whether
to open the first-run wizard; the seam is the same setup-token guard as the
bootstrap write, so an armed deployment answers only a holder of the token.
"""

from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.adapters.persistence.session import get_session
from app.api.bootstrap import router as bootstrap_router
from app.core.config import settings


class _FakeSession:
    """Stands in for the database: the endpoint asks exactly one scalar."""

    def __init__(self, tenant_id):
        self._tenant_id = tenant_id

    async def scalar(self, _stmt):
        return self._tenant_id


def _client(monkeypatch: pytest.MonkeyPatch, tenant_id) -> TestClient:
    app = FastAPI()
    app.include_router(bootstrap_router)

    async def override_session() -> AsyncIterator[_FakeSession]:
        yield _FakeSession(tenant_id)

    app.dependency_overrides[get_session] = override_session
    return TestClient(app)


URL = "/api/v1/bootstrap-state"


def test_armed_guard_answers_only_a_matching_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "setup_token", "the-setup-token")
    client = _client(monkeypatch, None)

    missing = client.get(URL)
    assert missing.status_code == 401

    wrong = client.get(URL, headers={"X-Setup-Token": "not-it"})
    assert wrong.status_code == 401

    no_tenant = client.get(URL, headers={"X-Setup-Token": "the-setup-token"})
    assert no_tenant.status_code == 200
    assert no_tenant.json() == {"tenant_exists": False}

    with_tenant = _client(monkeypatch, uuid4()).get(
        URL, headers={"X-Setup-Token": "the-setup-token"}
    )
    assert with_tenant.status_code == 200
    assert with_tenant.json() == {"tenant_exists": True}


def test_disarmed_guard_answers_without_a_header(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "setup_token", "")
    response = _client(monkeypatch, None).get(URL)
    assert response.status_code == 200
    assert response.json() == {"tenant_exists": False}
