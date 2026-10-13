"""The TOTP domain's rulebook made executable (phase 7, task E1).

Pins the RFC 6238 appendix B SHA-1 vectors exactly, the ±1-step window
edges, the replay-containment watermark, the fail-closed vocabulary, and
the otpauth URI shape. Pure stdlib — no clock, no I/O.
"""

import base64
import re

import pytest

from app.domain.mfa import (
    SECRET_MALFORMED,
    current_step,
    new_totp_secret,
    otpauth_uri,
    session_verified_at_login,
    totp_at,
    verify_totp,
)

# RFC 6238 appendix B's SHA-1 test vectors with the standard 20-byte ASCII
# key "12345678901234567890": (Unix time in seconds, T — the counter the
# time hashes to, the expected code). Appendix B prints 8-digit codes;
# this module emits 6, and the 6-digit code is the last six digits of the
# 8-digit value, so the vectors pin the exact HOTP arithmetic, not just
# the acceptance window. The test also pins T = time // 30, the step
# arithmetic ``current_step`` owns.
_RFC_6238_SHA1_VECTORS = [
    (59, 0x1, "287082"),
    (1111111109, 0x23523EC, "081804"),
    (1111111111, 0x23523ED, "050471"),
    (1234567890, 0x273EF07, "005924"),
    (2000000000, 66666666, "279037"),
    (20000000000, 666666666, "353130"),
]
_SECRET_BYTES = b"12345678901234567890"


@pytest.fixture
def secret() -> str:
    return base64.b32encode(_SECRET_BYTES).decode("ascii").rstrip("=")


class TestRfc6238Vectors:
    @pytest.mark.parametrize(("time_sec", "step", "expected"), _RFC_6238_SHA1_VECTORS)
    def test_vector_matches_exactly(
        self, secret: str, time_sec: int, step: int, expected: str
    ) -> None:
        assert current_step(time_sec) == step
        assert totp_at(secret, step) == expected


class TestNewSecret:
    def test_is_32_base32_chars_no_padding(self) -> None:
        for _ in range(50):
            value = new_totp_secret()
            assert len(value) == 32
            assert re.fullmatch(r"[A-Z2-7]+", value) is not None

    def test_round_trips_to_twenty_bytes(self) -> None:
        raw = base64.b32decode(new_totp_secret())
        assert len(raw) == 20

    def test_known_vector_key(self, secret: str) -> None:
        # The fixture secret decodes to appendix B's key.
        assert base64.b32decode(secret) == _SECRET_BYTES


class TestWindow:
    def test_each_of_three_window_steps_accepted(self, secret: str) -> None:
        for step in (999, 1000, 1001):
            assert verify_totp(secret, totp_at(secret, step), at_step=1000) == step

    def test_outside_window_refused(self, secret: str) -> None:
        for step in (998, 1002):
            assert (
                verify_totp(secret, totp_at(secret, step), at_step=1000) is None
            )


class TestReplay:
    def test_matched_step_at_or_below_watermark_refused(self, secret: str) -> None:
        code = totp_at(secret, 1000)
        assert verify_totp(secret, code, at_step=1000, last_used_step=1000) is None
        assert verify_totp(secret, code, at_step=1000, last_used_step=1001) is None

    def test_fresh_step_above_watermark_accepted(self, secret: str) -> None:
        code = totp_at(secret, 1002)
        assert (
            verify_totp(secret, code, at_step=1002, last_used_step=1001) == 1002
        )

    def test_no_watermark_accepts(self, secret: str) -> None:
        assert verify_totp(secret, totp_at(secret, 7), at_step=7, last_used_step=None) == 7


class TestFailClosed:
    @pytest.mark.parametrize(
        "bad_secret",
        ["", "not-base32!", "ABC", "JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXPX"],
    )
    def test_malformed_secret_refused(self, bad_secret: str) -> None:
        with pytest.raises(ValueError) as exc:
            totp_at(bad_secret, 1000)
        assert exc.value.args[0] == SECRET_MALFORMED

    def test_malformed_secret_verify_refused(self) -> None:
        with pytest.raises(ValueError) as exc:
            verify_totp("!!!", "123456", at_step=1000)
        assert exc.value.args[0] == SECRET_MALFORMED

    @pytest.mark.parametrize("bad_code", ["", "12345", "1234567", "12a456"])
    def test_malformed_code_refused(self, secret: str, bad_code: str) -> None:
        assert verify_totp(secret, bad_code, at_step=1000) is None

    def test_unicode_digit_code_refused_not_raised(self, secret: str) -> None:
        # Arabic-Indic digits are isdigit()-True but not ASCII; they must
        # hit the uniform None refusal, not escape as a compare_digest
        # TypeError (review fix: ASCII-only code validation).
        assert verify_totp(secret, "١٢٣٤٥٦", at_step=1000) is None


class TestOtpauthUri:
    def test_shape(self, secret: str) -> None:
        uri = otpauth_uri(issuer="K2P (Test)", account="admin@example.com", secret=secret)
        assert uri == (
            "otpauth://totp/K2P%20%28Test%29%3Aadmin%40example.com"
            f"?secret={secret}&issuer=K2P%20%28Test%29&algorithm=SHA1&digits=6&period=30"
        )

    def test_issuer_appears_in_path_and_parameter(self, secret: str) -> None:
        uri = otpauth_uri(issuer="Acme", account="ops@acme.hk", secret=secret)
        assert uri.startswith("otpauth://totp/Acme%3Aops%40acme.hk?")
        assert "&issuer=Acme&" in uri


class TestCurrentStep:
    def test_pure_arithmetic(self, secret: str) -> None:
        assert current_step(0) == 0
        assert current_step(29) == 0
        assert current_step(30) == 1
        assert current_step(59_999_999_999_999) == 1_999_999_999_999


class TestSessionVerifiedAtLogin:
    """The ``mfa_verified`` flag a freshly issued session carries (slice ①,
    2026-10-11 — optional staff MFA): Administrators always owe the TOTP
    step; General staff owe it only once enrolled."""

    def test_administrator_owes_the_step_enrolled_or_not(self) -> None:
        assert session_verified_at_login(role="administrator", enrolled=True) is False
        assert session_verified_at_login(role="administrator", enrolled=False) is False

    def test_general_staff_owe_it_only_once_enrolled(self) -> None:
        assert session_verified_at_login(role="general", enrolled=True) is False
        assert session_verified_at_login(role="general", enrolled=False) is True
