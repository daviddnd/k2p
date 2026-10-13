"""The login-throttle domain's rulebook made executable (slice ②, 2026-10-11).

Pins the bounded exponential backoff schedule, the decay window, the
success reset, the ledger cap, and the named refusal — every clock is
injected, no I/O. The API layer's contract tests (burst, success path)
live in ``test_api_login_throttle.py``.
"""

import pytest

from app.domain.login_throttle import (
    BASE_DELAY_SECONDS,
    DECAY_SECONDS,
    FAILURE_THRESHOLD,
    LEDGER_CAP,
    MAX_DELAY_SECONDS,
    TOO_MANY_ATTEMPTS,
    ThrottleLedger,
    required_delay,
)


class TestRequiredDelay:
    def test_below_threshold_no_delay(self) -> None:
        for count in range(FAILURE_THRESHOLD):
            assert required_delay(count) == 0.0

    def test_at_threshold_the_base_delay(self) -> None:
        assert required_delay(FAILURE_THRESHOLD) == BASE_DELAY_SECONDS

    def test_doubles_per_failure(self) -> None:
        assert required_delay(FAILURE_THRESHOLD + 1) == BASE_DELAY_SECONDS * 2
        assert required_delay(FAILURE_THRESHOLD + 2) == BASE_DELAY_SECONDS * 4

    def test_bounded_by_the_cap(self) -> None:
        assert required_delay(FAILURE_THRESHOLD + 20) == MAX_DELAY_SECONDS


class TestThrottleLedger:
    def test_fresh_ledger_is_allowed(self) -> None:
        assert ThrottleLedger().refusal_wait(now=1000.0) is None

    def test_below_threshold_always_allowed(self) -> None:
        ledger = ThrottleLedger()
        for _ in range(FAILURE_THRESHOLD - 1):
            ledger.record_failure(now=1000.0)
        assert ledger.refusal_wait(now=1000.0) is None

    def test_at_threshold_refuses_within_the_base_delay(self) -> None:
        ledger = ThrottleLedger()
        for _ in range(FAILURE_THRESHOLD):
            ledger.record_failure(now=1000.0)
        wait = ledger.refusal_wait(now=1000.5)
        assert wait is not None
        assert wait == pytest.approx(BASE_DELAY_SECONDS - 0.5)
        # The wait expired: the next attempt is allowed (and re-locks harder).
        assert ledger.refusal_wait(now=1000.0 + BASE_DELAY_SECONDS + 0.1) is None

    def test_each_further_failure_doubles_the_lock(self) -> None:
        ledger = ThrottleLedger()
        for _ in range(FAILURE_THRESHOLD + 1):
            ledger.record_failure(now=1000.0)
        wait = ledger.refusal_wait(now=1000.0)
        assert wait is not None
        assert wait == pytest.approx(BASE_DELAY_SECONDS * 2)

    def test_aged_failures_decay_off_the_ledger(self) -> None:
        ledger = ThrottleLedger()
        for _ in range(FAILURE_THRESHOLD):
            ledger.record_failure(now=1000.0)
        # One decay period later every failure aged out: allowed again.
        assert ledger.refusal_wait(now=1000.0 + DECAY_SECONDS + 1) is None
        ledger.record_failure(now=1000.0 + DECAY_SECONDS + 1)
        assert len(ledger.failures) == 1

    def test_partial_decay_shrinks_the_count(self) -> None:
        ledger = ThrottleLedger()
        for _ in range(FAILURE_THRESHOLD - 2):
            ledger.record_failure(now=1000.0)
        for _ in range(2):
            ledger.record_failure(now=2000.0)
        # The three oldest failures aged out; two remain — below the threshold.
        assert ledger.refusal_wait(now=2000.0) is None
        assert len(ledger.failures) == 2

    def test_success_clears_the_ledger(self) -> None:
        ledger = ThrottleLedger()
        for _ in range(FAILURE_THRESHOLD + 5):
            ledger.record_failure(now=1000.0)
        ledger.record_success()
        assert ledger.failures == []
        assert ledger.refusal_wait(now=1000.0) is None

    def test_ledger_is_capped(self) -> None:
        ledger = ThrottleLedger()
        for _ in range(LEDGER_CAP * 3):
            ledger.record_failure(now=1000.0)
        assert len(ledger.failures) == LEDGER_CAP

    def test_reading_the_wait_never_mutates(self) -> None:
        ledger = ThrottleLedger()
        for _ in range(FAILURE_THRESHOLD):
            ledger.record_failure(now=1000.0)
        before = list(ledger.failures)
        ledger.refusal_wait(now=1000.5)
        assert ledger.failures == before

    def test_named_refusal_is_stable(self) -> None:
        # The API layer spells the same string; one vocabulary owner.
        assert TOO_MANY_ATTEMPTS == "too_many_attempts"
