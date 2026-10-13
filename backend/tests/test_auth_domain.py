"""Unit tests for the password/session-token primitives (``app.domain.auth``).

Pure domain code, no database: hash/verify round trips, malformed stored
hashes failing closed, and session-token digest properties.
"""

from app.domain.auth import (
    SESSION_TTL,
    hash_password,
    new_session_token,
    session_token_digest,
    verify_password,
)

PASSWORD = "correct horse battery staple"


# --- password hashing ---


def test_hash_verify_round_trip() -> None:
    assert verify_password(PASSWORD, hash_password(PASSWORD)) is True


def test_wrong_password_is_rejected() -> None:
    assert verify_password("wrong password", hash_password(PASSWORD)) is False


def test_same_password_hashes_differently_each_time() -> None:
    # A random salt per hash: identical passwords never produce identical rows.
    assert hash_password(PASSWORD) != hash_password(PASSWORD)


def test_hash_format_carries_algorithm_and_parameters() -> None:
    # scrypt$<n>$<r>$<p>$<salt_hex>$<hash_hex> — parameters travel with the
    # hash so a future cost increase still verifies older rows.
    parts = hash_password(PASSWORD).split("$")
    assert parts[0] == "scrypt"
    assert len(parts) == 6
    assert all(part for part in parts[1:])


def test_malformed_stored_hashes_fail_closed() -> None:
    for stored in (
        None,
        "",
        "not-a-hash",
        "scrypt$1$2$3",
        "scrypt$abc$8$1$00$00",
        "bcrypt$2b$10$deadbeef",
        f"scrypt${2**30}$8$1$00$00",  # absurd cost parameter
        "scrypt$16384$8$1$zz$00",  # non-hex salt
    ):
        assert verify_password(PASSWORD, stored) is False


# --- session tokens ---


def test_session_tokens_are_unique() -> None:
    assert new_session_token() != new_session_token()


def test_session_token_digest_is_deterministic_per_token() -> None:
    token = new_session_token()
    assert session_token_digest(token) == session_token_digest(token)


def test_session_token_digest_distinguishes_tokens() -> None:
    assert session_token_digest(new_session_token()) != session_token_digest(new_session_token())


def test_session_token_digest_shape() -> None:
    # SHA-256 hex: the raw token is never stored, only its digest.
    digest = session_token_digest(new_session_token())
    assert len(digest) == 64
    int(digest, 16)


def test_session_ttl_is_positive_and_bounded() -> None:
    assert SESSION_TTL.total_seconds() > 0
    assert SESSION_TTL.total_seconds() <= 24 * 3600
