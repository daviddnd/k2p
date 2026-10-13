"""Per-tenant envelope encryption — the pure rulebook (Phase 7 slice D1; scope §8).

Single owner of:

- the **envelope grammar** — one canonical-JSON shape, versioned, exact key
  set: ``{version, key_id, purpose, nonce_b64, ciphertext_b64}`` (``version``
  is the integer ``1`` — ``1.0`` and ``true`` are different JSON shapes and
  refuse). Serialize and parse live here; ``parse_envelope`` fails closed on
  anything else — wrong version, missing or extra keys, non-string fields,
  bad base64, a nonce that is not the AES-GCM 12 bytes, an empty ciphertext.
  ``purpose`` parses as the free string the envelope carries; whether it is
  a purpose anyone may open *for* is not a grammar question — it is the
  classification's (``purpose_mismatch``), so a tampered purpose refuses
  with the reason that names the binding, not a shape error;
- the **key-material rule** — a tenant key is exactly 32 bytes (AES-256);
  anything else is ``key_material_invalid``, never coerced, never regenerated
  over;
- the **AAD construction rule** — ``purpose ":" key_id``, UTF-8, bound into
  every seal and open: a sealed blob refuses to open under a different
  purpose or key id, because the tag was computed over this exact string;
- the **purpose vocabulary** (``KeyPurpose``) — closed; ``totp_secret`` is
  the only member today (E1-E3, TOTP MFA). The §1 protected class
  (client-data encryption) is deliberately absent — ruling R2; adding a
  member is a legal-review act, not a code edit;
- the **open-failure classification** (``classify_open`` + the named reason
  constants) — the pure table the adapter executes, so every refusal is one
  of a closed vocabulary rather than an ad-hoc string.

No ciphertext is produced or consumed here: AES-GCM is the adapter's
primitive. Pure and framework-free per ADR-0006.
"""

import base64
import binascii
import hashlib
import json
from dataclasses import dataclass
from enum import StrEnum

# The envelope grammar, versioned. Bumping ENVELOPE_VERSION is a breaking
# act: older envelopes stay parseable only under the version that wrote them.
ENVELOPE_VERSION = 1

KEY_BYTES = 32
NONCE_BYTES = 12  # the 96-bit nonce AES-GCM is specified for
KEY_ID_CHARS = 16

# The closed refusal vocabulary. These strings are the API of this module's
# failure mode: routes map them, tests pin them, nothing else is ever raised.
ENVELOPE_MALFORMED = "envelope_malformed"
PURPOSE_MISMATCH = "purpose_mismatch"
KEY_UNKNOWN = "key_unknown"
KEY_INACTIVE = "key_inactive"
KEY_MATERIAL_INVALID = "key_material_invalid"


