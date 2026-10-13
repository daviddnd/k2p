"""The calendar adapters' shared failure vocabulary (the ``app.adapters.mail.
errors`` precedent).

Two kinds of failure, one per question the caller has to answer. An **auth
failure** is durable: the refresh grant is gone, the secret is wrong, or the
API rejects the credential — reconnecting the provider app is the fix, and
retrying the sweep will refuse the same way until then. A **transient
failure** is the provider (or the network) failing now: the outage shape, a
rate limit, a 5xx, or a body that is not the shape the API promises — the next
sweep may succeed, so the push state is left untouched and the call is
retried on the next cycle.

The Task 3 worker catches the family by the base class, never httpx: nothing
here carries a transport object, a response, or a provider body. ``reason`` is
a bounded code (the token endpoint's ``invalid_grant``, the API's own error
code), and ``status`` is the HTTP status when there was one (``None`` for a
transport failure or an unreadable body).
"""

# Refusals are recorded and rendered; keep them short and free of anything the
# provider echoed (the ``gmail_client`` precedent).
_MAX_REASON_LENGTH = 120


class CalendarProviderError(Exception):
    """Base of the calendar adapters' typed failures — the family's catch."""


class CalendarAuthFailed(CalendarProviderError):
    """The credential's grant is refused: the app registration must be reconnected.

    Raised for a refused token exchange (``invalid_grant``, ``invalid_client``)
    and for API answers 401/403. Durable — a later sweep refuses the same way
    until the credential is fixed.
    """

    def __init__(self, reason: str, *, status: int | None = None) -> None:
        self.reason = reason[:_MAX_REASON_LENGTH]
        self.status = status
        super().__init__(self.reason)


class CalendarProviderUnavailable(CalendarProviderError):
    """The provider could not complete the call now; a later sweep may succeed.

    Raised for transport errors (no status), rate limits and 5xx, responses
    without a usable body, and any other refusal that is not an auth verdict —
    including 404 on a write, where the honest reading is "retry on the next
    sweep" (the reconciliation lookup then finds nothing and re-creates).
    """

    def __init__(self, reason: str, *, status: int | None = None) -> None:
        self.reason = reason[:_MAX_REASON_LENGTH]
        self.status = status
        super().__init__(self.reason)
