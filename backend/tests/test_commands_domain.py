"""The command-envelope decode ladder and vocabulary, checked directly
(ADR-0012 open action 3; contract §5.2; the M7 orchestration vocabulary).

Two properties are proved here rather than asserted in prose: a well-formed
envelope of this version decodes, and everything else fails at the rung it
deserves — an unreadable document (no id to answer against), an unknown
version (refused without being executed), or a shape that is not this
version's. The vocabulary guard is the same rule the integration suite runs
over *stored* envelopes on both sides of the transport, so its teeth are
tested here: a company name where an opaque reference belongs must fail it.
"""

from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from app.domain.commands import (
    ENVELOPE_VERSION,
    CommandErrorCode,
    CommandName,
    EnvelopeInvalid,
    EnvelopeUnreadable,
    EnvelopeVersionUnsupported,
    command_violations,
    decode_envelope,
    result_violations,
)

COMMAND_ID = "7c9f2a3b-4d5e-4f60-8a1b-2c3d4e5f6071"
NOW = datetime(2026, 9, 21, 9, 15, tzinfo=UTC)


def envelope(**overrides: object) -> dict[str, object]:
    document: dict[str, object] = {
        "envelope_version": ENVELOPE_VERSION,
        "command_id": COMMAND_ID,
        "command": "ping",
        "params": {"local_record_refs": {}},
        "expected_outputs": [],
        "issued_at": NOW.isoformat(),
    }
    document.update(overrides)
    return document


def result(**overrides: object) -> dict[str, object]:
    document: dict[str, object] = {
        "envelope_version": ENVELOPE_VERSION,
        "command_id": COMMAND_ID,
        "status": "succeeded",
        "error_code": None,
        "telemetry": {"duration_ms": 2, "queue_depth": 1, "clock": NOW.isoformat()},
        "completed_at": NOW.isoformat(),
    }
    document.update(overrides)
    return document


# --- The decode ladder ----------------------------------------------------


def test_an_envelope_of_this_version_decodes() -> None:
    company_ref = str(uuid4())
    decoded = decode_envelope(
        envelope(
            params={"local_record_refs": {"company_ref": company_ref}},
            expected_outputs=["deployment_metadata"],
        )
    )

    assert str(decoded.command_id) == COMMAND_ID
    assert decoded.envelope_version == ENVELOPE_VERSION
    assert decoded.command == "ping"
    assert decoded.local_record_refs == {"company_ref": company_ref}
    assert decoded.expected_outputs == ("deployment_metadata",)
    assert decoded.issued_at == NOW


def test_a_document_without_an_id_is_unreadable() -> None:
    with pytest.raises(EnvelopeUnreadable):
        decode_envelope(envelope(command_id="the-first-one"))
    with pytest.raises(EnvelopeUnreadable):
        decode_envelope({"command": "ping"})


def test_a_version_this_worker_does_not_know_is_refused_as_such() -> None:
    # The version is checked before the shape: a later envelope may have
    # fields this build cannot judge, and it must not be executed anyway.
    with pytest.raises(EnvelopeVersionUnsupported) as raised:
        decode_envelope(envelope(envelope_version=ENVELOPE_VERSION + 1, note="new in v2"))

    assert raised.value.version == ENVELOPE_VERSION + 1


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"envelope_version": "1"}, id="version-as-text"),
        pytest.param({"envelope_version": True}, id="version-as-boolean"),
        pytest.param({"command": "Ping"}, id="command-not-an-identifier"),
        pytest.param({"command": "read the company file"}, id="command-is-prose"),
        pytest.param({"params": []}, id="params-not-an-object"),
        pytest.param({"params": {"refs": {}}}, id="params-renamed-key"),
        pytest.param(
            {"params": {"local_record_refs": {"company_ref": "Chan Tai Man Ltd"}}},
            id="reference-carries-a-name",
        ),
        pytest.param({"expected_outputs": ["deployment metadata"]}, id="output-is-prose"),
        pytest.param({"expected_outputs": "deployment_metadata"}, id="outputs-not-a-list"),
        pytest.param({"issued_at": "2026-09-21T09:15:00"}, id="issued-at-naive"),
        pytest.param({"issued_at": "yesterday"}, id="issued-at-not-a-timestamp"),
        pytest.param({"note": "for the auditor"}, id="unknown-field"),
    ],
)
def test_a_malformed_envelope_is_refused_locally(overrides: dict[str, object]) -> None:
    with pytest.raises(EnvelopeInvalid) as raised:
        decode_envelope(envelope(**overrides))

    assert raised.value.violations  # the reason names the field path, not a value


