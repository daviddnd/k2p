"""The per-tenant key: custody and the AES-GCM primitive (Phase 7 slice D1).

Custody, per the activation-token precedent and ADR-0014: one file holding
exactly 32 bytes of key material, mode 0600, in a gitignored path under the
service's working directory (``AICS_TENANT_KEY_FILE``), never in the
database (a dump, a replica, or a backup handed to an operator would carry
it), never in the image, never in a log or an audit entry — the audit chain
records only the key's id. The parent directory is created 0700, and the
write is atomic (a temporary sibling renamed into place), so a crash never
leaves a half-written key. The file must live on local, owner-only storage:
symlinked key files are outside this threat model — the write path is
safe-by-rename (the temporary sibling is created and renamed inside the
credential directory, so a symlinked target cannot be written through), and
a local single-tenant deployment does not defend against its own
administrator pointing the path elsewhere (the credential-store parity).

``load_or_create`` semantics: a missing file gets a fresh random key; a
present file is read and validated — exactly 32 bytes, anything else fails
closed with ``key_material_invalid``, never silently regenerated over. A
damaged key is left in place for the operator to fix, because a deployment
that overwrote its own key would render every envelope sealed under it
unreadable anyway.

Nonce discipline: every seal draws a fresh 12-byte nonce from ``os.urandom``
(the OS CSPRNG), never derived from the plaintext, the key, or a counter —
reuse is therefore a fresh-draw collision, not a programming error. A random
96-bit nonce under a single random 32-byte key is safe to roughly 2^32
seals (the AES-GCM random-nonce birthday bound); that is orders of magnitude
beyond this deployment's lifetime volume, and the key's rotation story below
resets the bound long before it matters.

Rotation (v1 ruling — one active key per tenant): rotating overwrites the
key file, and envelopes sealed under the older key become unreadable. That
is why rotation is never a bare overwrite here: re-encrypting every consumer
is the caller's explicit act, done through the application seam while the
old key still opens (E3 owns TOTP re-encryption at rotation time). The
pieces the re-encryption needs are all on this module: ``decrypt_for_tenant``
/ ``open_envelope`` classify a stale envelope's key id as ``key_unknown`` or
``key_inactive`` (the domain's table), so a caller that kept the previous
generation readable can still open, re-seal, and only then drop it.

The module executes the domain's rulebook (``app.domain.encryption``): the
envelope grammar, the key-material rule, the AAD construction, and the
open-failure classification all live there. Key material never appears in an
exception, a log line, or anything returned to a caller.
"""

import os
from dataclasses import dataclass
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.domain.encryption import (
    ENVELOPE_MALFORMED,
    NONCE_BYTES,
    EncryptionRefused,
    KeyPurpose,
    aad_for,
    classify_open,
    key_id_for,
    parse_envelope,
    require_key_material,
    serialize_envelope,
)

# Owner-only, like the other credential files: the key protects every
# at-rest secret this deployment seals.
FILE_MODE = 0o600
DIRECTORY_MODE = 0o700

_TEMPORARY_SUFFIX = ".tmp"


@dataclass(frozen=True)
class TenantKey:
    """One tenant's active key: the material and its public id."""

    key: bytes
    key_id: str


@dataclass(frozen=True)
class TenantKeyStore:
    """The key file at ``path``, read and created — never rewritten silently."""

    path: Path

    def load(self) -> TenantKey | None:
        """The tenant key, or ``None`` when no key file exists.

        A present file is read and validated — exactly 32 bytes, anything
        else fails closed with ``key_material_invalid``: a malformed key is
        never regenerated over, because the envelopes it sealed would be
        lost either way and silent overwrite would hide the damage.
        """
        if not self.path.exists():
            return None
        material = self.path.read_bytes()
        return TenantKey(key=require_key_material(material), key_id=key_id_for(material))

    def load_or_create(self) -> tuple[TenantKey, bool]:
        """The tenant key, and whether this call created it.

        Absent file: generate 32 random bytes, persist them atomically at
        mode 0600, return ``(key, True)``. Present file: read it, return
        ``(key, False)``. The malformed-key rule is ``load``'s: fail closed,
        leave the damaged file for the operator to fix.
        """
        existing = self.load()
        if existing is not None:
            return existing, False
        material = os.urandom(32)
        key = TenantKey(key=material, key_id=key_id_for(material))
        self._persist(material)
        return key, True

    def _persist(self, material: bytes) -> None:
        """Write the key atomically at mode 0600; a umask can only narrow it."""
        self.path.parent.mkdir(mode=DIRECTORY_MODE, parents=True, exist_ok=True)
        temporary = self.path.with_name(self.path.name + _TEMPORARY_SUFFIX)
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, FILE_MODE)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(material)
        os.replace(temporary, self.path)


def seal(key: TenantKey, *, purpose: KeyPurpose, plaintext: str) -> str:
    """Seal ``plaintext`` into one canonical envelope JSON string.

    The nonce is a fresh ``os.urandom(12)`` draw per seal — never reused,
    never derived from the plaintext (see the module docstring). The AAD is
    the domain rule ``purpose ":" key_id``, so the tag binds the blob to
    exactly this purpose and key.
    """
    aad = aad_for(purpose.value, key.key_id)
    nonce = os.urandom(NONCE_BYTES)
    ciphertext = AESGCM(key.key).encrypt(nonce, plaintext.encode("utf-8"), aad)
    return serialize_envelope(
        key_id=key.key_id, purpose=purpose, nonce=nonce, ciphertext=ciphertext
    )


def open_envelope(key: TenantKey, *, purpose: str, envelope: str) -> str:
    """Open one envelope sealed for ``purpose`` under ``key``.

    Refusals are the domain's closed vocabulary: a grammar violation is
    ``envelope_malformed``; an open under a different purpose is
    ``purpose_mismatch``; an envelope whose key id does not identify ``key``
    is ``key_unknown``. A failed tag — tampering, or a blob sealed under a
    different key id — is ``envelope_malformed``, the same refusal a grammar
    violation gets, so the error vocabulary never leaks which check failed.

    The id comparison is not a secret comparison: the key id is public (it
    is safe in audit entries), and with one active key per tenant an id
    mismatch is refused by ``classify_open`` before any primitive runs. The
    only secrets here — the key material and the plaintext — are never
    compared, only fed to the AEAD.
    """
    parsed = parse_envelope(envelope)
    reason = classify_open(
        envelope_purpose=parsed.purpose,
        expected_purpose=purpose,
        envelope_key_id=parsed.key_id,
        known_key_ids=frozenset({key.key_id}),
        active_key_id=key.key_id,
    )
    if reason is not None:
        raise EncryptionRefused(reason)
    try:
        plaintext = AESGCM(key.key).decrypt(
            parsed.nonce, parsed.ciphertext, aad_for(parsed.purpose, parsed.key_id)
        )
    except InvalidTag as exc:
        raise EncryptionRefused(ENVELOPE_MALFORMED) from exc
    try:
        return plaintext.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise EncryptionRefused(ENVELOPE_MALFORMED) from exc
