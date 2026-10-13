"""A faithful stand-in for the vendor control plane's wire contract.

The shapes here are copied from the service's own API models —
``control_plane/app/api/activation.py`` (registration, code exchange),
``.../api/entitlement.py`` (the frozen lease payload), and
``.../api/commands.py`` (the command queue, poll, and result endpoints),
``.../api/packs.py`` (the pack download) — and
the PKCE check is the same S256 verification
``control_plane/app/domain/activation.py`` performs. A client that satisfies
this fake therefore satisfies the real service; the other direction, the real
client against the real service, is
``test_control_plane_handshake_integration_db.py`` for the handshake and
``test_command_envelope_integration_db.py`` for the command channel.

No socket: the fake is a small ASGI app driven through
``httpx.ASGITransport``, so the suite stays hermetic. Its levers
(``lease_body``, ``delay_seconds``, ``request_ttl_seconds``,
``commands_status``, ``result_status``, ``registration_codes``, …) exist to
stage the cases the execution plane has to survive — a refused lease, a
malformed body, a slow or absent service, a report that fails after a
command has already run, a claim code the vendor never issued.
"""

import asyncio
import base64
import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

# The real service's bounds (``control_plane/app/api/activation.py``).
S256_CHALLENGE_LENGTH = 43
MAX_FINGERPRINT_LENGTH = 255
MAX_REGISTRATION_CODE_LENGTH = 512

_BASE64URL = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")
_UNRESERVED = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")


class UnreachableTransport(httpx.AsyncBaseTransport):
    """A transport that fails the way a dead host does — without a socket."""

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("control plane is down", request=request)


@dataclass
class _PendingRequest:
    id: UUID
    redirect_uri: str
    code_challenge: str
    deployment_fingerprint: str
    expires_at: datetime
    code: str | None = None
    consumed: bool = False
    # What the deployment presented with the request: the claim code, and
    # whether the field was in the body at all (absent and null are not the
    # same on the wire, and the absent form is the pre-registration one).
    registration_code: str | None = None
    registration_code_present: bool = False


