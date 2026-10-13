"""TOTP (RFC 6238) primitives for staff MFA — the domain rulebook.

Stdlib only (``hmac``, ``hashlib``, ``secrets``, ``base64``, ``struct``) —
no new external dependency (AGENTS.md), the hand-rolled-protocol precedent
of the ClamAV INSTREAM client and ``domain/auth.py``'s scrypt work. Pure
and deterministic per ADR-0006, aside from the deliberate randomness in
secret generation; **no clock reads** — every function takes its step or
its time in, and ``current_step`` is pure arithmetic on a caller-supplied
Unix timestamp.

The rulebook this module pins:

- **Parameters** — SHA-1, a 30-second step, 6 digits, and 20-byte
  base32 secrets (RFC 4226/6238's default SHA-1 key size). The window is
  the caller's ``at_step ± 1``: a code one step in either direction is
  accepted, nothing further.
- **Reference vectors** — ``tests/test_mfa_domain.py`` carries RFC 6238
  appendix B's SHA-1 vectors (the 20-byte ``"12345678901234567890"`` key)
  truncated to this module's 6 digits; the 6-digit code is the last six
  digits of appendix B's 8-digit value, so the vectors pin the exact HOTP
  arithmetic, not just the acceptance window.
- **Replay containment** — ``verify_totp`` returns the step the code
  matched so the caller persists it (``users.totp_last_step``); a code
  whose matched step is at or below the persisted watermark is refused
  even inside the window. Persisting the watermark is E3's job; this
  module only states the rule.
- **Constant time** — the presented code is compared with
  ``hmac.compare_digest`` against every candidate in the window; a
  refused code's shape never leaks *which* comparison ran.
- **Fail-closed vocabulary** — malformed secrets raise ``ValueError``
  with the named refusal ``SECRET_MALFORMED`` (follow
  ``domain/auth.py``'s fail-closed style: nothing is coerced, defaulted,
  or retried). A malformed or wrong code — empty, not six ASCII digits,
  outside the window — is simply ``None``: refusal by absence, the same
  shape a wrong code gets, so the response reveals nothing about *why*.
  The ASCII-digit rule is pinned by a regex, not ``str.isdigit``, whose
  non-ASCII digits would reach ``hmac.compare_digest`` and escape as a
  ``TypeError`` instead of the uniform ``None``. One more refusal exists
  beside those: ``totp_at`` raises ``ValueError("totp_step_negative")``
  on a negative step — a programmer-error guard, not attacker input
  (``verify_totp`` never generates a negative candidate), and named here
  so the vocabulary stays closed and documented.
"""

import base64
import binascii
import hashlib
import hmac
import re
import secrets
import struct
from urllib.parse import quote

from app.domain.rbac import Role

#: The named refusal for a secret that is not this module's base32 form.
SECRET_MALFORMED = "totp_secret_malformed"

#: The named refusal for a presented code that does not verify — wrong,
#: malformed, or a replayed step: one name for every cause, so the caller
#: cannot distinguish them (the uniform-failure property; E3's application
#: layer raises this name, the API maps it later).
CODE_INVALID = "totp_invalid"

#: The named refusal when an operation requires an enrollment the user does
#: not have (challenge/complete against an unenrolled account, or clearing
#: an account with nothing to clear).
NOT_ENROLLED = "mfa_not_enrolled"

#: The named refusal when completing enrollment over an existing enrollment
#: — re-enrollment is the R5 clear followed by a fresh begin, never an
#: overwrite.
ALREADY_ENROLLED = "mfa_already_enrolled"

#: A presented code must be exactly this: six ASCII digits. ``str.isdigit``
#: is not enough — it admits non-ASCII digits (Arabic-Indic and friends)
#: that ``hmac.compare_digest`` then refuses with TypeError, which would
#: escape the uniform ``None`` refusal.
_CODE = re.compile(r"[0-9]{6}")

_SECRET_BYTES = 20
_STEP_SECONDS = 30
_DIGITS = 6
_MODULUS = 10**_DIGITS
_WINDOW = 1


