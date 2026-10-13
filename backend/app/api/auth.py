"""Staff login: issues the Bearer session tokens ``get_current_actor`` accepts,
and the TOTP-MFA surface for staff (phase 7, scope §8; R5; slice ① opened
enrollment and the challenge to General staff, 2026-10-11).

Unauthenticated by design (like the tenant bootstrap seam). Every credential
failure returns the same 401 — the response never reveals whether the email
exists, the account is active, or the password was wrong. The response's MFA
fields map the session facts one-to-one: ``mfa_required`` is the negation of
the session's ``mfa_verified`` flag, and ``enrollment_state`` reads the
user's enrollment column.

The login throttle (slice ②, 2026-10-11; the refusal shape amended by the
founder ruling of 2026-10-12): failures are recorded per source and per
account through the single ``app.application.login_throttle`` seam, and an
attempt inside BOTH keys' bounded backoff is refused before any credential
work runs — 429 ``too_many_attempts`` with a ``Retry-After`` header, one
uniform body that never reveals whether the account exists (the named
refusal, decided at implementation over a silent uniform delay: a delay
would hold a worker per refused attempt, and the uniform 401 vocabulary
already exists only at the credential check). A single hot key never locks
anyone out: the attempt runs the check, and a correct password signs in
and clears both ledgers — otherwise a sustained poke every three minutes
could hold an account (or the office's shared source key) locked forever,
the victim's correct password refused before it was ever checked. The
pre-work refusal exists to bound work, not to lock accounts; an attacker
gains no guessing throughput from the pass-through, since the per-source
five-in-window bound is the same bound the check has always answered to. The source key is
the right-most ``X-Forwarded-For`` entry when the header is present — nginx
is the only off-host ingress (the compose file publishes uvicorn
loopback-only, for exactly this reason) and appends the observed peer —
else the transport peer (the loopback dev server). The account key is the
normalized email. A success clears both keys' ledgers; the same seam guards
the TOTP challenge (``/mfa/verify``), whose account key is the shared login
ledger.

The enrollment begin/complete, verify and status routes are open to every
active staff account (``require_active`` — an Administrator uses the same
surface for forced enrollment and the challenge; an enrolled General staff
member answers the login challenge on it; an unenrolled one binds an
authenticator from it, staff self-service since slice ①). The R5 recovery
``clear`` stays Administrator-only (``require_admin`` — the operator
procedure; the application layer's own admin assert is defense in depth).
The unverified-vs-verified distinction within the session is the gate plus
each route's own check. The unverified-session gate
(``deps.MFA_ALLOWED_PATH_PREFIXES``) allowlists exactly the enrollment
begin/complete, verify and status prefixes — nothing else is mounted under
them — while ``clear`` is deliberately NOT allowlisted: only a verified
session reaches the recovery, and an unverified session of either role is
refused the named 403 off the list. The shapes, pinned for the contract
record:

- ``POST /mfa/enrollment`` → 200 ``{secret, uri, enrollment_state: "pending"}``
  — the ONLY response that ever carries the plaintext secret.
- ``POST /mfa/enrollment/complete`` body ``{code, secret}`` (the caller-held
  secret from begin, handed back; nothing secret-shaped is persisted at
  begin) → 200 ``{enrollment_state: "enrolled"}``; a wrong, malformed or
  replayed code — or a mangled secret — is one named refusal,
  ``422 totp_invalid``; an already-enrolled account refuses
  ``409 mfa_already_enrolled`` (re-enrollment is the R5 clear, then a fresh
  begin).
- ``POST /mfa/verify`` body ``{code}`` (the login challenge; the presented
  session is the one marked verified) → 200
  ``{enrollment_state: "enrolled", "mfa_verified": true}``; a session already
  verified is refused ``409 mfa_not_required``; a wrong/malformed/replayed
  code is ``422 totp_invalid``; an unenrolled account ``409 mfa_not_enrolled``.
- ``GET /mfa/status`` → 200 ``{enrollment_state, mfa_verified}`` for the
  caller — the read the frontend's guard and enrollment screen route on.
- ``POST /mfa/clear`` body ``{user_id}`` → 200 ``{cleared: true}``; a target
  that is not enrolled refuses ``409 mfa_not_enrolled``; an unknown or
  cross-tenant target is ``404`` one-answer.

Every refusal above writes nothing and is named; the code bodies carry the
names verbatim (the house convention), except the gate's own 403, whose
structured body is E3's merged shape.
"""

