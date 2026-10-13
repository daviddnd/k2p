"""Domain tests for the deployment's activation primitives (ADR-0016 point 1).

Pure and database-free: the PKCE material this side owns, the deployment
fingerprint's stability, the expiry of a pending attempt, and the parsers
that refuse an unusable handshake response instead of half-reading it.
"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from app.domain.activation import (
    ActivationCredential,
    ActivationResponseInvalid,
    ActivationState,
    PendingActivation,
    code_challenge_for,
    fingerprint_for,
    new_code_verifier,
    new_deployment_fingerprint,
    parse_activation_request_response,
    parse_activation_token_response,
)

NOW = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)


def credential() -> ActivationCredential:
    return ActivationCredential(
        token="issued-token",
        token_id=uuid4(),
        control_plane_tenant_id=uuid4(),
        expires_at=NOW + timedelta(days=90),
    )


# --- PKCE material ---


def test_code_challenge_matches_rfc_7636_appendix_b() -> None:
    # The RFC's own worked example: a challenge this side computes is a
    # challenge the vendor's verify_pkce accepts.
    assert (
        code_challenge_for("dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk")
        == "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
    )


def test_a_new_verifier_is_spec_shaped_and_unique() -> None:
    first = new_code_verifier()
    second = new_code_verifier()

    assert len(first) == 43  # RFC 7636 §4.1 minimum, and the shape this side sends
    assert set(first) <= set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")
    assert first != second


# --- Deployment fingerprint ---


def test_fingerprint_is_minted_once_and_then_reused() -> None:
    minted = fingerprint_for(ActivationState())
    assert minted
    assert fingerprint_for(ActivationState(deployment_fingerprint=minted)) == minted


def test_a_fresh_fingerprint_is_unguessable_and_vendor_sized() -> None:
    assert 1 <= len(new_deployment_fingerprint()) <= 255


# --- Pending attempt lifetime ---


def test_pending_attempt_expires_exactly_at_its_expiry() -> None:
    pending = PendingActivation(
        request_id=uuid4(),
        verifier="v" * 43,
        local_tenant_id=uuid4(),
        local_user_id=uuid4(),
        expires_at=NOW,
    )

    assert pending.is_expired(now=NOW - timedelta(seconds=1)) is False
    assert pending.is_expired(now=NOW) is False
    assert pending.is_expired(now=NOW + timedelta(seconds=1)) is True


# --- Handshake response parsing ---


def test_registration_response_parses_the_request_and_its_expiry() -> None:
    result = parse_activation_request_response(
        {"id": str(uuid4()), "expires_at": NOW.isoformat()}
    )
    assert result.activated is False
    assert result.request_id is not None
    assert result.expires_at == NOW
    assert result.credential is None


def test_registration_response_parses_the_silent_activation() -> None:
    """The 2026-10-11 amendment's discriminator: a valid registration code
    answers ``activated: true`` with the credential inline — the same field
    set the token exchange has always returned."""
    tenant_id = uuid4()
    result = parse_activation_request_response(
        {
            "activated": True,
            "token": "the-plaintext-token",
            "token_id": str(uuid4()),
            "tenant_id": str(tenant_id),
            "expires_at": NOW.isoformat(),
        }
    )
    assert result.activated is True
    assert result.request_id is None
    assert result.credential is not None
    assert result.credential.token == "the-plaintext-token"
    assert result.credential.control_plane_tenant_id == tenant_id
    assert result.credential.expires_at == NOW


@pytest.mark.parametrize(
    "body",
    [
        {"expires_at": NOW.isoformat()},
        {"id": "not-a-uuid", "expires_at": NOW.isoformat()},
        {"id": str(uuid4())},
        {"id": str(uuid4()), "expires_at": "not-a-timestamp"},
        {"id": 17, "expires_at": NOW.isoformat()},
        # Silent answers must carry the whole credential, like the exchange.
        {"activated": True, "expires_at": NOW.isoformat()},
        {"activated": True, "token": "t", "expires_at": NOW.isoformat()},
    ],
)
def test_an_unusable_registration_response_is_refused(body: dict[str, object]) -> None:
    with pytest.raises(ActivationResponseInvalid):
        parse_activation_request_response(body)


def test_token_response_parses_the_credential() -> None:
    body = {
        "token": "issued-token",
        "token_id": str(uuid4()),
        "tenant_id": str(uuid4()),
        "expires_at": (NOW + timedelta(days=90)).isoformat(),
    }
    parsed = parse_activation_token_response(body)

    assert parsed.token == "issued-token"
    assert parsed.control_plane_tenant_id is not None


@pytest.mark.parametrize(
    "body",
    [
        {"token_id": str(uuid4()), "tenant_id": str(uuid4()), "expires_at": NOW.isoformat()},
        {
            "token": "",
            "token_id": str(uuid4()),
            "tenant_id": str(uuid4()),
            "expires_at": NOW.isoformat(),
        },
        {
            "token": "t",
            "token_id": "nope",
            "tenant_id": str(uuid4()),
            "expires_at": NOW.isoformat(),
        },
        {"token": "t", "token_id": str(uuid4()), "tenant_id": str(uuid4())},
    ],
)
def test_an_unusable_token_response_is_refused(body: dict[str, object]) -> None:
    with pytest.raises(ActivationResponseInvalid):
        parse_activation_token_response(body)


def test_the_parsed_credential_never_shows_its_token_in_a_repr() -> None:
    # A credential travels through tracebacks and debug logs; the fingerprint
    # of a leak is a token in a repr. Both secrets — the activation token and
    # the PKCE verifier — are kept out of it.
    state = ActivationState(
        credential=credential(),
        pending=PendingActivation(
            request_id=uuid4(),
            verifier="the-pkce-verifier",
            local_tenant_id=uuid4(),
            local_user_id=uuid4(),
            expires_at=NOW,
        ),
    )
    rendered = repr(state)

    assert "issued-token" not in rendered
    assert "the-pkce-verifier" not in rendered
    assert state.is_activated is True
