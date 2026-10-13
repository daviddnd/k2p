"""Command envelopes: the vocabulary and the decode ladder of the
control-plane → worker transport (ADR-0012 open action 3; contract §5.2).

The two deployables cannot import each other, so the vocabulary is written
here as well as in the vendor service (``control_plane/app/domain/commands.py``)
and both copies are exercised over stored data: the control plane's suite
checks its queue rows, and the execution plane's integration suite runs this
module's guard over the envelopes held on *both* sides — the machine-checked
half of the ADR-0012 hard rule.

An envelope may carry opaque identifiers, counts, durations, versions,
timestamps, and command names; nothing else survives the rules below. Free
text — a company name, an address, a remark — matches none of the grammars,
which is what makes "metadata-only" checkable rather than promised.

The **decode ladder** is this side's own, and its invariant is that nothing
is executed on a guess:

1. a document without a usable ``command_id`` is **unreadable** — there is
   nothing to record it under and nothing to report an outcome against, so
   the cycle counts it and drops it;
2. an ``envelope_version`` this build does not know (or cannot read as an
   integer) is refused without being executed — ``unsupported_envelope_version``
   for a different version, ``local_validation_failed`` for a version that is
   not one;
3. anything else that does not match this version's shape is refused with
   ``local_validation_failed``.

An envelope naming a command nobody implements is *readable* and is refused
later, by the registry, with ``unknown_command``: the guard checks what may
travel, the registry checks what may run.

Pure and framework-free per ADR-0006.
"""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

# The version this worker speaks. It executes this one and nothing else; the
# control plane refuses to enqueue for a deployment that reported less.
ENVELOPE_VERSION = 1

MAX_COUNT = 10**15

_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_OPAQUE_ID = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
_VERSION_TEXT = re.compile(r"^[0-9][0-9A-Za-z.+-]{0,31}$")


class CommandName(StrEnum):
    """The command registry this worker dispatches (contract §5.2).

    Two commands in this slice: ``ping``, which reports deployment metadata
    and touches nothing, and ``schedule_orchestration`` (M7), which asks the
    deployment to work its own queue — ingest mail, parse replies, run the
    confirmation-discovery sweep (§5.1.17), enqueue reminders, send messages
    — and report the cycle's counts. The enum is the registry's name
    vocabulary; the handler table lives in ``app.application.commands``.
    """

    PING = "ping"
    SCHEDULE_ORCHESTRATION = "schedule_orchestration"


class CommandStatus(StrEnum):
    """The outcome vocabulary a result envelope may carry (§5.1)."""

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    REJECTED = "rejected"


class CommandErrorCode(StrEnum):
    """The refusal vocabulary (§5.1), shared with the control plane.

    ``stale_state`` is also the reason a worker refuses an envelope that
    references local state the deployment no longer has: the control plane's
    view of this deployment is out of date.
    """

    UNSUPPORTED_ENVELOPE_VERSION = "unsupported_envelope_version"
    UNKNOWN_COMMAND = "unknown_command"
    ENTITLEMENT_DENIED = "entitlement_denied"
    STALE_STATE = "stale_state"
    LOCAL_VALIDATION_FAILED = "local_validation_failed"
    EXECUTION_FAILED = "execution_failed"


# Telemetry is a closed set of keys, each with a kind. A new key is a code
# change on both sides and a column on the control plane — deliberately,
# because that is the review point at which "metadata-only" is checked again.
TELEMETRY_COUNT_KEYS = frozenset(
    {
        "duration_ms",
        "queue_depth",
        "uptime_seconds",
        "mail_ingested",
        "replies_parsed",
        "confirmations_generated",
        "confirmations_refused",
        "confirmations_skipped",
        "reminders_enqueued",
        "messages_sent",
        "cycles_failed",
    }
)
TELEMETRY_VERSION_KEYS = frozenset({"app_version"})
TELEMETRY_TIMESTAMP_KEYS = frozenset({"clock"})
TELEMETRY_KEYS = TELEMETRY_COUNT_KEYS | TELEMETRY_VERSION_KEYS | TELEMETRY_TIMESTAMP_KEYS