@dataclass
class FakeControlPlane:
    """The vendor service's three endpoints, as one mutable object."""

    tier: str = "standard"
    seat_cap: int | None = None
    company_cap: int | None = None
    entitlement_version: int = 1
    lease_days: int = 30
    tenant_status: str = "active"
    tenant_id: UUID = field(default_factory=uuid4)
    request_ttl_seconds: float = 600.0

    # Levers for the cases the execution plane must survive.
    lease_body: dict[str, Any] | None = None
    lease_status: int = 200
    delay_seconds: float = 0.0

    # The command channel (contract §5.2): what this fake has queued, what it
    # was told, and the levers that stage an outage in the middle of a cycle.
    commands_status: int = 200
    result_status: int = 200
    command_envelope_version: int = 1
    commands: list[dict[str, Any]] = field(default_factory=list)
    results: list[dict[str, Any]] = field(default_factory=list)

    # The pack channel (contract §2.2): the catalog this fake has published,
    # every release it was asked for, and the levers that stage a refusal
    # (``packs_status``) or a body that does not hash to its digest
    # (``pack_digest_overrides``).
    packs_status: int = 200
    pack_releases: dict[str, dict[str, Any]] = field(default_factory=dict)
    pack_digest_overrides: dict[str, str] = field(default_factory=dict)
    pack_fetches: list[tuple[str, int | None]] = field(default_factory=list)

    # What the fake was asked to do (so a test can assert a client did not
    # re-ask, or did).
    lease_fetches: int = 0
    registrations: int = 0
    token_exchanges: int = 0
    command_polls: int = 0
    result_posts: int = 0
    reported_versions: list[int] = field(default_factory=list)

    # The self-service signup's claim codes (CP4a), as the real service
    # treats them: issued by ``issue_registration_code``, then bound to the
    # first deployment fingerprint that presents them — idempotent from that
    # deployment, refused (``registration_code_used``) from any other.
    registration_codes: dict[str, str | None] = field(default_factory=dict)

    requests: dict[str, _PendingRequest] = field(default_factory=dict)
    tokens: dict[str, datetime] = field(default_factory=dict)
    app: FastAPI = field(init=False)

    def __post_init__(self) -> None:
        self.app = _build_app(self)

    def issue_registration_code(self) -> str:
        """Mint a claim code, the way the vendor's self-service signup does."""
        code = secrets.token_urlsafe(24)
        self.registration_codes[code] = None
        return code

    def enqueue(self, command: str = "ping", **envelope: Any) -> dict[str, Any]:
        """Put an envelope in the fake's queue, as the vendor would."""
        document: dict[str, Any] = {
            "envelope_version": self.command_envelope_version,
            "command_id": str(uuid4()),
            "command": command,
            "params": {"local_record_refs": {}},
            "expected_outputs": [],
            "issued_at": datetime.now(UTC).isoformat(),
        }
        document.update(envelope)
        self.commands.append(document)
        return document

    def publish_pack(
        self,
        pack_type: str,
        payload: dict[str, Any],
        *,
        version: int = 1,
        signature: str | None = None,
    ) -> dict[str, Any]:
        """Publish a release, computing the digest the way the real service does.

        The canonical form (sorted keys, no whitespace, escaped non-ASCII) is
        the wire contract; the real control plane's definition lives in
        ``control_plane/app/domain/packs.py`` and the execution plane's in
        ``backend/app/domain/packs.py``. This mirror is independent of both,
        which is what makes "the digest a client check passes" meaningful —
        and the cross-deployable suite checks the real pair rather than this.
        """
        document: dict[str, Any] = {
            "pack_type": pack_type,
            "version": version,
            "sha256": _payload_sha256(payload),
            "payload": payload,
            "published_at": datetime.now(UTC).isoformat(),
            "published_by": "vendor_ops",
            "signature": signature,
        }
        self.pack_releases[pack_type] = document
        return document

    def unreported(self) -> list[dict[str, Any]]:
        """The envelopes this fake would hand over: everything unanswered."""
        answered = {result["command_id"] for result in self.results}
        return [item for item in self.commands if str(item["command_id"]) not in answered]

    def complete_handshake(self, request_id: UUID) -> str:
        """Mint the one-time code the browser would bring back."""
        request = self.requests[str(request_id)]
        request.code = secrets.token_urlsafe(24)
        return request.code

    def lease_payload(self) -> dict[str, Any]:
        """The lease body as the real service renders it (frozen §2.2 shape)."""
        if self.lease_body is not None:
            return self.lease_body
        now = datetime.now(UTC)
        return {
            "tier": self.tier,
            "license_expires_at": (now + timedelta(days=self.lease_days)).isoformat(),
            "seat_cap": self.seat_cap,
            "company_cap": self.company_cap,
            "entitlement_version": self.entitlement_version,
            "issued_at": now.isoformat(),
            "signature": None,
        }