def test_a_command_nobody_implements_still_decodes() -> None:
    # ``unknown_command`` is the registry's refusal, not the guard's: an
    # envelope naming a command this build does not have is a perfectly
    # readable envelope, and it must be recordable so the refusal can be
    # reported.
    assert decode_envelope(envelope(command="deliver_pack")).command == "deliver_pack"
    assert command_violations(envelope(command="deliver_pack")) == []


# --- The vocabulary guard -------------------------------------------------


def test_a_well_formed_envelope_passes_the_guard() -> None:
    assert command_violations(envelope()) == []
    assert result_violations(result()) == []
    assert (
        result_violations(
            result(status="rejected", error_code="entitlement_denied", telemetry={})
        )
        == []
    )


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"command": "Ping"}, id="command-not-an-identifier"),
        pytest.param(
            {"params": {"local_record_refs": {"company_ref": "Chan Tai Man Ltd"}}},
            id="reference-carries-a-name",
        ),
        pytest.param({"expected_outputs": ["the director's HKID"]}, id="output-is-prose"),
        pytest.param({"issued_at": "21/09/2026 09:15"}, id="issued-at-not-rfc3339"),
        pytest.param({"tenant_id": str(uuid4())}, id="field-the-transport-does-not-have"),
    ],
)
def test_a_command_envelope_outside_the_vocabulary_fails_the_guard(
    overrides: dict[str, object],
) -> None:
    assert command_violations(envelope(**overrides))


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"status": "maybe"}, id="unknown-status"),
        pytest.param({"status": "failed"}, id="code-missing-on-failure"),
        pytest.param({"error_code": "execution_failed"}, id="code-on-success"),
        pytest.param({"telemetry": {"notes": "called the director"}}, id="unknown-telemetry-key"),
        pytest.param({"telemetry": {"app_version": "the 2026-09-21 build"}}, id="version-is-prose"),
        pytest.param({"telemetry": {"duration_ms": "fast"}}, id="count-is-text"),
        pytest.param({"completed_at": "soon"}, id="completed-at-not-a-timestamp"),
    ],
)
def test_a_result_envelope_outside_the_vocabulary_fails_the_guard(
    overrides: dict[str, object],
) -> None:
    assert result_violations(result(**overrides))


def test_the_error_vocabulary_is_the_contract_one() -> None:
    assert {member.value for member in CommandErrorCode} == {
        "unsupported_envelope_version",
        "unknown_command",
        "entitlement_denied",
        "stale_state",
        "local_validation_failed",
        "execution_failed",
    }


# --- The M7 orchestration vocabulary --------------------------------------


def test_schedule_orchestration_is_in_the_registry() -> None:
    assert CommandName.SCHEDULE_ORCHESTRATION == "schedule_orchestration"


def test_orchestration_telemetry_keys_are_accepted() -> None:
    telemetry = {
        "mail_ingested": 4,
        "replies_parsed": 2,
        "reminders_enqueued": 1,
        "messages_sent": 3,
        "cycles_failed": 0,
    }
    assert result_violations(result(telemetry=telemetry)) == []


def test_telemetry_keys_stay_closed() -> None:
    violations = result_violations(result(telemetry={"sprockets_polled": 1}))
    assert "telemetry: unknown key" in violations


def test_envelope_version_unchanged() -> None:
    assert ENVELOPE_VERSION == 1
    # The vendor's copy speaks the same version: an additive vocabulary
    # change never bumps it. The two deployables cannot share an interpreter,
    # so the sibling copy is loaded by file path.
    import importlib.util

    path = (
        Path(__file__).resolve().parents[2]
        / "control_plane"
        / "app"
        / "domain"
        / "commands.py"
    )
    spec = importlib.util.spec_from_file_location("control_plane_commands", path)
    assert spec is not None and spec.loader is not None
    control_plane_commands = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(control_plane_commands)
    assert control_plane_commands.ENVELOPE_VERSION == 1
