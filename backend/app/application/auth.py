"""Staff login and the TOTP-MFA lifecycle: credential verification, session
issue, session resolution, enrollment, challenge, and the R5 recovery.

Services flush but never commit — the API session dependency commits at the
request edge (see ``app.application.identity``).

The MFA rulebook this module pins:

- **Enrollment is possession-proved.** ``begin_mfa_enrollment`` generates
  the secret and returns it (with the otpauth URI) — the ONLY moment the
  plaintext exists, the only value the caller ever holds. Nothing is
  persisted at that step; enrollment completes only when a code verifies
  against that same secret (the caller hands it back to
  ``complete_mfa_enrollment``), which proves the device holds the key before
  anything is sealed. Completing over an existing enrollment refuses
  (``mfa_already_enrolled``): re-enrollment is the R5 clear followed by a
  fresh begin, never an overwrite.
- **The sealed secret is the only stored form.** On completion the secret
  is sealed via the D1 seam (``encrypt_for_tenant``,
  ``KeyPurpose.totp_secret``); the column never holds plaintext and the
  secret never appears in an audit entry, an exception, or a log line.
- **The login flow.** After password verification, the session is issued
  with ``mfa_verified`` named explicitly through the domain rule
  (the column's server default stays on the column permanently —
  migration 0031 — and no insert relies on it). Administrators get
  ``False`` — an enrolled one until the challenge passes, an unenrolled one
  in the forced-enrollment state; General staff get ``False`` once enrolled
  (slice ①, 2026-10-11 — optional staff MFA) and ``True`` while unenrolled.
  The response vocabulary (``mfa_required`` / ``enrollment_state``) is the
  API layer's mapping of exactly these facts.
- **The challenge.** ``verify_mfa_challenge`` opens the sealed secret,
  verifies the code at the current step against the replay watermark
  (``users.totp_last_step``), and on success marks the presented session
  verified and persists the matched step — both ride the caller's session,
  so they commit or roll back with the request. A wrong, malformed, or
  replayed code is one named refusal (``totp_invalid``) and nothing is
  written; the fixed comparison work in ``app.domain.mfa.verify_totp``
  bounds any timing difference. Failed challenges are deliberately NOT
  audited: an attacker feeding codes would otherwise get to write to the
  compliance chain, and the uniform-failure property means an entry could
  say nothing about why the attempt failed.
- **The R5 recovery.** ``clear_mfa`` is the operator procedure for a lost
  device: the caller must be an Administrator (asserted here through the
  single RBAC decision, and enforced again at the route in E4), the target
  must be enrolled (else ``mfa_not_enrolled``, nothing written), and the
  one ``mfa_cleared`` entry names the operator as actor and the target by
  id — never by secret.
"""

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.persistence.models import AuthSession, User
from app.application import audit
from app.application.encryption import decrypt_for_tenant, encrypt_for_tenant
from app.application.rbac import load_actor_context
from app.domain.audit import AuditAction
from app.domain.auth import (
    SESSION_TTL,
    new_session_token,
    session_token_digest,
    verify_password,
)
from app.domain.encryption import KeyPurpose
from app.domain.mfa import (
    ALREADY_ENROLLED,
    CODE_INVALID,
    NOT_ENROLLED,
    current_step,
    new_totp_secret,
    otpauth_uri,
    session_verified_at_login,
    verify_totp,
)
from app.domain.rbac import is_admin

#: The named refusal when a non-administrator calls the recovery clear —
#: the same ``forbidden`` vocabulary the API layer uses everywhere else.
ADMIN_REQUIRED = "forbidden"


async def login(
    session: AsyncSession, *, email: str, password: str
) -> tuple[str, User, AuthSession] | None:
    """Verify credentials and issue a session; ``None`` for any failure.

    Every failure path (unknown email, inactive account, missing or wrong
    credentials) returns the same ``None`` after exactly one scrypt pass, so
    neither the response nor its timing distinguishes them. The session
    inherits ``user_id`` and ``tenant_id`` from the matched user row, so a
    session can never point across tenants by construction. ``mfa_verified``
    is named on every insert through the domain rule
    (``app.domain.mfa.session_verified_at_login``): the column's server
    default (migration 0031) keeps pre-MFA sessions valid, but an account
    owes the TOTP step exactly when the rule says so — Administrators always
    (enrolled → challenge first, unenrolled → forced enrollment), General
    staff once enrolled (slice ①, 2026-10-11) — and everyone else signs
    straight in with ``True``.
    """
    user = await session.scalar(select(User).where(User.email == email).limit(1))
    stored_hash = user.password_hash if user is not None and user.is_active else None
    # Always exactly one scrypt pass, then one combined decision — no
    # short-circuit that would skip the work for unknown emails.
    password_ok = verify_password(password, stored_hash)
    if user is None or not password_ok:
        return None
    token = new_session_token()
    auth_session = AuthSession(
        user_id=user.id,
        tenant_id=user.tenant_id,
        token_digest=session_token_digest(token),
        expires_at=datetime.now(UTC) + SESSION_TTL,
        mfa_verified=session_verified_at_login(
            role=user.role, enrolled=user.totp_secret is not None
        ),
    )
    session.add(auth_session)
    await session.flush()
    await audit.record_event(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.STAFF_LOGIN,
        actor_user_id=user.id,
        entity_type="user",
        entity_id=user.id,
    )
    return token, user, auth_session


