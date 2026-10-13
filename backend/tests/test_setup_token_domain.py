"""Hermetic tests for the bootstrap guard decision (``app.domain.setup``).

The guard is opt-in: disarmed it must allow everything (the pre-guard
behaviour every existing flow relies on); armed it must allow only the exact
token, compared in constant time as bytes.
"""

from app.domain.setup import setup_token_allows

TOKEN = "correct-horse-setup-token"


def test_disarmed_allows_with_or_without_a_presented_token() -> None:
    assert setup_token_allows(configured="", presented=None) is True
    assert setup_token_allows(configured="", presented="anything") is True


def test_armed_allows_the_exact_token() -> None:
    assert setup_token_allows(configured=TOKEN, presented=TOKEN) is True


def test_armed_refuses_a_wrong_token() -> None:
    assert setup_token_allows(configured=TOKEN, presented=TOKEN + "x") is False
    assert setup_token_allows(configured=TOKEN, presented="") is False


def test_armed_refuses_a_missing_token() -> None:
    assert setup_token_allows(configured=TOKEN, presented=None) is False


def test_armed_refuses_a_non_ascii_token_without_raising() -> None:
    # A header can carry non-ASCII bytes; the comparison must refuse, not raise.
    assert setup_token_allows(configured=TOKEN, presented="密碼-pässwörd") is False
