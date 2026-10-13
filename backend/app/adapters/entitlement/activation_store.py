"""This deployment's local activation state, in one file, mode 0600.

The activation token is a credential (ADR-0016 point 3) and is treated like
one under ADR-0014's custody rules: **never in the database** — a database
backup, a replica, or a dump handed to an operator would leak it — never in
the image, and never in a log. It lives in a file whose path comes from
``AICS_CP_ACTIVATION_TOKEN_FILE`` (default a gitignored path under the
service's working directory), written atomically with mode 0600.

That file also carries the deployment fingerprint and the pending activation
attempt, because they share its lifetime rule rather than its secrecy: all
three are local state that must survive a restart, must not travel into the
database, and must not be copied into an image. The pending attempt holds the
PKCE verifier until the browser returns, and the deployment fingerprint
outlives deactivation — the vendor keys the tenant by it, so forgetting it
would register a second tenant on the next activation.

A missing file is a deployment that has never activated. An unreadable or
unparsable one is read as empty rather than raised: the ungated path (manual
fill → sign → export) must never depend on this file, and a damaged
credential is recovered by activating again, not by taking the product down.
"""

import json
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from uuid import UUID

from app.domain.activation import (
    ActivationCredential,
    ActivationState,
    PendingActivation,
)

# Owner-only: the token is the deployment's credential (ADR-0014).
FILE_MODE = 0o600
DIRECTORY_MODE = 0o700

_TEMPORARY_SUFFIX = ".tmp"


@dataclass(frozen=True)
class ActivationStore:
    """The credential file at ``path``, read and written whole."""

    path: Path

    def load(self) -> ActivationState:
        """The state the file holds (an empty one when it holds nothing usable)."""
        document = self._read_document()
        fingerprint = document.get("deployment_fingerprint")
        return ActivationState(
            deployment_fingerprint=fingerprint if isinstance(fingerprint, str) else None,
            credential=_parse_credential(document.get("credential")),
            pending=_parse_pending(document.get("pending")),
        )

    def save(self, state: ActivationState) -> None:
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

    def _read_document(self) -> dict[str, object]:
        try:
            document = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return document if isinstance(document, dict) else {}


def _document_for(state: ActivationState) -> dict[str, object]:
    document: dict[str, object] = {}
    if state.deployment_fingerprint is not None:
        document["deployment_fingerprint"] = state.deployment_fingerprint
    if state.credential is not None:
        document["credential"] = {
            "token": state.credential.token,
            "token_id": str(state.credential.token_id),
            "control_plane_tenant_id": str(state.credential.control_plane_tenant_id),
            "expires_at": state.credential.expires_at.isoformat(),
        }
    if state.pending is not None:
        document["pending"] = {
            "request_id": str(state.pending.request_id),
            "verifier": state.pending.verifier,
            "local_tenant_id": str(state.pending.local_tenant_id),
            "local_user_id": str(state.pending.local_user_id),
            "expires_at": state.pending.expires_at.isoformat(),
        }
    return document


def _parse_credential(value: object) -> ActivationCredential | None:
    if not isinstance(value, dict):
        return None
    try:
        return ActivationCredential(
            token=_text(value, "token"),
            token_id=UUID(_text(value, "token_id")),
            control_plane_tenant_id=UUID(_text(value, "control_plane_tenant_id")),
            expires_at=_timestamp(value, "expires_at"),
        )
    except (KeyError, ValueError, TypeError):
        return None


def _parse_pending(value: object) -> PendingActivation | None:
    if not isinstance(value, dict):
        return None
    try:
        return PendingActivation(
            request_id=UUID(_text(value, "request_id")),
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
