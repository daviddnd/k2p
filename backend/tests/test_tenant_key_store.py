"""Tests for the tenant-key store and the AES-GCM seam (Phase 7 slice D1).

Custody is a security property, so it is pinned where it would otherwise be
invisible: the file is owner-only, a present key is never regenerated, a
malformed key fails closed instead of being silently replaced, and the key
material never surfaces in an envelope, an exception string, or anywhere off
the filesystem. The nonce discipline (a fresh ``os.urandom(12)`` per seal)
is pinned by sealing the same plaintext twice and demanding two blobs.
"""

import base64
import hashlib
import json
import stat

import pytest

from app.adapters.secrets.tenant_keys import (
    TenantKeyStore,
    open_envelope,
    seal,
)
from app.domain.encryption import (
    KEY_MATERIAL_INVALID,
    KEY_UNKNOWN,
    PURPOSE_MISMATCH,
    EncryptionRefused,
    KeyPurpose,
    parse_envelope,
)

PLAINTEXT = "the TOTP seed, had this a real consumer"


def test_a_missing_key_is_created_owner_only(tmp_path) -> None:
    store = TenantKeyStore(tmp_path / "nested" / "tenant-key")

    key, created = store.load_or_create()

    assert created is True
    assert len(key.key) == 32
    assert key.key_id == hashlib.sha256(key.key).hexdigest()[:16]
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
    assert stat.S_IMODE(store.path.parent.stat().st_mode) == 0o700
    # The file holds exactly the key material — 32 bytes, nothing wrapped
    # around it.
    assert store.path.read_bytes() == key.key


def test_a_present_key_is_read_never_regenerated(tmp_path) -> None:
    store = TenantKeyStore(tmp_path / "tenant-key")
    first, created_first = store.load_or_create()
    second, created_second = store.load_or_create()

    assert created_first is True
    assert created_second is False
    assert second == first
    assert store.path.read_bytes() == first.key


def test_a_malformed_key_fails_closed_and_is_not_regenerated(tmp_path) -> None:
    store = TenantKeyStore(tmp_path / "tenant-key")
    store.path.write_bytes(b"only-sixteen-by")

    with pytest.raises(EncryptionRefused) as excinfo:
        store.load_or_create()

    assert excinfo.value.reason == KEY_MATERIAL_INVALID
    # The damaged key is left untouched for the operator to fix — never
    # silently overwritten by a fresh one.
    assert store.path.read_bytes() == b"only-sixteen-by"


def test_every_seal_draws_a_fresh_nonce(tmp_path) -> None:
    store = TenantKeyStore(tmp_path / "tenant-key")
    key, _ = store.load_or_create()

    first = seal(key, purpose=KeyPurpose.TOTP_SECRET, plaintext=PLAINTEXT)
    second = seal(key, purpose=KeyPurpose.TOTP_SECRET, plaintext=PLAINTEXT)

    assert first != second
    first_envelope = parse_envelope(first)
    second_envelope = parse_envelope(second)
    assert first_envelope.nonce != second_envelope.nonce
    assert first_envelope.ciphertext != second_envelope.ciphertext


def test_a_sealed_envelope_round_trips_under_its_purpose(tmp_path) -> None:
    store = TenantKeyStore(tmp_path / "tenant-key")
    key, _ = store.load_or_create()

    envelope = seal(key, purpose=KeyPurpose.TOTP_SECRET, plaintext=PLAINTEXT)

    assert open_envelope(key, purpose="totp_secret", envelope=envelope) == PLAINTEXT
    parsed = parse_envelope(envelope)
    assert parsed.key_id == key.key_id
    assert parsed.purpose == "totp_secret"


def test_a_flipped_purpose_refuses_before_any_primitive_runs(tmp_path) -> None:
    # AAD binding, end to end: the purpose field is authenticated data, so
    # flipping it is refused as purpose_mismatch — classification, not a
    # decryption attempt.
    store = TenantKeyStore(tmp_path / "tenant-key")
    key, _ = store.load_or_create()
    document = json.loads(seal(key, purpose=KeyPurpose.TOTP_SECRET, plaintext=PLAINTEXT))
    document["purpose"] = "something_else"

    with pytest.raises(EncryptionRefused) as excinfo:
        open_envelope(key, purpose="totp_secret", envelope=json.dumps(document))
    assert excinfo.value.reason == PURPOSE_MISMATCH


def test_a_flipped_key_id_refuses_as_key_unknown(tmp_path) -> None:
    store = TenantKeyStore(tmp_path / "tenant-key")
    key, _ = store.load_or_create()
    document = json.loads(seal(key, purpose=KeyPurpose.TOTP_SECRET, plaintext=PLAINTEXT))
    document["key_id"] = "0" * 16

    with pytest.raises(EncryptionRefused) as excinfo:
        open_envelope(key, purpose="totp_secret", envelope=json.dumps(document))
    assert excinfo.value.reason == KEY_UNKNOWN


def test_a_tampered_envelope_refuses_to_open(tmp_path) -> None:
    store = TenantKeyStore(tmp_path / "tenant-key")
    key, _ = store.load_or_create()
    envelope = seal(key, purpose=KeyPurpose.TOTP_SECRET, plaintext=PLAINTEXT)

    document = json.loads(envelope)
    raw = bytearray(base64.b64decode(document["ciphertext_b64"]))
    raw[0] ^= 0x01
    document["ciphertext_b64"] = base64.b64encode(bytes(raw)).decode("ascii")

    with pytest.raises(EncryptionRefused) as excinfo:
        open_envelope(key, purpose="totp_secret", envelope=json.dumps(document))
    assert excinfo.value.reason == "envelope_malformed"


def test_an_envelope_sealed_under_another_key_is_key_unknown(tmp_path) -> None:
    first_store = TenantKeyStore(tmp_path / "first" / "tenant-key")
    other_store = TenantKeyStore(tmp_path / "other" / "tenant-key")
    sealing_key, _ = first_store.load_or_create()
    foreign_key, _ = other_store.load_or_create()
    envelope = seal(sealing_key, purpose=KeyPurpose.TOTP_SECRET, plaintext=PLAINTEXT)

    with pytest.raises(EncryptionRefused) as excinfo:
        open_envelope(foreign_key, purpose="totp_secret", envelope=envelope)
    assert excinfo.value.reason == KEY_UNKNOWN


def test_key_material_never_appears_in_an_exception(tmp_path) -> None:
    store = TenantKeyStore(tmp_path / "tenant-key")
    key, _ = store.load_or_create()
    envelope = seal(key, purpose=KeyPurpose.TOTP_SECRET, plaintext=PLAINTEXT)

    exceptions: list[EncryptionRefused] = []
    try:
        open_envelope(key, purpose="totp_secret", envelope=envelope[:-2] + "AA")
    except EncryptionRefused as exc:
        exceptions.append(exc)
    broken = TenantKeyStore(tmp_path / "broken" / "tenant-key")
    broken.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    broken.path.write_bytes(b"bad")
    try:
        broken.load_or_create()
    except EncryptionRefused as exc:
        exceptions.append(exc)

    assert len(exceptions) == 2
    for material in (key.key.hex(), base64.b64encode(key.key).decode("ascii")):
        assert all(material not in str(exc) for exc in exceptions)
