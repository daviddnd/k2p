"""Login-failure throttle: bounded exponential backoff, per key (slice ②).

The pure rulebook for ``POST /api/v1/auth/login``'s rate limiting, per the
2026-10-11 auth-hardening slice. Stdlib only, no clock reads — every
function takes its time in (ADR-0006); the application layer supplies
``time.monotonic()`` and owns the process-wide ledger registry.

The rulebook this module pins:

- **Two keys, one refusal.** The application consults a per-source ledger
  (the client address) and a per-account ledger (the normalized email);
  either tripping refuses the attempt, and the refusal never says which.
  Both ledgers are written on every credential failure and cleared on
  success, so a legitimate sign-in washes the slate.
- **Uniform refusal — no account enumeration.** The named refusal
  (``too_many_attempts``, HTTP 429 with ``Retry-After``) is answered
  before any credential work runs, with one body for every cause: it
  reveals that failures happened, never whether the account exists.
  A refused attempt records nothing — pumping a locked endpoint cannot
  extend the lock.
- **Bounded exponential backoff.** The first ``FAILURE_THRESHOLD``
  failures in the decay window are answered with the ordinary uniform
  401; beyond the threshold the next attempt within
  ``required_delay(count)`` of the most recent failure is refused, the
  delay doubling per further failure and hard-capped at
  ``MAX_DELAY_SECONDS``.
- **Decay and reset.** Failures older than ``DECAY_SECONDS`` drop off the
  ledger, so the count — and the delay — shrinks as attempts quieten; a
  success clears the ledger outright. The ledger's length is hard-capped
  (``LEDGER_CAP``), so memory is bounded no matter the attempt rate.
- **Soft limit.** Check-then-record across concurrent requests is not
  atomic; the throttle bounds work, it is not a lock.
"""

from dataclasses import dataclass, field

#: Failures within the window before any backoff applies.
FAILURE_THRESHOLD = 5

#: The first backoff period — the wait after the threshold-th failure.
BASE_DELAY_SECONDS = 2.0

#: The backoff never grows past this, however deep the failure count.
MAX_DELAY_SECONDS = 900.0

#: A failure older than this drops off the ledger (and out of the count).
DECAY_SECONDS = 900.0

#: At most this many failure timestamps are held per key.
LEDGER_CAP = 32

#: The named refusal for a throttled login; the API layer spells the same.
TOO_MANY_ATTEMPTS = "too_many_attempts"


def required_delay(failure_count: int) -> float:
    """The backoff period for ``failure_count`` recent failures, in seconds.

    Below the threshold there is no delay; above it the delay doubles per
    failure beyond the threshold, hard-capped at ``MAX_DELAY_SECONDS``.
    """
    if failure_count < FAILURE_THRESHOLD:
        return 0.0
    return min(MAX_DELAY_SECONDS, BASE_DELAY_SECONDS * 2.0 ** (failure_count - FAILURE_THRESHOLD))


@dataclass
class ThrottleLedger:
    """One key's recent failure timestamps; all decisions take ``now`` in."""

    failures: list[float] = field(default_factory=list)

    def record_failure(self, *, now: float) -> None:
        """Account one credential failure at ``now``; prune and cap first."""
        self._prune(now=now)
        self.failures.append(now)
        if len(self.failures) > LEDGER_CAP:
            del self.failures[: len(self.failures) - LEDGER_CAP]

    def record_success(self) -> None:
        """A successful sign-in washes the slate."""
        self.failures.clear()

    def refusal_wait(self, *, now: float) -> float | None:
        """Seconds before an attempt is allowed again, or ``None`` if now.

        An attempt within ``required_delay(count)`` of the most recent
        failure is refused; reading never mutates the ledger.
        """
        self._prune(now=now)
        delay = required_delay(len(self.failures))
        if delay == 0.0:
            return None
        wait = self.failures[-1] + delay - now
        return wait if wait > 0 else None

    def _prune(self, *, now: float) -> None:
        cutoff = now - DECAY_SECONDS
        self.failures = [stamp for stamp in self.failures if stamp > cutoff]