def _build_app(control_plane: FakeControlPlane) -> FastAPI:
    app = FastAPI()

    @app.post("/api/v1/activation/requests")
    async def register(payload: dict[str, Any]) -> JSONResponse:
        if control_plane.delay_seconds:
            await asyncio.sleep(control_plane.delay_seconds)
        redirect_uri = payload.get("redirect_uri")
        challenge = payload.get("code_challenge")
        fingerprint = payload.get("deployment_fingerprint")
        registration_code = payload.get("registration_code")
        if not _wellformed_redirect_uri(redirect_uri):
            return JSONResponse({"detail": "invalid_redirect_uri"}, status_code=422)
        if not _wellformed_challenge(challenge):
            return JSONResponse({"detail": "invalid_code_challenge"}, status_code=422)
        if not isinstance(fingerprint, str) or not 1 <= len(fingerprint) <= MAX_FINGERPRINT_LENGTH:
            return JSONResponse({"detail": "invalid_fingerprint"}, status_code=422)
        if registration_code is not None:
            if (
                not isinstance(registration_code, str)
                or not 1 <= len(registration_code) <= MAX_REGISTRATION_CODE_LENGTH
            ):
                return JSONResponse({"detail": "invalid_registration_code"}, status_code=422)
            # The real service's claim rules (control_plane/app/application/
            # registration.py): unknown codes refuse, a used code is refused
            # from any deployment but the one that claimed it, and a refusal
            # aborts the request before it exists.
            if registration_code not in control_plane.registration_codes:
                return JSONResponse({"detail": "registration_code_invalid"}, status_code=400)
            claimed_by = control_plane.registration_codes[registration_code]
            if claimed_by is not None and claimed_by != fingerprint:
                return JSONResponse({"detail": "registration_code_used"}, status_code=400)
            control_plane.registration_codes[registration_code] = fingerprint
            # The 2026-10-11 amendment, mirrored: a valid code IS the
            # authorization — the token answers directly, no request pending.
            token = secrets.token_urlsafe(32)
            control_plane.tokens[token] = datetime.now(UTC) + timedelta(days=30)
            return JSONResponse(
                {
                    "activated": True,
                    "token": token,
                    "token_id": str(uuid4()),
                    "tenant_id": str(control_plane.tenant_id),
                    "expires_at": control_plane.tokens[token].isoformat(),
                },
                status_code=201,
            )
        pending = _PendingRequest(
            id=uuid4(),
            redirect_uri=redirect_uri,
            code_challenge=challenge,
            deployment_fingerprint=fingerprint,
            expires_at=datetime.now(UTC) + timedelta(seconds=control_plane.request_ttl_seconds),
            registration_code=registration_code,
            registration_code_present="registration_code" in payload,
        )
        control_plane.requests[str(pending.id)] = pending
        control_plane.registrations += 1
        return JSONResponse(
            {"id": str(pending.id), "expires_at": pending.expires_at.isoformat()}, status_code=201
        )

    @app.post("/api/v1/activation/token")
    async def exchange(payload: dict[str, Any]) -> JSONResponse:
        code = payload.get("code")
        verifier = payload.get("code_verifier")
        fingerprint = payload.get("deployment_fingerprint")
        if not isinstance(code, str) or not isinstance(verifier, str):
            return JSONResponse({"detail": "invalid_code"}, status_code=422)
        pending = next(
            (item for item in control_plane.requests.values() if item.code == code), None
        )
        if pending is None:
            return JSONResponse({"detail": "invalid_code"}, status_code=400)
        if pending.consumed:
            return JSONResponse({"detail": "code_consumed"}, status_code=400)
        if datetime.now(UTC) > pending.expires_at:
            return JSONResponse({"detail": "code_expired"}, status_code=400)
        if pending.deployment_fingerprint != fingerprint:
            return JSONResponse({"detail": "fingerprint_mismatch"}, status_code=400)
        if not _verifier_matches(verifier=verifier, challenge=pending.code_challenge):
            # The real service leaves a code usable after a failed attempt.
            return JSONResponse({"detail": "invalid_verifier"}, status_code=400)
        pending.consumed = True
        token = secrets.token_urlsafe(32)
        now = datetime.now(UTC)
        control_plane.tokens[token] = now + timedelta(days=90)
        control_plane.token_exchanges += 1
        return JSONResponse(
            {
                "token": token,
                "token_id": str(uuid4()),
                "tenant_id": str(control_plane.tenant_id),
                "expires_at": (now + timedelta(days=90)).isoformat(),
            }
        )

    @app.get("/api/v1/entitlement")
    async def lease(request: Request) -> JSONResponse:
        if control_plane.delay_seconds:
            await asyncio.sleep(control_plane.delay_seconds)
        token = _bearer(request.headers.get("authorization"))
        expires_at = control_plane.tokens.get(token) if token else None
        if expires_at is None or datetime.now(UTC) > expires_at:
            return JSONResponse({"detail": "invalid activation token"}, status_code=401)
        if control_plane.tenant_status != "active":
            return JSONResponse(
                {"detail": f"tenant_{control_plane.tenant_status}"}, status_code=403
            )
        control_plane.lease_fetches += 1
        if control_plane.lease_status != 200:
            return JSONResponse({"detail": "refused"}, status_code=control_plane.lease_status)
        return JSONResponse(control_plane.lease_payload())

    @app.get("/api/v1/commands")
    async def commands_poll(request: Request) -> JSONResponse:
        if control_plane.delay_seconds:
            await asyncio.sleep(control_plane.delay_seconds)
        if not _authenticated(control_plane, request):
            return JSONResponse({"detail": "invalid activation token"}, status_code=401)
        declared = request.query_params.get("envelope_version")
        if declared is not None:
            control_plane.reported_versions.append(int(declared))
        control_plane.command_polls += 1
        if control_plane.commands_status != 200:
            return JSONResponse({"detail": "refused"}, status_code=control_plane.commands_status)
        # The real service hands back everything without an outcome, so a
        # worker that polls twice is re-delivered the same envelope.
        return JSONResponse({"commands": control_plane.unreported()})

    @app.post("/api/v1/commands/results")
    async def commands_result(request: Request, payload: dict[str, Any]) -> JSONResponse:
        if not _authenticated(control_plane, request):
            return JSONResponse({"detail": "invalid activation token"}, status_code=401)
        control_plane.result_posts += 1
        if control_plane.result_status != 200:
            return JSONResponse({"detail": "refused"}, status_code=control_plane.result_status)
        control_plane.results = [
            recorded
            for recorded in control_plane.results
            if recorded.get("command_id") != payload.get("command_id")
        ]
        control_plane.results.append(payload)
        return JSONResponse(payload)

    @app.get("/api/v1/packs/{pack_type}")
    async def pack(request: Request, pack_type: str) -> JSONResponse:
        """One release, or the refusal shape the real service sends.

        The real routes answer 403 with ``{"code": "entitlement_denied",
        "reason": …}`` for a tenant whose tier is Free and 404 with
        ``{"code": "pack_not_found", …}`` for a type it has never published;
        the lever ``packs_status`` stages the first (and any other refusal) so
        the client's reason handling is exercised.
        """
        if not _authenticated(control_plane, request):
            return JSONResponse({"detail": "invalid activation token"}, status_code=401)
        requested = request.query_params.get("version")
        version = int(requested) if requested is not None else None
        control_plane.pack_fetches.append((pack_type, version))
        if control_plane.packs_status != 200:
            return JSONResponse(
                {"detail": {"code": "entitlement_denied", "reason": "tier_not_licensed"}},
                status_code=control_plane.packs_status,
            )
        release = control_plane.pack_releases.get(pack_type)
        if release is None or (version is not None and release.get("version") != version):
            return JSONResponse(
                {
                    "detail": {
                        "code": "pack_not_found",
                        "reason": f"{pack_type}:{version if version is not None else 'latest'}",
                    }
                },
                status_code=404,
            )
        body = dict(release)
        override = control_plane.pack_digest_overrides.get(pack_type)
        if override is not None:
            body["sha256"] = override
        return JSONResponse(body)

    return app