import math
import time
from datetime import UTC, datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.persistence.models import AuthSession, User
from app.adapters.persistence.session import get_session
from app.api.deps import ActorRef, bearer_token, get_current_actor, require_active, require_admin
from app.api.identity import _EMAIL_PATTERN, UserResponse, _user_response
from app.application import auth, login_throttle
from app.application.auth import ADMIN_REQUIRED
from app.domain.login_throttle import TOO_MANY_ATTEMPTS
from app.domain.mfa import ALREADY_ENROLLED, CODE_INVALID, NOT_ENROLLED, SECRET_MALFORMED
from app.domain.rbac import ActorContext, Role

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


class LoginRequest(BaseModel):
    email: str = Field(min_length=3, max_length=320, pattern=_EMAIL_PATTERN)
    # No policy check at login — only that a password was presented.
    password: str = Field(min_length=1, max_length=128)


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_at: datetime
    user: UserResponse
    # The MFA facts the frontend routes on (phase 7, scope §8): whether the
    # session still owes a TOTP challenge / enrollment, and which of the two.
    mfa_required: bool
    enrollment_state: Literal["enrolled", "pending", "not_required"]


def _client_source(request: Request) -> str:
    """The throttle's source key: the forwarded-for tail, else the peer.

    nginx is the deployment's only off-host ingress — uvicorn is published
    loopback-only (`deploy/docker-compose.yml`) — and appends the observed
    peer to ``X-Forwarded-For``, so the right-most entry is the trustworthy
    one; a client-supplied left part is ignored for keying. A bare peer (no
    header — the loopback dev server, the test client) is the fallback.
    """
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded is not None:
        tail = forwarded.rsplit(",", 1)[-1].strip()
        if tail:
            return tail
    return request.client.host if request.client is not None else "unknown"


