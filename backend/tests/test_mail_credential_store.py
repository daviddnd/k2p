"""Tests for the mailbox credential file (ADR-0005; Phase 3 custody).

The mailbox tokens are credentials under ADR-0014, so where they are kept is
a security property rather than a storage detail: a 0600 file, written
atomically, outside the database and outside the image, with the pending
attempt beside them because it shares their lifetime rule. These pin the
parts that would be invisible until they were wrong — the file mode, the
atomic replace, the refusal to be broken by a damaged file, and a disconnect
that leaves no token behind.
"""

import json
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from app.adapters.mail.credential_store import MailCredentialStore
from app.domain.mail import (
    GMAIL_SCOPES,
    MailboxCredential,
    MailConnectionState,
    PendingMailConnection,
)

NOW = datetime(2026, 9, 23, 9, 0, tzinfo=UTC)
MARKER = "planted-token-marker"


def credential() -> MailboxCredential:
    return MailboxCredential(
        access_token=f"{MARKER}-access",
        refresh_token=f"{MARKER}-refresh",
        address="ai-filings@tcsp.example",
        scopes=GMAIL_SCOPES,
        connected_at=NOW,
        token_expires_at=NOW + timedelta(hours=1),
    )


def pending() -> PendingMailConnection:
    return PendingMailConnection(
        state="state-1",
        verifier="v" * 43,
        local_tenant_id=uuid4(),
        local_user_id=uuid4(),
        expires_at=NOW + timedelta(minutes=10),
    )


def test_a_missing_file_is_a_deployment_with_no_mailbox(tmp_path: Path) -> None:
    state = MailCredentialStore(tmp_path / "absent").load()

    assert state == MailConnectionState()
    assert state.is_connected is False


def test_the_credential_file_is_written_owner_only(tmp_path: Path) -> None:
    store = MailCredentialStore(tmp_path / "nested" / "mail-credential")
    store.save(MailConnectionState(credential=credential()))

    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
    # The directory the deployment created for it is owner-only too.
    assert stat.S_IMODE(store.path.parent.stat().st_mode) == 0o700


def test_a_saved_state_round_trips(tmp_path: Path) -> None:
    store = MailCredentialStore(tmp_path / "mail-credential")
    state = MailConnectionState(credential=credential(), pending=pending())
    store.save(state)

    assert store.load() == state


def test_the_tokens_are_only_in_this_file(tmp_path: Path) -> None:
    # The point of the file: the credential is usable from here and is not
    # anywhere else the deployment stores state (the database, above all).
    store = MailCredentialStore(tmp_path / "mail-credential")
    store.save(MailConnectionState(credential=credential()))

    document = json.loads(store.path.read_text(encoding="utf-8"))
    assert document["credential"]["refresh_token"] == f"{MARKER}-refresh"
    assert document["credential"]["address"] == "ai-filings@tcsp.example"


def test_a_damaged_file_reads_as_empty_rather_than_raising(tmp_path: Path) -> None:
    # Nothing in the product depends on this file to reach the statutory
    # work (the manual path never did), so a damaged credential is recovered
    # by connecting again rather than by taking a request down.
    store = MailCredentialStore(tmp_path / "mail-credential")
    store.path.write_text("{ not json", encoding="utf-8")

    assert store.load() == MailConnectionState()


def test_a_credential_with_a_missing_field_reads_as_absent(tmp_path: Path) -> None:
    store = MailCredentialStore(tmp_path / "mail-credential")
    store.path.write_text(
        json.dumps(
            {
                "credential": {"access_token": "a", "address": "x@y"},
                "pending": {"state": "only-a-state"},
            }
        ),
        encoding="utf-8",
    )

    state = store.load()

    assert state.credential is None
    assert state.pending is None


def test_saving_replaces_the_file_rather_than_appending(tmp_path: Path) -> None:
    store = MailCredentialStore(tmp_path / "mail-credential")
    store.save(MailConnectionState(credential=credential()))
    store.save(MailConnectionState(pending=pending()))

    state = store.load()
    assert state.pending is not None
    assert state.credential is None
    # The temporary sibling the atomic write uses is not left behind.
    assert not (tmp_path / "mail-credential.tmp").exists()


def test_disconnecting_leaves_no_token_on_disk(tmp_path: Path) -> None:
    store = MailCredentialStore(tmp_path / "mail-credential")
    store.save(MailConnectionState(credential=credential(), pending=pending()))

    store.clear()

    assert not store.path.exists()
    assert MARKER not in "".join(
        path.read_text(encoding="utf-8", errors="ignore")
        for path in tmp_path.rglob("*")
        if path.is_file()
    )


def test_clearing_a_missing_file_is_not_an_error(tmp_path: Path) -> None:
    MailCredentialStore(tmp_path / "never-written").clear()
