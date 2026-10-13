"""Password hashing and session-token primitives for staff login.

Stdlib cryptography only (``hashlib.scrypt``, ``hmac``, ``secrets``) — no new
external dependency, which would need sign-off (AGENTS.md). Pure and
deterministic per ADR-0006, aside from the deliberate randomness in salt and
token generation.

Stored hash format: ``scrypt$<n>$<r>$<p>$<salt_hex>$<hash_hex>`` — the cost
parameters travel with the hash, so a future cost increase still verifies
rows hashed with the old ones. Session tokens are opaque random strings;
only their SHA-256 digest is ever stored, so a database leak does not leak
live tokens.
"""

import hashlib
import hmac
import secrets
from datetime import timedelta

# scrypt cost parameters for interactive logins (RFC 7914's recommended
# baseline); ~16 MiB memory and ~100 ms per attempt, which also bounds
# online guessing. maxmem is explicit because some OpenSSL builds default
# to a lower limit than these parameters need.
_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_MAXMEM = 64 * 1024 * 1024
_DKLEN = 32
_SALT_LEN = 16

# Sanity bounds for parameters parsed out of a stored hash; anything outside
# is treated as malformed (fail closed) so a tampered row cannot trigger an
# absurd memory/CPU allocation.
_MAX_N = 2**20
_MAX_R_P = 32

#: Lifetime of a staff login session from issue.
SESSION_TTL = timedelta(hours=12)


def hash_password(password: str) -> str:
    """Hash ``password`` with a fresh random salt."""
    salt = secrets.token_bytes(_SALT_LEN)
    derived = _scrypt(password, salt, _SCRYPT_N, _SCRYPT_R, _SCRYPT_P)
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${salt.hex()}${derived.hex()}"


def verify_password(password: str, stored_hash: str | None) -> bool:
    """Check ``password`` against a stored hash; anything malformed → False.

    A missing or malformed stored hash still runs one scrypt pass against a
    fixed dummy salt, so the response time does not reveal whether the
    account has credentials.
    """
    parsed = _parse(stored_hash)
    if parsed is None:
        n, r, p, salt, expected = _SCRYPT_N, _SCRYPT_R, _SCRYPT_P, bytes(_SALT_LEN), bytes(_DKLEN)
    else:
        n, r, p, salt, expected = parsed
    candidate = _scrypt(password, salt, n, r, p)
    return hmac.compare_digest(candidate, expected)


def new_session_token() -> str:
    """A new opaque session token (presented as a Bearer credential)."""
    return secrets.token_urlsafe(32)


def session_token_digest(token: str) -> str:
    """The storable SHA-256 digest of a session token (hex)."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _scrypt(password: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=_DKLEN, maxmem=_SCRYPT_MAXMEM
    )


def _parse(stored_hash: str | None) -> tuple[int, int, int, bytes, bytes] | None:
    if stored_hash is None:
        return None
    parts = stored_hash.split("$")
    if len(parts) != 6 or parts[0] != "scrypt":
        return None
    try:
        n, r, p = (int(part) for part in parts[1:4])
        salt = bytes.fromhex(parts[4])
        expected = bytes.fromhex(parts[5])
    except ValueError:
        return None
    if not (1 < n <= _MAX_N and 1 <= r <= _MAX_R_P and 1 <= p <= _MAX_R_P):
        return None
    if len(salt) == 0 or len(expected) != _DKLEN:
        return None
    return n, r, p, salt, expected