@router.post("/login")
async def login(
    payload: LoginRequest,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> LoginResponse:
    source = _client_source(request)
    account = payload.email.strip().lower()
    now = time.monotonic()
    wait = login_throttle.refusal_wait(source_key=source, account_key=account, now=now)
    if wait is not None:
        # Answered before any credential work: bounded cost per refusal,
        # and the uniform body never names the tripped key or the account.
        raise HTTPException(
            status_code=429,
            detail=TOO_MANY_ATTEMPTS,
            headers={"Retry-After": str(max(1, math.ceil(wait)))},
        )
    result = await auth.login(session, email=payload.email, password=payload.password)
    if result is None:
        login_throttle.record_failure(source_key=source, account_key=account, now=now)
        raise HTTPException(status_code=401, detail="invalid credentials")
    login_throttle.record_success(source_key=source, account_key=account)
    token, user, auth_session = result
    if auth_session.mfa_verified:
        enrollment_state: Literal["enrolled", "pending", "not_required"] = "not_required"
    else:
        enrollment_state = "enrolled" if user.totp_secret is not None else "pending"
    return LoginResponse(
        access_token=token,
        expires_at=auth_session.expires_at,
        user=_user_response(user),
        mfa_required=not auth_session.mfa_verified,
        enrollment_state=enrollment_state,
    )


# --- The TOTP-MFA surface (phase 7, scope §8; the shapes are pinned in the
# module docstring — H1 records them verbatim). --------------------------------

MfaEnrollmentState = Literal["enrolled", "pending", "not_required"]


class MfaEnrollmentBeginResponse(BaseModel):
    secret: str
    uri: str
    enrollment_state: Literal["pending"]


class MfaEnrollmentCompleteRequest(BaseModel):
    # No shape on `code`: a malformed code must reach the domain and take the
    # same named refusal as a wrong one (uniform failure), not a Pydantic 422.
    code: str = Field(max_length=16)
    secret: str = Field(max_length=64)


class MfaEnrollmentCompleteResponse(BaseModel):
    enrollment_state: Literal["enrolled"]


class MfaCodeRequest(BaseModel):
    code: str = Field(max_length=16)


class MfaVerifyResponse(BaseModel):
    enrollment_state: Literal["enrolled"]
    mfa_verified: bool


class MfaStatusResponse(BaseModel):
    enrollment_state: MfaEnrollmentState
    mfa_verified: bool


class MfaClearRequest(BaseModel):
    user_id: UUID


class MfaClearResponse(BaseModel):
    cleared: bool


async def _caller_user(session: AsyncSession, actor: ActorRef) -> User:
    """The caller's user row; a session whose user vanished fails closed 401."""
    user = await session.get(User, actor.user_id)
    if user is None:
        raise HTTPException(status_code=401, detail="not authenticated")
    return user


async def _presented_session(session: AsyncSession, authorization: str | None) -> AuthSession:
    """The session row behind this request's Bearer token.

    ``get_current_actor`` authenticated and gated the same token already;
    this re-resolution is how the verify/status routes get the row the
    challenge must mark verified. Unreachable past the gate; fails closed.
    """
    token = bearer_token(authorization)
    auth_session = await auth.resolve_session(session, token=token) if token else None
    if auth_session is None:
        raise HTTPException(status_code=401, detail="not authenticated")
    return auth_session


@router.post("/mfa/enrollment")
async def begin_mfa_enrollment(
    session: Annotated[AsyncSession, Depends(get_session)],
    _actor: Annotated[ActorRef, Depends(get_current_actor)],
    _context: Annotated[ActorContext, Depends(require_active())],
) -> MfaEnrollmentBeginResponse:
    """Begin enrollment: the only response ever carrying the plaintext secret."""
    user = await _caller_user(session, _actor)
    secret, uri = await auth.begin_mfa_enrollment(session, user=user)
    return MfaEnrollmentBeginResponse(secret=secret, uri=uri, enrollment_state="pending")


@router.post("/mfa/enrollment/complete")
async def complete_mfa_enrollment(
    payload: MfaEnrollmentCompleteRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    _actor: Annotated[ActorRef, Depends(get_current_actor)],
    _context: Annotated[ActorContext, Depends(require_active())],
) -> MfaEnrollmentCompleteResponse:
    """Seal the caller-held secret once the confirming code verifies."""
    user = await _caller_user(session, _actor)
    try:
        await auth.complete_mfa_enrollment(
            session,
            user=user,
            secret=payload.secret,
            code=payload.code,
            now=datetime.now(UTC),
        )
    except ValueError as exc:
        if str(exc) == ALREADY_ENROLLED:
            raise HTTPException(status_code=409, detail="mfa_already_enrolled") from None
        if str(exc) in (CODE_INVALID, SECRET_MALFORMED):
            # One named refusal for every "the code/secret pair does not
            # verify" cause; nothing is written.
            raise HTTPException(status_code=422, detail="totp_invalid") from None
        raise
    return MfaEnrollmentCompleteResponse(enrollment_state="enrolled")


@router.post("/mfa/verify")
async def verify_mfa_challenge(
    payload: MfaCodeRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    _actor: Annotated[ActorRef, Depends(get_current_actor)],
    _context: Annotated[ActorContext, Depends(require_active())],
    authorization: Annotated[str | None, Header()] = None,
) -> MfaVerifyResponse:
    """The login challenge; on success the presented session is verified.

    The challenge consults the login-throttle seam before any verification
    work (review fix pass, 2026-10-12): a wrong code costs one HMAC, not one
    scrypt, so an unthrottled challenge is online-brute-forceable against
    exactly the second factor's threat model. The refusal fires only when
    both the presented-session key and the shared account ledger are hot —
    a single hot key still runs the check, so a correct code passes and
    clears the ledger. The account key is the shared login ledger; the
    session key bounds one presented bearer session.
    """
    user = await _caller_user(session, _actor)
    presented = await _presented_session(session, authorization)
    if presented.mfa_verified:
        raise HTTPException(status_code=409, detail="mfa_not_required")
    source_key = f"verify:{presented.token_digest}"
    account_key = user.email.strip().lower()
    now = time.monotonic()
    wait = login_throttle.refusal_wait(
        source_key=source_key, account_key=account_key, now=now
    )
    if wait is not None:
        raise HTTPException(
            status_code=429,
            detail=TOO_MANY_ATTEMPTS,
            headers={"Retry-After": str(max(1, math.ceil(wait)))},
        )
    try:
        await auth.verify_mfa_challenge(
            session,
            user=user,
            code=payload.code,
            now=datetime.now(UTC),
            auth_session=presented,
        )
    except ValueError as exc:
        if str(exc) == CODE_INVALID:
            login_throttle.record_failure(
                source_key=source_key, account_key=account_key, now=now
            )
            raise HTTPException(status_code=422, detail="totp_invalid") from None
        if str(exc) == NOT_ENROLLED:
            raise HTTPException(status_code=409, detail="mfa_not_enrolled") from None
        raise
    login_throttle.record_success(source_key=source_key, account_key=account_key)
    return MfaVerifyResponse(enrollment_state="enrolled", mfa_verified=True)


@router.get("/mfa/status")
async def mfa_status(
    session: Annotated[AsyncSession, Depends(get_session)],
    _actor: Annotated[ActorRef, Depends(get_current_actor)],
    _context: Annotated[ActorContext, Depends(require_active())],
    authorization: Annotated[str | None, Header()] = None,
) -> MfaStatusResponse:
    """The caller's MFA facts — the read the frontend's guard routes on."""
    user = await _caller_user(session, _actor)
    presented = await _presented_session(session, authorization)
    enrollment_state: MfaEnrollmentState = (
        "enrolled"
        if user.totp_secret is not None
        else "pending"
        if user.role == Role.ADMINISTRATOR.value
        else "not_required"
    )
    return MfaStatusResponse(
        enrollment_state=enrollment_state, mfa_verified=presented.mfa_verified
    )


@router.post("/mfa/clear")
async def clear_mfa(
    payload: MfaClearRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[ActorRef, Depends(get_current_actor)],
    context: Annotated[ActorContext, Depends(require_admin())],
) -> MfaClearResponse:
    """The R5 recovery. Deliberately NOT in the gate's allowlist: an
    unverified Administrator is refused by the gate before this route runs;
    reaching it at all means the session is verified."""
    target = await session.get(User, payload.user_id)
    if target is None or target.tenant_id != actor.tenant_id:
        # One answer for unknown and cross-tenant: the caller learns nothing
        # about other tenants' accounts.
        raise HTTPException(status_code=404, detail="user not found")
    actor_user = await session.get(User, actor.user_id)
    if actor_user is None:
        raise HTTPException(status_code=403, detail="forbidden")
    try:
        await auth.clear_mfa(session, target_user=target, actor=actor_user)
    except ValueError as exc:
        if str(exc) == NOT_ENROLLED:
            raise HTTPException(status_code=409, detail="mfa_not_enrolled") from None
        if str(exc) == ADMIN_REQUIRED:
            raise HTTPException(status_code=403, detail="forbidden") from None
        raise
    return MfaClearResponse(cleared=True)