_COMMAND_ENVELOPE_KEYS = frozenset(
    {"envelope_version", "command_id", "command", "params", "expected_outputs", "issued_at"}
)
_RESULT_ENVELOPE_KEYS = frozenset(
    {"envelope_version", "command_id", "status", "error_code", "telemetry", "completed_at"}
)

# The local reference an envelope uses to name a company. The one reference
# name the transport interprets; every other name stays opaque.
COMPANY_REF = "company_ref"


@dataclass(frozen=True)
class CommandEnvelope:
    """A readable envelope of this version."""

    command_id: UUID
    envelope_version: int
    command: str
    local_record_refs: Mapping[str, str]
    expected_outputs: tuple[str, ...]
    issued_at: datetime


class EnvelopeUnreadable(Exception):
    """The document carries no command id — it cannot be recorded or answered."""


class EnvelopeVersionUnsupported(Exception):
    """The envelope declares a version this worker does not execute."""

    def __init__(self, version: int) -> None:
        self.version = version
        super().__init__(f"envelope_version={version}")


class EnvelopeInvalid(Exception):
    """The envelope does not match this version's shape.

    ``violations`` names the field paths (never their values); the reason is
    stored in the local record and the audit entry, not sent back.
    """

    def __init__(self, violations: list[str]) -> None:
        self.violations = violations
        super().__init__("; ".join(violations))


def command_id_of(document: Mapping[str, object]) -> UUID | None:
    """The idempotency key an inbound document carries, if it carries one."""
    value = document.get("command_id")
    if not isinstance(value, str) or _OPAQUE_ID.match(value) is None:
        return None
    return UUID(value)


def decode_envelope(document: Mapping[str, object]) -> CommandEnvelope:
    """The envelope a document carries; raises the refusal explaining why not."""
    command_id = command_id_of(document)
    if command_id is None:
        raise EnvelopeUnreadable

    version = document.get("envelope_version")
    if isinstance(version, bool) or not isinstance(version, int):
        raise EnvelopeInvalid(["envelope_version: not an integer"])
    if version != ENVELOPE_VERSION:
        raise EnvelopeVersionUnsupported(version)

    violations = command_violations(document)
    if violations:
        raise EnvelopeInvalid(violations)

    params = document["params"]
    assert isinstance(params, Mapping)  # the guard accepted the document
    refs = params["local_record_refs"]
    assert isinstance(refs, Mapping)
    outputs = document["expected_outputs"]
    assert isinstance(outputs, Sequence) and not isinstance(outputs, (str, bytes))
    return CommandEnvelope(
        command_id=command_id,
        envelope_version=version,
        command=str(document["command"]),
        local_record_refs={str(name): str(value) for name, value in refs.items()},
        expected_outputs=tuple(str(name) for name in outputs),
        issued_at=datetime.fromisoformat(str(document["issued_at"])),
    )


def command_violations(document: Mapping[str, object]) -> list[str]:
    """Every place a command envelope carries something outside the vocabulary.

    An empty list means the document is a well-formed envelope of this
    version. ``command`` must be identifier-shaped but need not be one this
    worker implements — that is the registry's decision, not the guard's.
    """
    violations: list[str] = []
    _check_keys(document, _COMMAND_ENVELOPE_KEYS, violations)
    _check_version(document, violations)
    if command_id_of(document) is None:
        violations.append("command_id: not an opaque id")
    command = document.get("command")
    if not isinstance(command, str) or _IDENTIFIER.match(command) is None:
        violations.append("command: not an identifier")
    params = document.get("params")
    if not isinstance(params, Mapping):
        violations.append("params: not an object")
    else:
        if set(params) != {"local_record_refs"}:
            violations.append("params: unexpected keys")
        refs = params.get("local_record_refs")
        if not isinstance(refs, Mapping):
            violations.append("params.local_record_refs: not an object")
        else:
            for name, value in refs.items():
                if not isinstance(name, str) or _IDENTIFIER.match(name) is None:
                    violations.append("params.local_record_refs: key is not an identifier")
                if not isinstance(value, str) or _OPAQUE_ID.match(value) is None:
                    violations.append(f"params.local_record_refs.{name}: not an opaque id")
    _check_output_names(document.get("expected_outputs"), violations)
    _check_timestamp(document.get("issued_at"), "issued_at", violations)
    return violations