def _payload_sha256(payload: dict[str, Any]) -> str:
    """The canonical digest of a payload (the pack channel's wire contract)."""
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _authenticated(control_plane: FakeControlPlane, request: Request) -> bool:
    token = _bearer(request.headers.get("authorization"))
    expires_at = control_plane.tokens.get(token) if token else None
    return expires_at is not None and datetime.now(UTC) <= expires_at


def _wellformed_redirect_uri(value: object) -> bool:
    if not isinstance(value, str) or not value:
        return False
    parts = urlsplit(value)
    return parts.scheme in ("http", "https") and bool(parts.netloc) and not parts.fragment


def _wellformed_challenge(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == S256_CHALLENGE_LENGTH
        and all(character in _BASE64URL for character in value)
    )


def _verifier_matches(*, verifier: str, challenge: str) -> bool:
    """The S256 check the real service performs (RFC 7636 §4.6, constant-time)."""
    if not 43 <= len(verifier) <= 128 or not all(c in _UNRESERVED for c in verifier):
        return False
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    computed = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return hmac.compare_digest(computed, challenge)


def _bearer(authorization: str | None) -> str | None:
    if authorization is None:
        return None
    scheme, _, token = authorization.partition(" ")
    return token.strip() if scheme.lower() == "bearer" and token.strip() else None
