"""Tests for the deployment's credential file (ADR-0014/0016; CP1b).

The activation token is a credential, so where it is kept is a security
property, not a storage detail: a 0600 file, atomically replaced, outside the
database and outside the image, holding non-secret identity beside it. These
tests pin the parts that would be invisible until they were wrong — the file
mode, the atomic replace, the survival of the deployment fingerprint across
deactivation, and the refusal to be broken by a damaged file.
"""

import json
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from app.adapters.entitlement.activation_store import ActivationStore
from app.domain.activation import (
    ActivationCredential,
    ActivationState,
    PendingActivation,
)

NOW = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)


def credential() -> ActivationCredential:
    return ActivationCredential(
        token="issued-token",
        token_id=uuid4(),
        control_plane_tenant_id=uuid4(),
        expires_at=NOW + timedelta(days=90),
    )


def pending() -> PendingActivation:
    return PendingActivation(
        request_id=uuid4(),
        verifier="v" * 43,
        local_tenant_id=uuid4(),
        local_user_id=uuid4(),
        expires_at=NOW + timedelta(minutes=10),
    )


def test_a_missing_file_is_a_deployment_that_has_not_activated(tmp_path: Path) -> None:
    state = ActivationStore(tmp_path / "absent").load()

    assert state == ActivationState()
    assert state.is_activated is False


def test_the_credential_file_is_written_owner_only(tmp_path: Path) -> None:
    store = ActivationStore(tmp_path / "nested" / "credential")
    store.save(ActivationState(deployment_fingerprint="fp", credential=credential()))

    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
    # The directory the deployment created for it is owner-only too.
    assert stat.S_IMODE(store.path.parent.stat().st_mode) == 0o700


def test_a_saved_state_round_trips(tmp_path: Path) -> None:
    store = ActivationStore(tmp_path / "credential")
    state = ActivationState(
        deployment_fingerprint="fp", credential=credential(), pending=pending()
    )
    store.save(state)

    assert store.load() == state


def test_the_token_is_only_in_this_file(tmp_path: Path) -> None:
    # The point of the file: the credential is usable from here and is not
    # anywhere else the deployment stores state (the database, above all).
    store = ActivationStore(tmp_path / "credential")
    store.save(ActivationState(deployment_fingerprint="fp", credential=credential()))

    document = json.loads(store.path.read_text(encoding="utf-8"))
    assert document["credential"]["token"] == "issued-token"
    assert document["deployment_fingerprint"] == "fp"


def test_deactivation_keeps_the_fingerprint_and_drops_the_credential(tmp_path: Path) -> None:
    store = ActivationStore(tmp_path / "credential")
    store.save(
        ActivationState(deployment_fingerprint="fp", credential=credential(), pending=pending())
    )

    store.save(
        ActivationState(deployment_fingerprint="fp", credential=None, pending=None)
    )
    state = store.load()

    assert state.deployment_fingerprint == "fp"
    assert state.credential is None
    assert state.pending is None


def test_a_damaged_file_reads_as_empty_rather_than_raising(tmp_path: Path) -> None:
    # The ungated path (manual fill → sign → export) must never depend on
    # this file; a damaged credential is recovered by activating again.
    store = ActivationStore(tmp_path / "credential")
    store.path.write_text("{ this is not json", encoding="utf-8")

    assert store.load() == ActivationState()


def test_a_credential_with_a_missing_field_reads_as_absent(tmp_path: Path) -> None:
    store = ActivationStore(tmp_path / "credential")
    store.path.write_text(
        json.dumps({"deployment_fingerprint": "fp", "credential": {"token": "t"}}),
        encoding="utf-8",
    )

    state = store.load()

    assert state.deployment_fingerprint == "fp"
    assert state.credential is None


def test_saving_replaces_the_file_rather_than_appending(tmp_path: Path) -> None:
    store = ActivationStore(tmp_path / "credential")
    store.save(ActivationState(deployment_fingerprint="first", credential=credential()))
    store.save(ActivationState(deployment_fingerprint="second"))

    assert store.load().deployment_fingerprint == "second"
    # The temporary sibling the atomic write uses is not left behind.
    assert not (tmp_path / "credential.tmp").exists()