def result_violations(document: Mapping[str, object]) -> list[str]:
    """Every place a result envelope carries something outside the vocabulary.

    ``error_code`` is present exactly when the outcome is not ``succeeded``.
    """
    violations: list[str] = []
    _check_keys(document, _RESULT_ENVELOPE_KEYS, violations)
    _check_version(document, violations)
    if command_id_of(document) is None:
        violations.append("command_id: not an opaque id")
    status_value = document.get("status")
    known_status = (
        status_value
        if isinstance(status_value, str)
        and status_value in {member.value for member in CommandStatus}
        else None
    )
    if known_status is None:
        violations.append("status: not a known status")
    error_code = document.get("error_code")
    if error_code is None:
        if known_status is not None and known_status != CommandStatus.SUCCEEDED.value:
            violations.append("error_code: missing for a non-succeeded status")
    elif not isinstance(error_code, str) or error_code not in {
        member.value for member in CommandErrorCode
    }:
        violations.append("error_code: not a known error code")
    elif known_status == CommandStatus.SUCCEEDED.value:
        violations.append("error_code: present on a succeeded result")
    telemetry = document.get("telemetry")
    if not isinstance(telemetry, Mapping):
        violations.append("telemetry: not an object")
    else:
        for key, value in telemetry.items():
            _check_telemetry(key, value, violations)
    _check_timestamp(document.get("completed_at"), "completed_at", violations)
    return violations


def _check_keys(
    document: Mapping[str, object], allowed: frozenset[str], violations: list[str]
) -> None:
    unknown = sorted(set(document) - allowed)
    if unknown:
        violations.append(f"unknown keys: {', '.join(unknown)}")


def _check_version(document: Mapping[str, object], violations: list[str]) -> None:
    version = document.get("envelope_version")
    if isinstance(version, bool) or not isinstance(version, int):
        violations.append("envelope_version: not an integer")
    elif version != ENVELOPE_VERSION:
        violations.append("envelope_version: not this build's version")


def _check_output_names(declared: object, violations: list[str]) -> None:
    if not isinstance(declared, Sequence) or isinstance(declared, (str, bytes)):
        violations.append("expected_outputs: not a list")
        return
    for name in declared:
        if not isinstance(name, str) or _IDENTIFIER.match(name) is None:
            violations.append("expected_outputs: entry is not an identifier")


def _check_telemetry(key: object, value: object, violations: list[str]) -> None:
    if not isinstance(key, str) or key not in TELEMETRY_KEYS:
        violations.append("telemetry: unknown key")
        return
    if key in TELEMETRY_COUNT_KEYS:
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= MAX_COUNT:
            violations.append(f"telemetry.{key}: not a count")
    elif key in TELEMETRY_VERSION_KEYS:
        if not isinstance(value, str) or _VERSION_TEXT.match(value) is None:
            violations.append(f"telemetry.{key}: not a version")
    else:
        _check_timestamp(value, f"telemetry.{key}", violations)


def _check_timestamp(value: object, field: str, violations: list[str]) -> None:
    if not isinstance(value, str):
        violations.append(f"{field}: not a timestamp")
        return
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        violations.append(f"{field}: not a timestamp")
        return
    if parsed.tzinfo is None:
        violations.append(f"{field}: not timezone-aware")
