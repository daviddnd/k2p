"""The mailbox credential, in one file, mode 0600 (ADR-0005; ADR-0014 custody).

Gmail hands this deployment an access token and a refresh token, and the
refresh token is the durable one: it is a credential under ADR-0014's rules,
so it is kept where the activation token is kept — **never in the database**
(a dump, a replica, or a backup handed to an operator would carry it), never
in the image, never in a log — in a file whose path comes from
``AICS_MAIL_CREDENTIAL_FILE`` (default a gitignored path under the service's
working directory), written atomically with mode 0600.

The pending consent attempt lives in the same file for the same reason: it
holds the PKCE verifier and the ``state`` between the administrator's POST
and the browser's return, it must survive a restart, and it must not travel
into the database. The two share a lifetime rule rather than a shape.

A missing file is a deployment that has not connected a mailbox. An
unreadable or unparsable one reads as empty rather than raising: nothing in
the statutory work depends on this file, and a damaged credential is fixed
by connecting again.

``clear`` removes the file outright rather than writing an empty document:
disconnecting is the operator asking the deployment to hold no mailbox
credential, and a file that holds no token is still a file holding a
redacted shape of one.
"""

import json
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from uuid import UUID

from app.domain.mail import (
    MailboxCredential,
    MailConnectionState,
    PendingMailConnection,
)

# Owner-only: the refresh token is the deployment's mailbox credential.
FILE_MODE = 0o600
DIRECTORY_MODE = 0o700

_TEMPORARY_SUFFIX = ".tmp"


@dataclass(frozen=True)
class MailCredentialStore:
    """The credential file at ``path``, read and written whole."""

    path: Path

    def load(self) -> MailConnectionState:
        """The state the file holds (an empty one when it holds nothing usable)."""
        document = self._read_document()
        return MailConnectionState(
            credential=_parse_credential(document.get("credential")),
            pending=_parse_pending(document.get("pending")),
        )

    def save(self, state: MailConnectionState) -> None:
        """Write ``state`` at mode 0600, replacing whatever was there.

        The write goes to a temporary sibling and is then renamed into place,
        so a concurrent reader — or a crash — never observes a half-written
        credential file.
        """
        self.path.parent.mkdir(mode=DIRECTORY_MODE, parents=True, exist_ok=True)
        temporary = self.path.with_name(self.path.name + _TEMPORARY_SUFFIX)
        # os.open applies FILE_MODE directly; a umask can only remove bits
        # from it, never widen it.
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, FILE_MODE)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(_document_for(state), handle, sort_keys=True)
        os.replace(temporary, self.path)

    def clear(self) -> None:
        """Forget everything: the tokens, the address, and any pending attempt."""
        self.path.unlink(missing_ok=True)

    def _read_document(self) -> dict[str, object]:
        try:
            document = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return document if isinstance(document, dict) else {}


def _document_for(state: MailConnectionState) -> dict[str, object]:
    document: dict[str, object] = {}
    if state.credential is not None:
        document["credential"] = {
            "access_token": state.credential.access_token,
            "refresh_token": state.credential.refresh_token,
            "address": state.credential.address,
            "scopes": list(state.credential.scopes),
            "connected_at": state.credential.connected_at.isoformat(),
            "token_expires_at": state.credential.token_expires_at.isoformat(),
        }
    if state.pending is not None:
        document["pending"] = {
            "state": state.pending.state,
            "verifier": state.pending.verifier,
            "local_tenant_id": str(state.pending.local_tenant_id),
            "local_user_id": str(state.pending.local_user_id),
            "expires_at": state.pending.expires_at.isoformat(),
        }
    return document


def _parse_credential(value: object) -> MailboxCredential | None:
    if not isinstance(value, dict):
        return None
    try:
        scopes = value["scopes"]
        if not isinstance(scopes, list) or not all(isinstance(scope, str) for scope in scopes):
            raise ValueError("scopes")
        return MailboxCredential(
            access_token=_text(value, "access_token"),
            refresh_token=_text(value, "refresh_token"),
            address=_text(value, "address"),
            scopes=tuple(scopes),
            connected_at=_timestamp(value, "connected_at"),
            token_expires_at=_timestamp(value, "token_expires_at"),
        )
    except (KeyError, ValueError, TypeError):
        return None


def _parse_pending(value: object) -> PendingMailConnection | None:
    if not isinstance(value, dict):
        return None
    try:
        return PendingMailConnection(
            state=_text(value, "state"),
            verifier=_text(value, "verifier"),
            local_tenant_id=UUID(_text(value, "local_tenant_id")),
            local_user_id=UUID(_text(value, "local_user_id")),
            expires_at=_timestamp(value, "expires_at"),
        )
    except (KeyError, ValueError, TypeError):
        return None


def _text(document: dict[str, object], field: str) -> str:
    value = document[field]
    if not isinstance(value, str) or not value:
        raise ValueError(field)
    return value


def _timestamp(document: dict[str, object], field: str) -> datetime:
    return datetime.fromisoformat(_text(document, field))
