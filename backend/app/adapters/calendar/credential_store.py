"""The calendar provider credential, one file per tenant, mode 0600
(slice M8; ADR-0014 custody; ADR-0013's 2026-10-03 ruling R2).

A sibling of the mailbox store (``app.adapters.mail.credential_store``):
the custody *pattern* is duplicated deliberately — the file is written
atomically at mode 0600 inside a mode-0700 directory, the secret never
touches the database — while the *shape* is this concern's own. One provider
credential per tenant: the Google or Microsoft app registration the push
authenticates with, held as ``{tenant_id}.json`` under the directory
``AICS_CALENDAR_CREDENTIAL_DIR`` names (a gitignored path under the
deployment's working directory by default). The per-tenant file keeps the
store honest for the day the deployment stops being single-tenant, and makes
"one credential per tenant" answerable on the filesystem.

A missing file is a tenant that has not connected a calendar provider — the
push simply does not run for it. An unreadable or unparsable file reads as
no credential rather than raising: nothing in the statutory work depends on
this store, and a damaged credential is fixed by connecting again.

``clear`` removes the tenant's file outright rather than writing an empty
document: disconnecting is the operator asking the deployment to hold no
provider credential, and a file that holds no token is still a file holding
a redacted shape of one.

**The document's identity and timestamps.** Besides the credential fields,
each document carries an ``id`` (a UUID, minted on first save and preserved
across re-saves — the probe's flag update is a re-save, not a new credential)
and ``created_at`` / ``updated_at`` (UTC ISO instants, ``created_at``
likewise preserved). The Administrator read surfaces exactly those three as
the credential's metadata, and the audit chain names the id on connect and
disconnect — a custody mutation is recorded against an identity, never the
tokens. ``load`` reads the credential alone and accepts a document that
predates the metadata fields; ``load_stored`` requires them, so a legacy file
still answers "is a credential connected?" while the metadata read honestly
reports what it cannot know.
"""

import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

from app.domain.calendar_push import CalendarProviderName, ProviderCredential

# Owner-only, like the mailbox store: the refresh token is the tenant's
# credential against its own app registration.
FILE_MODE = 0o600
DIRECTORY_MODE = 0o700

_TEMPORARY_SUFFIX = ".tmp"


@dataclass(frozen=True)
class StoredCredential:
    """The credential plus the document's own identity and timestamps."""

    credential: ProviderCredential
    credential_id: UUID
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class CalendarCredentialStore:
    """The credential directory at ``directory``, one file per tenant."""

    directory: Path

    def path_for(self, tenant_id: UUID) -> Path:
        """The tenant's credential file."""
        return self.directory / f"{tenant_id}.json"

    def load(self, tenant_id: UUID) -> ProviderCredential | None:
        """The tenant's credential, or ``None`` when none is usable."""
        stored = self.load_stored(tenant_id)
        return None if stored is None else stored.credential

    def load_stored(self, tenant_id: UUID) -> StoredCredential | None:
        """The credential with its id and timestamps, or ``None``.

        A document without the metadata fields (written before they existed)
        reads as no credential here, while ``load`` above still accepts it:
        the sweep only needs the credential, and the management read must
        never invent metadata it does not hold.
        """
        document = self._read_document(tenant_id)
        if document is None:
            return None
        try:
            credential_id = UUID(_text(document, "id"))
            created_at = datetime.fromisoformat(_text(document, "created_at"))
            updated_at = datetime.fromisoformat(_text(document, "updated_at"))
        except (KeyError, ValueError, TypeError):
            return None
        credential = _parse_credential(document)
        if credential is None:
            return None
        return StoredCredential(
            credential=credential,
            credential_id=credential_id,
            created_at=created_at,
            updated_at=updated_at,
        )

    def save(self, tenant_id: UUID, credential: ProviderCredential) -> StoredCredential:
        """Write the tenant's credential at mode 0600, replacing whatever was there.

        The write goes to a temporary sibling and is then renamed into place,
        so a concurrent reader — or a crash — never observes a half-written
        credential file. The document's ``id`` and ``created_at`` survive the
        replacement (a probe's flag update is the same credential, and the
        audit chain's identity must not move under it); ``updated_at`` stamps
        this write.
        """
        self.directory.mkdir(mode=DIRECTORY_MODE, parents=True, exist_ok=True)
        path = self.path_for(tenant_id)
        now = datetime.now(UTC)
        credential_id = uuid4()
        created_at = now
        existing = self._read_document(tenant_id)
        if existing is not None:
            try:
                credential_id = UUID(_text(existing, "id"))
                created_at = datetime.fromisoformat(_text(existing, "created_at"))
            except (KeyError, ValueError, TypeError):
                pass  # a damaged legacy document loses its lineage, not the write
        stored = StoredCredential(
            credential=credential,
            credential_id=credential_id,
            created_at=created_at,
            updated_at=now,
        )
        temporary = path.with_name(path.name + _TEMPORARY_SUFFIX)
        # os.open applies FILE_MODE directly; a umask can only remove bits
        # from it, never widen it.
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, FILE_MODE)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(_document_for(stored), handle, sort_keys=True)
        os.replace(temporary, path)
        return stored

    def clear(self, tenant_id: UUID) -> None:
        """Forget the tenant's credential: the file is removed outright."""
        self.path_for(tenant_id).unlink(missing_ok=True)

    def _read_document(self, tenant_id: UUID) -> dict[str, object] | None:
        """The raw document when it parses to an object, else ``None``."""
        try:
            document = json.loads(self.path_for(tenant_id).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return document if isinstance(document, dict) else None


def _document_for(stored: StoredCredential) -> dict[str, object]:
    credential = stored.credential
    document: dict[str, object] = {
        "id": str(stored.credential_id),
        "provider": credential.provider.value,
        "client_id": credential.client_id,
        "client_secret": credential.client_secret,
        "refresh_token": credential.refresh_token,
        "calendar_id": credential.calendar_id,
        "probe_passed": credential.probe_passed,
        "created_at": stored.created_at.isoformat(),
        "updated_at": stored.updated_at.isoformat(),
    }
    if credential.directory_tenant_id is not None:
        document["directory_tenant_id"] = credential.directory_tenant_id
    return document


def _parse_credential(document: dict[str, object]) -> ProviderCredential | None:
    try:
        provider = CalendarProviderName(_text(document, "provider"))
        directory_tenant_id = document.get("directory_tenant_id")
        if directory_tenant_id is not None and not isinstance(directory_tenant_id, str):
            raise ValueError("directory_tenant_id")
        probe_passed = document.get("probe_passed", False)
        if not isinstance(probe_passed, bool):
            raise ValueError("probe_passed")
        credential = ProviderCredential(
            provider=provider,
            client_id=_text(document, "client_id"),
            client_secret=_text(document, "client_secret"),
            refresh_token=_text(document, "refresh_token"),
            calendar_id=_text(document, "calendar_id"),
            directory_tenant_id=directory_tenant_id,
            probe_passed=probe_passed,
        )
    except (KeyError, ValueError, TypeError):
        return None
    # Microsoft's registration is directory-scoped; the directory tenant id
    # is what the token request authenticates against. A document that omits
    # it is a Google shape, whatever the provider field claims.
    if credential.provider is CalendarProviderName.MICROSOFT and (
        credential.directory_tenant_id is None
    ):
        return None
    return credential


def _text(document: dict[str, object], field: str) -> str:
    value = document[field]
    if not isinstance(value, str) or not value:
        raise ValueError(field)
    return value