async def resolve_session(session: AsyncSession, *, token: str) -> AuthSession | None:
    """Resolve a presented session token; unknown or expired → ``None``."""
    auth_session = await session.scalar(
        select(AuthSession).where(AuthSession.token_digest == session_token_digest(token))
    )
    if auth_session is None or auth_session.expires_at <= datetime.now(UTC):
        return None
    return auth_session


async def begin_mfa_enrollment(session: AsyncSession, *, user: User) -> tuple[str, str]:
    """Generate the TOTP secret and its otpauth URI; persist nothing.

    The return is the only moment the plaintext secret exists: the caller
    (E4's enrollment route) renders it once for the authenticator app and
    holds it until ``complete_mfa_enrollment`` hands it back with the
    confirming code. ``otpauth_uri`` receives only the fresh
    ``new_totp_secret()`` shape this function generates, by contract.
    """
    secret = new_totp_secret()
    return secret, otpauth_uri(issuer="K2P", account=user.email, secret=secret)


async def complete_mfa_enrollment(
    session: AsyncSession, *, user: User, secret: str, code: str, now: datetime
) -> None:
    """Complete enrollment when ``code`` verifies against ``secret``.

    ``secret`` is the value ``begin_mfa_enrollment`` returned — the caller
    holds it between the two steps and hands it back with the confirming
    code; nothing secret-shaped is persisted or logged along the way. There
    is no watermark yet (the first verify takes ``last_used_step=None``), so
    a code in the current window proves possession. On success the secret is
    sealed under ``KeyPurpose.totp_secret``, ``totp_enrolled_at`` is stamped,
    the watermark starts cleared, and one ``mfa_enrolled`` entry records the
    actor — never the secret. Any refusal (already enrolled, or a
    wrong/malformed code) raises the named refusal and writes nothing.
    """
    if user.totp_secret is not None:
        raise ValueError(ALREADY_ENROLLED)
    matched = verify_totp(secret, code, at_step=current_step(now.timestamp()))
    if matched is None:
        raise ValueError(CODE_INVALID)
    user.totp_secret = await encrypt_for_tenant(
        session, purpose=KeyPurpose.TOTP_SECRET, plaintext=secret
    )
    user.totp_enrolled_at = now
    user.totp_last_step = None
    await session.flush()
    await audit.record_event(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.MFA_ENROLLED,
        actor_user_id=user.id,
        entity_type="user",
        entity_id=user.id,
    )


async def verify_mfa_challenge(
    session: AsyncSession, *, user: User, code: str, now: datetime, auth_session: AuthSession
) -> None:
    """Verify a login-challenge code; on success mark the session verified.

    The sealed secret is opened through the D1 seam, the code is checked at
    the current step with the persisted replay watermark, and on success the
    presented session's ``mfa_verified`` becomes ``True`` and the matched
    step becomes the new ``totp_last_step`` — both ride the caller's
    session. A wrong, malformed, or replayed code is the one named refusal
    (``totp_invalid``) and nothing is written; a challenge against an
    unenrolled account refuses closed (``mfa_not_enrolled``). Failed
    challenges are deliberately not audited (see the module docstring).
    """
    if user.totp_secret is None:
        raise ValueError(NOT_ENROLLED)
    secret = await decrypt_for_tenant(
        session, purpose=KeyPurpose.TOTP_SECRET, envelope=user.totp_secret
    )
    matched = verify_totp(
        secret,
        code,
        at_step=current_step(now.timestamp()),
        last_used_step=user.totp_last_step,
    )
    if matched is None:
        raise ValueError(CODE_INVALID)
    auth_session.mfa_verified = True
    user.totp_last_step = matched
    await session.flush()


async def clear_mfa(session: AsyncSession, *, target_user: User, actor: User) -> None:
    """The R5 recovery: clear the target's enrollment; one ``mfa_cleared`` entry.

    The caller must be an Administrator — asserted here through the single
    RBAC decision (``load_actor_context`` + ``is_admin``) and enforced again
    at the route in E4. The target must be enrolled, else ``mfa_not_enrolled``
    and nothing is written. The three enrollment columns are cleared together
    and the audit entry names the operator as actor and the target by id —
    never by secret.
    """
    context = await load_actor_context(session, user_id=actor.id, tenant_id=actor.tenant_id)
    if context is None or not is_admin(context):
        raise ValueError(ADMIN_REQUIRED)
    if target_user.totp_secret is None:
        raise ValueError(NOT_ENROLLED)
    target_user.totp_secret = None
    target_user.totp_enrolled_at = None
    target_user.totp_last_step = None
    await session.flush()
    await audit.record_event(
        session,
        tenant_id=target_user.tenant_id,
        action=AuditAction.MFA_CLEARED,
        actor_user_id=actor.id,
        entity_type="user",
        entity_id=target_user.id,
        details={"target_user_id": target_user.id},
    )
