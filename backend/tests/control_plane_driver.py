"""Drives the real control plane with the real lease client, in one process.

Run as a subprocess by ``test_control_plane_handshake_integration_db.py``.
The two deployables cannot share an interpreter: both name their package
``app``, and whichever is imported first claims the name — in the backend's
own test process that is the backend, so the vendor service would be
unreachable by import. A fresh process holds both real artifacts at once
instead: the vendor service imported normally (here *it* is ``app``), and the
execution plane's lease client loaded straight from its file — which is
possible only because that module deliberately imports nothing from
``app.*`` (see its docstring).

Everything travels over ASGI in this process: no socket, no network, and no
second implementation of either side. The flow is the whole handshake:

1. the deployment's own client registers an activation request (real PKCE
   verifier, real deployment fingerprint);
2. the "browser" follows the vendor's authorize redirect to Google, which is
   the fake identity provider the vendor's own test suite uses;
3. the browser comes back to the vendor's callback and is bounced to the
   deployment's redirect URI with the one-time code;
4. the client redeems the code with its verifier and reads the lease.

The result is printed as JSON on stdout. The credential is never printed —
only its length, which is enough for the caller to see that a token came back.

Environment: ``AICS_CP_DIR`` (the ``control_plane`` directory),
``AICS_LEASE_CLIENT`` (path to ``control_plane_client.py``), and whatever
``AICS_CP_*`` configuration the vendor service needs — the database URL above
all, which the service reads the same way it does in production.
"""

import asyncio
import base64
import hashlib
import importlib.util
import json
import os
import sys
from pathlib import Path
from types import ModuleType
from urllib.parse import parse_qs, urlsplit

REDIRECT_URI = "http://127.0.0.1:8000/api/v1/entitlement/activation/callback"
CONTROL_PLANE_URL = "http://control-plane.test"
FINGERPRINT = "integration-deployment-fingerprint"


def _import_control_plane() -> None:
    """Put the vendor service on the path — before anything imports ``app``."""
    sys.path.insert(0, str(Path(os.environ["AICS_CP_DIR"]).resolve()))


def _load_lease_client() -> ModuleType:
    """Load the execution plane's client from its file, as its own module."""
    spec = importlib.util.spec_from_file_location(
        "aics_lease_client", Path(os.environ["AICS_LEASE_CLIENT"]).resolve()
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _pkce() -> tuple[str, str]:
    """A verifier and its S256 challenge (RFC 7636); the client sends both apart."""
    verifier = base64.urlsafe_b64encode(os.urandom(32)).rstrip(b"=").decode("ascii")
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return verifier, base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


async def _run() -> dict[str, object]:
    _import_control_plane()
    import httpx
    from app.adapters.google_oidc import GoogleOidcClient
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

    from app.adapters.persistence.session import get_session
    from app.api.deps import get_oidc_client
    from app.core.config import settings as control_plane_settings
    from app.main import create_app
    from tests.fake_google_idp import (
        AUTHORIZATION_ENDPOINT,
        TOKEN_ENDPOINT,
        USERINFO_ENDPOINT,
        FakeGoogleIdP,
    )

    lease_client = _load_lease_client()

    # Provision the service the way its own suite does — except for the
    # database, which comes from the environment through its real settings.
    identity_provider = FakeGoogleIdP()
    control_plane_settings.google_authorization_endpoint = AUTHORIZATION_ENDPOINT
    control_plane_settings.google_token_endpoint = TOKEN_ENDPOINT
    control_plane_settings.google_userinfo_endpoint = USERINFO_ENDPOINT
    control_plane_settings.google_redirect_uri = (
        f"{CONTROL_PLANE_URL}/api/v1/auth/google/callback"
    )
    control_plane_settings.google_client_id = "driver-client-id"
    control_plane_settings.google_client_secret = "driver-client-secret"
    control_plane_settings.state_secret = "driver-state-secret"
    control_plane_settings.vendor_admin_key = "driver-vendor-key"

    engine = create_async_engine(control_plane_settings.database_url)
    try:
        async with engine.connect() as connection:
            await connection.begin()
            async with AsyncSession(bind=connection, expire_on_commit=False) as session:
                app = create_app()

                async def override_session():  # type: ignore[no-untyped-def]
                    yield session

                app.dependency_overrides[get_session] = override_session
                app.dependency_overrides[get_oidc_client] = lambda: GoogleOidcClient(
                    transport=httpx.ASGITransport(app=identity_provider.app)
                )
                transport = httpx.ASGITransport(app=app)
                deployment = lease_client.ControlPlaneClient(
                    base_url=CONTROL_PLANE_URL, timeout=5.0, transport=transport
                )
                async with httpx.AsyncClient(
                    transport=transport, base_url=CONTROL_PLANE_URL, follow_redirects=False
                ) as browser:
                    result = await _handshake(
                        deployment, browser, identity_provider
                    )
            await connection.rollback()
    finally:
        await engine.dispose()
    return result


async def _silent_handshake(deployment) -> dict[str, object]:  # type: ignore[no-untyped-def]
    """The 2026-10-11 amendment across the real pair: a registration code
    activates without any browser — the vendor answers the credential in the
    registration call itself, and the lease reads on the fresh token."""
    signup = await deployment._request(
        "POST",
        "/api/v1/registrations",
        json_body={
            "legal_name": "Silent TCSP Limited",
            "contact_email": "silent@tcsp.example",
        },
    )
    code = signup["registration_code"]
    _, challenge = _pkce()
    registration = await deployment.register_activation(
        redirect_uri=REDIRECT_URI,
        code_challenge=challenge,
        deployment_fingerprint=FINGERPRINT,
        registration_code=code,
    )
    if registration.get("activated") is not True:
        raise AssertionError(f"expected a silent activation, got: {registration!r}")
    lease = await deployment.fetch_lease(token=registration["token"])
    return {
        "silent": True,
        "tenant_id": registration["tenant_id"],
        "token_length": len(registration["token"]),
        "lease": lease,
    }


async def _handshake(deployment, browser, identity_provider) -> dict[str, object]:  # type: ignore[no-untyped-def]
    if os.environ.get("AICS_HANDSHAKE_MODE") == "silent":
        return await _silent_handshake(deployment)
    verifier, challenge = _pkce()

    registration = await deployment.register_activation(
        redirect_uri=REDIRECT_URI,
        code_challenge=challenge,
        deployment_fingerprint=FINGERPRINT,
    )
    request_id = registration["id"]

    authorized = await browser.get(f"/api/v1/authorize?request_id={request_id}")
    assert authorized.status_code == 302, authorized.text
    state = parse_qs(urlsplit(authorized.headers["location"]).query)["state"][0]

    google_code = identity_provider.issue_code(
        subject="integration-admin", email="admin@tcsp.example"
    )
    bounced = await browser.get(
        f"/api/v1/auth/google/callback?code={google_code}&state={state}"
    )
    assert bounced.status_code == 302, bounced.text
    code = parse_qs(urlsplit(bounced.headers["location"]).query)["code"][0]

    credential = await deployment.exchange_activation_code(
        code=code, code_verifier=verifier, deployment_fingerprint=FINGERPRINT
    )
    lease = await deployment.fetch_lease(token=credential["token"])

    return {
        "tenant_id": credential["tenant_id"],
        "token_id": credential["token_id"],
        "token_length": len(credential["token"]),
        "lease": lease,
    }


def main() -> int:
    try:
        result = asyncio.run(_run())
    except Exception as exc:
        print(f"{exc.__class__.__name__}: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
