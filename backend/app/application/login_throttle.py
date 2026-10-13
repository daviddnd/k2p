"""The login throttle's process-wide ledger registry (slice ②, 2026-10-11).

The single throttle seam the login and MFA-challenge routes consult. The
backoff *rules* live in ``app.domain.login_throttle`` (pure, clock-injected);
this module owns only the process-wide registry mapping throttle keys to
``ThrottleLedger`` rows and the two-key consult the routes perform. The
deployment runs one uvicorn process (``deploy/backend.Dockerfile``), so
in-memory state is the deployment's truth; no external service is added.

The rulebook this module pins:

- **Key namespaces.** Keys are namespaced here (``source:`` / ``account:``),
  never at the call site, so an address and an email that spell alike can
  never share a ledger.
- **One refusal, and only when both keys are hot** (the founder ruling of
  2026-10-12). ``refusal_wait`` answers a wait only when the source AND the
  account ledger are both inside their backoff; a single hot key lets the
  attempt through to the credential check, where a correct password signs
  in and clears both ledgers. The pre-work refusal exists to bound work,
  not to lock accounts: an attacker gains no guessing throughput from a
  single-hot pass-through (the per-source five-in-window bound is the same
  bound the check has always answered to), while the old behaviour allowed
  a sustained poke every three minutes to lock an account forever — with
  no recovery, since the victim's correct password was refused before it
  was ever checked. The API layer's 429 body stays uniform: it never names
  which key refused (now: that both did).
- **Both keys write together.** A credential failure records on the source
  and the account ledger alike; a success clears both.
- **Memory.** Every touch prunes by the decay window and the ledger length
  is hard-capped (the domain's ``LEDGER_CAP``), so each ledger stays bounded
  at any attempt rate; a ledger that prunes or clears to empty is dropped
  from the registry, so the registry holds hot keys only — a distinct
  attempted email or address is not a permanent entry.
- **Concurrency.** A check-then-record across two awaited calls is not
  atomic — the throttle bounds work, it is not a lock. The check creates
  nothing: a clean key is allowed without entering the registry.
"""

from app.domain.login_throttle import ThrottleLedger

#: The process-wide registry: throttle key → its recent-failure ledger.
#: Owned here; tests reset it between cases.
_ledgers: dict[str, ThrottleLedger] = {}


def refusal_wait(*, source_key: str, account_key: str, now: float) -> float | None:
    """Seconds a caller must wait before any credential work, or ``None`` to proceed.

    The pre-work refusal fires only when BOTH keys are hot (the founder
    ruling of 2026-10-12): a single hot key — the attacked account, or the
    shared office egress — no longer locks anyone out. The attempt proceeds
    to the credential check, and a correct password or code signs in and
    clears both ledgers. Consults existing ledgers only; a clean key is
    allowed without its registry entry being created, and an emptied ledger
    is dropped.
    """
    source_wait: float | None = None
    account_wait: float | None = None
    for key in (f"source:{source_key}", f"account:{account_key}"):
        ledger = _ledgers.get(key)
        if ledger is None:
            continue
        wait = ledger.refusal_wait(now=now)
        _drop_if_empty(key, ledger)
        if key.startswith("source:"):
            source_wait = wait
        else:
            account_wait = wait
    if source_wait is not None and account_wait is not None:
        return max(source_wait, account_wait)
    return None


def record_failure(*, source_key: str, account_key: str, now: float) -> None:
    """One credential failure, recorded on both keys at ``now``."""
    for key in (f"source:{source_key}", f"account:{account_key}"):
        _ledger(key).record_failure(now=now)


def record_success(*, source_key: str, account_key: str) -> None:
    """A successful sign-in clears both keys' ledgers (dropping emptied ones)."""
    for key in (f"source:{source_key}", f"account:{account_key}"):
        ledger = _ledgers.get(key)
        if ledger is not None:
            ledger.record_success()
            _drop_if_empty(key, ledger)


def _ledger(key: str) -> ThrottleLedger:
    ledger = _ledgers.get(key)
    if ledger is None:
        ledger = ThrottleLedger()
        _ledgers[key] = ledger
    return ledger


def _drop_if_empty(key: str, ledger: ThrottleLedger) -> None:
    """An emptied ledger leaves the registry: attempted keys must not
    accumulate forever (a cleared ledger is a few dozen bytes; dropping it
    is the honest bound, not an optimisation)."""
    if not ledger.failures:
        _ledgers.pop(key, None)
