"""Pure rules for per-tenant envelope encryption (Phase 7 slice D1; scope §8).

The domain owns the envelope grammar, the key-material rule, the AAD
construction, and the pure classification of open failures — the rulebook the
adapter executes. These tests pin the grammar's edges: the canonical
round-trip, every malformed-envelope refusal, and the classification table
(purpose binding, key-known vs key-active), all without a key ever touching
a cipher.
"""

import base64
import hashlib
import json

import pytest

from app.domain.encryption import (
    ENVELOPE_VERSION,
    KEY_INACTIVE,
    KEY_MATERIAL_INVALID,
    KEY_UNKNOWN,
    PURPOSE_MISMATCH,
    EncryptionRefused,
    KeyPurpose,
    aad_for,
    classify_open,
    key_id_for,
    parse_envelope,
    require_key_material,
    serialize_envelope,
)

KEY = bytes(range(32))
KEY_ID = hashlib.sha256(KEY).hexdigest()[:16]
NONCE = bytes(range(12))
CIPHERTEXT = b"\x01" * 28


def envelope_document(**overrides: object) -> dict[str, object]:
    document: dict[str, object] = {
        "version": ENVELOPE_VERSION,
        "key_id": KEY_ID,
        "purpose": KeyPurpose.TOTP_SECRET.value,
        "nonce_b64": base64.b64encode(NONCE).decode("ascii"),
        "ciphertext_b64": base64.b64encode(CIPHERTEXT).decode("ascii"),
    }
    document.update(overrides)
    return document


# --- key material ----------------------------------------------------------


def test_a_32_byte_key_is_the_only_acceptable_material() -> None:
    assert require_key_material(KEY) == KEY
    for length in (0, 16, 31, 33, 64):
        with pytest.raises(EncryptionRefused) as excinfo:
            require_key_material(bytes(length))
        assert excinfo.value.reason == KEY_MATERIAL_INVALID


def test_the_key_id_is_the_key_digest_prefix() -> None:
    assert key_id_for(KEY) == KEY_ID
    assert len(key_id_for(KEY)) == 16
    assert key_id_for(bytes(32)) != key_id_for(KEY)


# --- AAD construction ------------------------------------------------------


def test_the_aad_binds_purpose_and_key_id() -> None:
    assert aad_for(KeyPurpose.TOTP_SECRET, KEY_ID) == f"totp_secret:{KEY_ID}".encode()
    assert aad_for(KeyPurpose.TOTP_SECRET, KEY_ID) != aad_for(
        KeyPurpose.TOTP_SECRET, "0" * 16
    )


# --- envelope round-trip ---------------------------------------------------


def test_a_serialized_envelope_parses_back_to_itself() -> None:
    text = serialize_envelope(
        key_id=KEY_ID,
        purpose=KeyPurpose.TOTP_SECRET,
        nonce=NONCE,
        ciphertext=CIPHERTEXT,
    )
    envelope = parse_envelope(text)

    assert envelope.key_id == KEY_ID
    assert envelope.purpose == "totp_secret"
    assert envelope.nonce == NONCE
    assert envelope.ciphertext == CIPHERTEXT
    # Canonical form: fixed key set, sorted keys, no insignificant whitespace.
    assert json.loads(text) == envelope_document()
    assert serialize_envelope(
        key_id=envelope.key_id,
        purpose=KeyPurpose(envelope.purpose),
        nonce=envelope.nonce,
        ciphertext=envelope.ciphertext,
    ) == text


# --- malformed envelopes: every named refusal -------------------------------


def parse(document: object) -> None:
    text = document if isinstance(document, str) else json.dumps(document)
    parse_envelope(text)


@pytest.mark.parametrize(
    "document",
    [
        "not json at all",
        json.dumps(["a", "list"]),
        envelope_document(version=2),
        envelope_document(version="1"),
        envelope_document(version=1.0),  # JSON's ``1.0`` is not the integer 1
        envelope_document(version=True),  # Python: True == 1 — refused as a shape
        {k: v for k, v in envelope_document().items() if k != "nonce_b64"},
        {k: v for k, v in envelope_document().items() if k != "ciphertext_b64"},
        {k: v for k, v in envelope_document().items() if k != "purpose"},
        {**envelope_document(), "unexpected": "key"},
        envelope_document(key_id=""),
        envelope_document(key_id=123),
        envelope_document(purpose=123),
        envelope_document(purpose=""),
        envelope_document(nonce_b64="!!! not base64 !!!"),
        envelope_document(ciphertext_b64="!!! not base64 !!!"),
        envelope_document(nonce_b64=base64.b64encode(b"short").decode("ascii")),
        envelope_document(nonce_b64=base64.b64encode(bytes(16)).decode("ascii")),
        envelope_document(ciphertext_b64=base64.b64encode(b"").decode("ascii")),
    ],
)
def test_a_malformed_envelope_is_refused_with_the_named_reason(document: object) -> None:
    with pytest.raises(EncryptionRefused) as excinfo:
        parse(document)
    assert excinfo.value.reason == "envelope_malformed"


# --- open-failure classification (the pure table the adapter executes) ------


def test_an_openable_envelope_classifies_to_none() -> None:
    reason = classify_open(
        envelope_purpose="totp_secret",
        expected_purpose="totp_secret",
        envelope_key_id=KEY_ID,
        known_key_ids=frozenset({KEY_ID}),
        active_key_id=KEY_ID,
    )
    assert reason is None


def test_an_open_for_a_different_purpose_is_classified() -> None:
    reason = classify_open(
        envelope_purpose="totp_secret",
        expected_purpose="something_else",
        envelope_key_id=KEY_ID,
        known_key_ids=frozenset({KEY_ID}),
        active_key_id=KEY_ID,
    )
    assert reason == PURPOSE_MISMATCH


def test_an_unknown_purpose_string_is_a_mismatch_not_a_shape_error() -> None:
    # Purpose membership is the classification's question, not the grammar's:
    # a blob carrying a purpose no typed caller can request refuses as
    # purpose_mismatch, never envelope_malformed.
    reason = classify_open(
        envelope_purpose="not_a_purpose",
        expected_purpose="totp_secret",
        envelope_key_id=KEY_ID,
        known_key_ids=frozenset({KEY_ID}),
        active_key_id=KEY_ID,
    )
    assert reason == PURPOSE_MISMATCH


def test_an_unknown_key_classifies_before_active_key_matters() -> None:
    reason = classify_open(
        envelope_purpose="totp_secret",
        expected_purpose="totp_secret",
        envelope_key_id=KEY_ID,
        known_key_ids=frozenset(),
        active_key_id=KEY_ID,
    )
    assert reason == KEY_UNKNOWN


def test_a_known_but_inactive_key_is_classified_distinct_from_unknown() -> None:
    previous_id = "a" * 16
    reason = classify_open(
        envelope_purpose="totp_secret",
        expected_purpose="totp_secret",
        envelope_key_id=previous_id,
        known_key_ids=frozenset({previous_id}),
        active_key_id=KEY_ID,
    )
    assert reason == KEY_INACTIVE
