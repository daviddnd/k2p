"""The deployment's half of the activation handshake (ADR-0016 point 1).

Four things live here, all pure:

- the **PKCE material** this side owns — the code verifier it generates and
  never lets go of, and the S256 challenge the vendor service stores
  (``control_plane/app/domain/activation.py`` has the vendor's half: the two
  deployables share no code by design, only the RFC 7636 wire rules, which
  ``code_challenge_for`` mirrors so the vendor's ``verify_pkce`` accepts what
  this deployment sends);
- the **deployment fingerprint** the vendor keys the tenant by — generated
  once and kept, because a changed fingerprint would register a second
  tenant and strand the entitlement on the first;
- the shape of the **local activation state**: the credential the control
  plane issued and the pending attempt between ``POST /entitlement/activation``
  and the browser's return;
- the **response parsers** for the two handshake calls that hand this side
  something it must not misread — a request id/expiry, and the credential.

Pure and framework-free per ADR-0006: stdlib hashing and randomness only.
"""

import base64
import hashlib
import secrets
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID

# RFC 7636 §4.1: the verifier is 43-128 unreserved characters. 32 random
# bytes encode to exactly 43 base64url characters, all within that set.
_VERIFIER_BYTES = 32

# The vendor's ``deployment_fingerprint`` column takes 255 characters; a
# 24-byte token is 32 characters and unguessable, which is all it needs to
# be — it identifies a deployment, it does not authenticate one.
_FINGERPRINT_BYTES = 24


class ActivationResponseInvalid(Exception):
    """A handshake response is not the shape the wire contract fixes.

    ``reason`` names the field, never its value: the body is the vendor's,
    and a refusal is recorded (and, for the administrator, rendered) without
    echoing what arrived.
    """

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class ActivationCredential:
    """The credential the control plane issued to this deployment.

    ``token`` is the secret (ADR-0016 point 3, ADR-0014 custody): it is
    written to the deployment's credential file, never to the database, never
    to a log, and never into an audit entry. ``control_plane_tenant_id`` is
    the vendor's tenant — a different id space from this deployment's own
    ``tenants`` rows, which is why the two are never mixed.
    """

    token: str = field(repr=False)
    token_id: UUID
    control_plane_tenant_id: UUID
    expires_at: datetime


@dataclass(frozen=True)
class PendingActivation:
    """An activation an administrator started that the browser has not finished.

    ``verifier`` is the PKCE secret: it is what makes the one-time code the
    vendor hands the browser useless to anyone who steals it. The two local
    ids record who to attribute the completed activation to — the callback
    itself is unauthenticated by necessity, so the attribution comes from
    here, not from the request.
    """

    request_id: UUID
    verifier: str = field(repr=False)
    local_tenant_id: UUID
    local_user_id: UUID
    expires_at: datetime

    def is_expired(self, *, now: datetime) -> bool:
        return now > self.expires_at


@dataclass(frozen=True)
class ActivationState:
    """This deployment's local activation state, as one file holds it.

    An empty state (``ActivationState()``) is a deployment that has never
    activated: no fingerprint, no credential, no attempt. Deactivation drops
    the credential and the attempt and keeps the fingerprint — the deployment
    is still the same deployment to the vendor, and a re-activation has to
    land on the same tenant.
    """

    deployment_fingerprint: str | None = None
    credential: ActivationCredential | None = None
    pending: PendingActivation | None = None

    @property
    def is_activated(self) -> bool:
        return self.credential is not None


def new_code_verifier() -> str:
    """A fresh PKCE code verifier (RFC 7636 §4.1)."""
    return secrets.token_urlsafe(_VERIFIER_BYTES)


def code_challenge_for(verifier: str) -> str:
    """The S256 challenge for a verifier (RFC 7636 §4.2)."""
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def new_deployment_fingerprint() -> str:
    """A fresh deployment fingerprint, generated on a deployment's first activation."""
    return secrets.token_urlsafe(_FINGERPRINT_BYTES)


def fingerprint_for(state: ActivationState) -> str:
    """The deployment fingerprint to register, minted on a deployment's first activation.

    Stable once minted: the vendor keys the tenant by it, so a regenerated
    fingerprint would strand the entitlement on a tenant no deployment
    presents any more.
    """
    if state.deployment_fingerprint is not None:
        return state.deployment_fingerprint
    return new_deployment_fingerprint()


@dataclass(frozen=True)
class ActivationRequestResult:
    """``POST /activation/requests`` answered — one of two shapes.

    Browser path: ``activated`` false with the pending ``request_id`` and its
    ``expires_at`` (the historical shape). Silent path (ADR-0016 amendment
    2026-10-11): ``activated`` true with the ``credential`` inline — the same
    field set the token exchange returns, so the deployment activates without
    ever opening a browser.
    """

    activated: bool
    request_id: UUID | None = None
    expires_at: datetime | None = None
    credential: ActivationCredential | None = None


def parse_activation_request_response(raw: Mapping[str, object]) -> ActivationRequestResult:
    """The discriminator read of ``POST /activation/requests``."""
    if raw.get("activated") is True:
        return ActivationRequestResult(
            activated=True, credential=parse_activation_token_response(raw)
        )
    return ActivationRequestResult(
        activated=False,
        request_id=_required_uuid(raw, "id"),
        expires_at=_required_timestamp(raw, "expires_at"),
    )


def parse_activation_token_response(raw: Mapping[str, object]) -> ActivationCredential:
    """The credential from ``POST /activation/token``.

    This is the one time the plaintext token exists on this side; it goes
    straight to the credential file (``app.adapters.entitlement.activation_store``).
    """
    token = raw.get("token")
    if not isinstance(token, str) or not token:
        raise ActivationResponseInvalid("missing_field:token")
    return ActivationCredential(
        token=token,
        token_id=_required_uuid(raw, "token_id"),
        control_plane_tenant_id=_required_uuid(raw, "tenant_id"),
        expires_at=_required_timestamp(raw, "expires_at"),
    )


def _required_uuid(raw: Mapping[str, object], field: str) -> UUID:
    value = raw.get(field)
    if not isinstance(value, str):
        raise ActivationResponseInvalid(f"missing_field:{field}")
    try:
        return UUID(value)
    except ValueError:
        raise ActivationResponseInvalid(f"invalid_field:{field}") from None


def _required_timestamp(raw: Mapping[str, object], field: str) -> datetime:
    value = raw.get(field)
    if not isinstance(value, str):
        raise ActivationResponseInvalid(f"missing_field:{field}")
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        raise ActivationResponseInvalid(f"invalid_field:{field}") from None