class EncryptionRefused(Exception):
    """One of the named refusals above; ``reason`` is the closed vocabulary."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class KeyPurpose(StrEnum):
    """The closed purpose vocabulary bound into every envelope's AAD.

    A purpose scopes what a sealed blob may be opened for; opening under a
    different purpose is a refusal, not a decryption attempt. ``totp_secret``
    is the only member today; the §1 protected class stays unbuilt (ruling
    R2), so client data has no purpose here and no path through this seam.
    """

    TOTP_SECRET = "totp_secret"


@dataclass(frozen=True)
class Envelope:
    """A parsed envelope: the bytes the adapter feeds to and from AES-GCM.

    ``purpose`` is the string the envelope carries — validated as a string,
    not against the vocabulary (that is ``classify_open``'s job).
    """

    key_id: str
    purpose: str
    nonce: bytes
    ciphertext: bytes


def require_key_material(key: bytes) -> bytes:
    """The only acceptable key material: exactly 32 bytes, returned as given."""
    if len(key) != KEY_BYTES:
        raise EncryptionRefused(KEY_MATERIAL_INVALID)
    return key


def key_id_for(key: bytes) -> str:
    """The key's public identity: SHA-256 hex, first 16 chars.

    Safe to record, log, and store — it identifies the key without revealing
    it; finding the key from the id is a preimage search.
    """
    require_key_material(key)
    return hashlib.sha256(key).hexdigest()[:KEY_ID_CHARS]


def aad_for(purpose: str, key_id: str) -> bytes:
    """The additional authenticated data: ``purpose ":" key_id``, UTF-8.

    ``purpose`` is the string value — a ``KeyPurpose`` member's value seals,
    the parsed string opens; the construction is identical both sides, so
    the tag binds the blob to exactly this purpose and key id.
    """
    return f"{purpose}:{key_id}".encode()


def serialize_envelope(
    *, key_id: str, purpose: KeyPurpose, nonce: bytes, ciphertext: bytes
) -> str:
    """The canonical envelope JSON: fixed key set, sorted keys, no whitespace."""
    if len(nonce) != NONCE_BYTES:
        raise EncryptionRefused(ENVELOPE_MALFORMED)
    if not ciphertext:
        raise EncryptionRefused(ENVELOPE_MALFORMED)
    document = {
        "version": ENVELOPE_VERSION,
        "key_id": key_id,
        "purpose": purpose.value,
        "nonce_b64": base64.b64encode(nonce).decode("ascii"),
        "ciphertext_b64": base64.b64encode(ciphertext).decode("ascii"),
    }
    return json.dumps(document, sort_keys=True, separators=(",", ":"))


def parse_envelope(text: str) -> Envelope:
    """Parse one envelope, failing closed with ``envelope_malformed``.

    The check order is deliberate: structure first (JSON shape, version, key
    set), then encodings (base64), then sizes (nonce, ciphertext). Any
    deviation from the grammar refuses; nothing is guessed, padded, or
    defaulted.
    """
    try:
        document = json.loads(text)
    except ValueError as exc:
        raise EncryptionRefused(ENVELOPE_MALFORMED) from exc
    if not isinstance(document, dict):
        raise EncryptionRefused(ENVELOPE_MALFORMED)
    expected_keys = {"version", "key_id", "purpose", "nonce_b64", "ciphertext_b64"}
    if set(document) != expected_keys:
        raise EncryptionRefused(ENVELOPE_MALFORMED)
    # JSON's ``1``, ``1.0``, and ``true`` compare equal in Python; only the
    # integer shape is this grammar's version.
    version = document["version"]
    if isinstance(version, bool) or not isinstance(version, int) or version != ENVELOPE_VERSION:
        raise EncryptionRefused(ENVELOPE_MALFORMED)
    key_id = document["key_id"]
    purpose_value = document["purpose"]
    if not isinstance(key_id, str) or not key_id:
        raise EncryptionRefused(ENVELOPE_MALFORMED)
    if not isinstance(purpose_value, str) or not purpose_value:
        raise EncryptionRefused(ENVELOPE_MALFORMED)
    nonce = _b64(document["nonce_b64"])
    ciphertext = _b64(document["ciphertext_b64"])
    if len(nonce) != NONCE_BYTES:
        raise EncryptionRefused(ENVELOPE_MALFORMED)
    if not ciphertext:
        raise EncryptionRefused(ENVELOPE_MALFORMED)
    return Envelope(
        key_id=key_id, purpose=purpose_value, nonce=nonce, ciphertext=ciphertext
    )


def classify_open(
    *,
    envelope_purpose: str,
    expected_purpose: str,
    envelope_key_id: str,
    known_key_ids: frozenset[str],
    active_key_id: str,
) -> str | None:
    """The pure open-failure table; ``None`` means the open may proceed.

    Order matters: the purpose binding is checked before any key material
    question, so a blob opened for the wrong purpose refuses as
    ``purpose_mismatch`` — including a purpose string outside the
    ``KeyPurpose`` vocabulary, which no typed caller can request — without
    revealing whether its key is known. A key id the deployment has never
    held is ``key_unknown``; an id that is known but no longer active is
    ``key_inactive`` — the rotation vocabulary E3's re-encryption path
    needs. Today the deployment holds one key, so ``known_key_ids`` is
    ``{active_key_id}`` and ``key_inactive`` fires only once a rotation keeps
    prior generations readable.
    """
    if envelope_purpose != expected_purpose:
        return PURPOSE_MISMATCH
    if envelope_key_id not in known_key_ids:
        return KEY_UNKNOWN
    if envelope_key_id != active_key_id:
        return KEY_INACTIVE
    return None


def _b64(value: object) -> bytes:
    if not isinstance(value, str):
        raise EncryptionRefused(ENVELOPE_MALFORMED)
    try:
        return base64.b64decode(value.encode("ascii"), validate=True)
    except (UnicodeEncodeError, binascii.Error, ValueError) as exc:
        raise EncryptionRefused(ENVELOPE_MALFORMED) from exc
