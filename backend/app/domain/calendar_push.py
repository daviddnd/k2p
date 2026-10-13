"""One-way calendar sync's API-push half (scope §2.4 v1.22; ADR-0013 item 3,
2026-10-03 amendment; slice M8).

The feed (``app.domain.calendar_sync``) is the generic fallback: a subscriber's
client polls an `.ics` capability URL and applies its own rules. The push is
the better path for a tenant that has registered its own Google or Microsoft
app with this deployment: events land in the TCSP's own calendar directly,
updated in place, with no subscribe action and no poll lag. The push reuses
the feed's projection verbatim — same ``IcsEvent`` entries, same
obligation-keyed UIDs — so the two paths can never assert different things
about the same obligation. This module owns only what the push adds:

- **The push/fallback decision per tenant.** No provider credential → the
  tenant stays on the feed. A credential → push, and the decision carries the
  provider and the probe flag. The probe (ruling R5; the ``/mail/probe``
  precedent) is honest work — it writes and deletes a probe event, proving
  write permission — so a credential whose probe never passed does not yet
  push: ``PushDecision.pushes`` is true only for a probed credential. The
  fail-safe default is the feed, which every tier keeps.
- **Idempotency by content (ruling R3).** A push happens only when the
  entry's content hash changes or the obligation is new; a disappeared
  obligation deletes the provider event once and forgets the row. The hash
  is a pure function over the projection entry's canonical fields — identity,
  date, summary, description, alarms — so any real change (a moved due date,
  a renamed company, an alarm fix) re-pushes and mere re-sweeps do not. The
  description carries the risk band as of the sweep's date, so a band change
  across days is a real content change and re-pushes too.
- **The push-set derivation.** ``derive_push_plan`` classifies the covered
  companies' entries against the stored state as created / updated /
  unchanged, and marks stored rows whose obligation has disappeared for
  delete. Entries of companies the §5.1 gate now denies are carried
  separately as *retained*: a lapsed gate stops pushes and never deletes
  (ADR-0012's "already-synced entries persist"), so a retained entry must be
  absent from every set of the plan.

Pure and framework-free per ADR-0006: stdlib only.
"""

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum

from app.domain.ics import IcsEvent


class CalendarProviderName(StrEnum):
    """The calendar providers the push supports (the TCSP's own registration)."""

    GOOGLE = "google"
    MICROSOFT = "microsoft"


class SyncMode(StrEnum):
    """How a tenant's deadlines reach its calendar: the API push or the feed."""

    PUSH = "push"
    FEED = "feed"


@dataclass(frozen=True)
class ProviderCredential:
    """One tenant's provider credential — the TCSP's own app registration.

    Google's shape is ``client_id`` / ``client_secret`` / ``refresh_token``
    plus the target calendar; Microsoft's adds ``directory_tenant_id`` (the
    M365 tenant the app is registered in). The credential is custody data,
    not configuration (ADR-0014): it lives in a mode-0600 file per tenant,
    never the database. ``probe_passed`` records that the probe wrote and
    deleted a probe event against the target calendar — until then the
    credential exists but the push stays off.
    """

    provider: CalendarProviderName
    client_id: str
    client_secret: str
    refresh_token: str
    calendar_id: str
    directory_tenant_id: str | None = None  # Microsoft only
    probe_passed: bool = False


@dataclass(frozen=True)
class PushDecision:
    """How one tenant's sync runs: the mode, the provider, the probe flag."""

    mode: SyncMode
    provider: CalendarProviderName | None
    probe_passed: bool

    @property
    def pushes(self) -> bool:
        """Whether the push actually fires: a probed credential, never an
        unprobed one — the fail-safe default is the feed."""
        return self.mode is SyncMode.PUSH and self.probe_passed


@dataclass(frozen=True)
class PushEntryState:
    """One stored push row as the domain sees it: what the provider holds."""

    uid: str
    provider_event_id: str
    content_hash: str


@dataclass(frozen=True)
class PushPlan:
    """The sweep's work over one projection, derived purely from the hashes.

    ``created`` and ``updated`` carry the entries to upsert (updated keeps
    its UID — the provider updates the event in place); ``deleted`` carries
    the stored state of each disappeared obligation, provider event id
    included, so the caller can delete the provider event and forget the row.
    """

    created: tuple[IcsEvent, ...]
    updated: tuple[IcsEvent, ...]
    unchanged: tuple[str, ...]
    deleted: tuple[PushEntryState, ...]


def decide_sync(credential: ProviderCredential | None) -> PushDecision:
    """The push/fallback decision for one tenant's credential state."""
    if credential is None:
        return PushDecision(mode=SyncMode.FEED, provider=None, probe_passed=False)
    return PushDecision(
        mode=SyncMode.PUSH, provider=credential.provider, probe_passed=credential.probe_passed
    )


def content_hash(event: IcsEvent) -> str:
    """The deterministic content hash of one projection entry.

    A pure function over the entry's canonical fields — identity, date,
    summary, description, and every alarm — serialized as JSON and digested.
    Two entries with equal fields hash equal, in any process and on any run;
    any real change moves the hash, and nothing time-varying outside the
    fields (object identity, dict order) can.
    """
    payload = [
        event.uid,
        event.date.isoformat(),
        event.summary,
        event.description,
        [[alarm.days_before, alarm.description] for alarm in event.alarms],
    ]
    return hashlib.sha256(json.dumps(payload, separators=(",", ":")).encode("utf-8")).hexdigest()


def derive_push_plan(
    *,
    pushed: Sequence[IcsEvent],
    retained: Sequence[IcsEvent],
    existing: Mapping[str, PushEntryState],
) -> PushPlan:
    """Classify the pushed entries against the stored state, and mark deletes.

    ``pushed`` is the current projection of the companies the §5.1 gate
    covers — the only entries a sweep may write. ``retained`` is the
    projection of the companies the gate now denies: their stored rows must
    survive (a lapsed gate never deletes), so a retained UID is excluded
    from the delete set without being upserted. Every set is sorted by UID
    so a sweep is deterministic run over run.
    """
    pushed_by_uid = {event.uid: event for event in pushed}
    retained_uids = {event.uid for event in retained}
    created: list[IcsEvent] = []
    updated: list[IcsEvent] = []
    unchanged: list[str] = []
    for uid in sorted(pushed_by_uid):
        event = pushed_by_uid[uid]
        state = existing.get(uid)
        if state is None:
            created.append(event)
        elif state.content_hash != content_hash(event):
            updated.append(event)
        else:
            unchanged.append(uid)
    deleted = tuple(
        existing[uid]
        for uid in sorted(existing)
        if uid not in pushed_by_uid and uid not in retained_uids
    )
    return PushPlan(
        created=tuple(created), updated=tuple(updated), unchanged=tuple(unchanged), deleted=deleted
    )