def session_verified_at_login(*, role: str, enrolled: bool) -> bool:
    """The ``mfa_verified`` flag a freshly issued session carries.

    Administrators always owe the TOTP step — an enrolled one until the
    challenge passes, an unenrolled one in forced enrollment. General staff
    owe it only once enrolled (slice ①, 2026-10-11: optional staff MFA — a
    staff member binds an authenticator from the settings surface, and every
    sign-in from then issues an unverified session until the challenge
    passes); an unenrolled General account signs straight in.
    """
    return role != Role.ADMINISTRATOR.value and not enrolled


def new_totp_secret() -> str:
    """A fresh TOTP secret: 20 random bytes, base32-encoded, no padding."""
    return base64.b32encode(secrets.token_bytes(_SECRET_BYTES)).decode("ascii")


def current_step(now: int | float) -> int:
    """The TOTP step a Unix timestamp (seconds) falls in — pure arithmetic."""
    return int(now) // _STEP_SECONDS


def totp_at(secret: str, step: int) -> str:
    """The 6-digit TOTP for ``secret`` at ``step`` (RFC 6238, SHA-1).

    ``step`` is the RFC's T: whole 30-second intervals since the Unix
    epoch. A malformed secret refuses with ``SECRET_MALFORMED``.
    """
    if step < 0:
        raise ValueError("totp_step_negative")
    return _hotp(_decode_secret(secret), step)


def verify_totp(
    secret: str,
    code: str,
    *,
    at_step: int,
    last_used_step: int | None = None,
) -> int | None:
    """Check ``code`` in the ``at_step ± 1`` window; return the matched step.

    The matched step is the value the caller persists as the replay
    watermark: on success it is strictly greater than
    ``last_used_step`` by construction. ``None`` covers every refusal —
    malformed or wrong code, outside the window, or a replayed step — so
    the caller cannot distinguish causes. A malformed secret refuses with
    ``SECRET_MALFORMED``, same as ``totp_at``.
    """
    key = _decode_secret(secret)
    if _CODE.fullmatch(code) is None:
        return None
    for step in range(at_step - _WINDOW, at_step + _WINDOW + 1):
        if step < 0:
            continue
        if last_used_step is not None and step <= last_used_step:
            continue
        if hmac.compare_digest(_hotp(key, step), code):
            return step
    return None


def otpauth_uri(*, issuer: str, account: str, secret: str) -> str:
    """The ``otpauth://totp/`` URI an authenticator app scans (E3 renders it).

    The Google Authenticator convention: the issuer appears once in the
    path (``issuer:account``) and once as a parameter, URL-encoded both
    times; the algorithm/digits/period parameters pin this module's
    parameters so an app cannot drift onto a different profile.

    ``secret`` is rendered as-is and deliberately not validated here —
    the caller must pass a ``new_totp_secret()``-shaped value (E3's
    enrollment path guarantees this); validating would make rendering
    able to refuse, which is not this function's job.
    """
    label = quote(f"{issuer}:{account}", safe="")
    return (
        f"otpauth://totp/{label}?secret={secret}"
        f"&issuer={quote(issuer, safe='')}"
        f"&algorithm=SHA1&digits={_DIGITS}&period={_STEP_SECONDS}"
    )


def _hotp(key: bytes, step: int) -> str:
    """One 6-digit HOTP value (RFC 4226 dynamic truncation, HMAC-SHA-1)."""
    digest = hmac.new(key, struct.pack(">Q", step), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(value % _MODULUS).zfill(_DIGITS)


def _decode_secret(secret: str) -> bytes:
    """This module's base32 secret as bytes; anything else refuses, closed.

    Accepts the unpadded form ``new_totp_secret`` produces (padding is
    re-derived, so both spellings of the same key decode identically) and
    requires exactly 20 decoded bytes — the size this module generates.
    """
    if not secret:
        raise ValueError(SECRET_MALFORMED)
    normalized = secret.strip().upper()
    try:
        key = base64.b32decode(normalized + "=" * (-len(normalized) % 8), casefold=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError(SECRET_MALFORMED) from exc
    if len(key) != _SECRET_BYTES:
        raise ValueError(SECRET_MALFORMED)
    return key
