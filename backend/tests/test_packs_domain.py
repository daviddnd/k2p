"""Pack-release domain rules on the execution plane (pure; no I/O).

Three properties the whole pack channel rests on, pinned here:

- **the digest is a wire contract.** ``payload_sha256`` is computed by the
  vendor service and recomputed here; the known answer below is the same one
  the control plane's ``app.domain.packs`` test asserts, so a change to
  either canonical form fails on the side that changed rather than at a
  customer's deployment;
- **a release is parsed strictly** — unknown fields, unknown types, and
  malformed values are refused rather than ignored, like every other
  document this deployment receives from the vendor;
- **nothing is installed unchecked**: a payload that does not hash to its
  digest is refused, and a release carrying a signature is refused unless the
  signature verifies against the deployment's pinned vendor public key
  (Ed25519 over the release digest — the other half of the seam is the
  control plane's signing key, and a signed release with no pinned key is
  refused rather than trusted).
"""

import base64
import json
from datetime import UTC, datetime

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from app.domain.entitlement import LicenseState, Tier
from app.domain.packs import (
    PackRefusal,
    PackRefused,
    PackRelease,
    PackReleaseInvalid,
    PackType,
    RefreshAction,
    pack_refusal_reason,
    parse_pack_release,
    payload_sha256,
    verify_release,
    verify_signature,
)

# The digest of ``KNOWN_PAYLOAD`` as the control plane computes it — the
# other half of this assertion lives in ``control_plane/tests/test_packs_domain.py``.
KNOWN_PAYLOAD: dict[str, object] = {
    "rules": [{"code": "nar1_due", "offset_days": 42}],
    "forms": {"nar1": {"mapping": {"items": []}}},
}
KNOWN_DIGEST = "cd95d1a33672a750b8d3e645a799ad312bb4e0b28a1088dad2ee616bf1a18902"


def release_document(**overrides: object) -> dict[str, object]:
    document: dict[str, object] = {
        "pack_type": "deadline_rules",
        "version": 3,
        "sha256": KNOWN_DIGEST,
        "payload": KNOWN_PAYLOAD,
        "published_at": datetime.now(UTC).isoformat(),
        "published_by": "vendor_ops",
        "signature": None,
    }
    document.update(overrides)
    return document


def test_the_digest_is_the_reviewed_wire_contract() -> None:
    assert payload_sha256(KNOWN_PAYLOAD) == KNOWN_DIGEST
    assert payload_sha256({"a": 1}) == (
        "015abd7f5cc57a2dd94b7590f04ad8084273905ee33ec5cebeae62276a97f862"
    )
    # JSONB does not preserve key order and the wire may escape non-ASCII;
    # neither may move the digest.
    assert payload_sha256({"b": 1, "a": "陳大文"}) == payload_sha256({"a": "陳大文", "b": 1})


def test_a_well_formed_release_parses() -> None:
    release = parse_pack_release(release_document())

    assert release.pack_type is PackType.DEADLINE_RULES
    assert release.version == 3
    assert release.sha256 == KNOWN_DIGEST
    assert release.payload == KNOWN_PAYLOAD
    assert release.signature is None


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"notes": "hello"}, id="unknown-field"),
        pytest.param({"pack_type": "mapping_bundle"}, id="unknown-pack-type"),
        pytest.param({"version": 0}, id="version-not-positive"),
        pytest.param({"version": "3"}, id="version-not-an-integer"),
        pytest.param({"sha256": "not-a-digest"}, id="sha256-malformed"),
        pytest.param({"payload": []}, id="payload-not-an-object"),
        pytest.param({"signature": "short"}, id="signature-malformed"),
        pytest.param({"published_at": "yesterday"}, id="published-at-unparsable"),
        pytest.param({"published_at": "2026-01-01T00:00:00"}, id="published-at-naive"),
    ],
)
def test_a_release_outside_the_shape_is_refused(overrides: dict[str, object]) -> None:
    with pytest.raises(PackReleaseInvalid) as refused:
        parse_pack_release(release_document(**overrides))

    # The refusal names a field or a kind, never a value.
    assert refused.value.reason


def test_a_missing_field_is_refused() -> None:
    document = release_document()
    del document["sha256"]

    with pytest.raises(PackReleaseInvalid, match="missing_field:sha256"):
        parse_pack_release(document)


def test_an_oversized_payload_is_refused() -> None:
    document = release_document(payload={"blob": ["f" * 4000] * 300})

    with pytest.raises(PackReleaseInvalid, match="payload_too_large"):
        parse_pack_release(document)


def test_a_payload_that_does_not_hash_to_its_digest_is_refused() -> None:
    release = parse_pack_release(release_document(sha256="0" * 64))

    with pytest.raises(PackRefused) as refused:
        verify_release(release)

    assert refused.value.reason is PackRefusal.SHA256_MISMATCH


# --- The signature seam: Ed25519 over the release digest -------------------
#
# The control plane signs the hex digest (``app.domain.packs.sign_release`` on
# the vendor side); these tests sign with ``cryptography`` directly, so a
# format drift between the two deployables' domain functions fails here rather
# than at a customer's deployment. The control plane's suite pins the same
# interop from the signing side.


def _keypair() -> tuple[Ed25519PrivateKey, Ed25519PublicKey]:
    private = Ed25519PrivateKey.generate()
    return private, private.public_key()


def _vendor_signs(digest: str, private: Ed25519PrivateKey) -> str:
    """What the vendor's signer produces, built here without the vendor module."""
    return base64.b64encode(private.sign(digest.encode("ascii"))).decode("ascii")


def test_a_valid_signature_verifies_against_the_pinned_key() -> None:
    private, public = _keypair()
    signature = _vendor_signs(KNOWN_DIGEST, private)

    assert verify_signature(KNOWN_DIGEST, signature, public) is True


def test_a_signature_over_a_different_digest_does_not_verify() -> None:
    private, public = _keypair()
    signature = _vendor_signs(KNOWN_DIGEST, private)

    assert verify_signature("0" * 64, signature, public) is False


def test_a_signature_from_the_wrong_key_does_not_verify() -> None:
    signing_private, _ = _keypair()
    _, other_public = _keypair()
    signature = _vendor_signs(KNOWN_DIGEST, signing_private)

    assert verify_signature(KNOWN_DIGEST, signature, other_public) is False


def test_garbage_signature_bytes_do_not_raise() -> None:
    _, public = _keypair()

    assert verify_signature(KNOWN_DIGEST, "not base64!!", public) is False
    assert verify_signature(KNOWN_DIGEST, "", public) is False


def test_a_signed_release_installs_when_the_signature_verifies() -> None:
    private, public = _keypair()
    release = parse_pack_release(
        release_document(signature=_vendor_signs(KNOWN_DIGEST, private))
    )

    verify_release(release, public_key=public)


def test_a_signed_release_with_a_wrong_key_is_refused() -> None:
    signing_private, _ = _keypair()
    _, other_public = _keypair()
    release = parse_pack_release(
        release_document(signature=_vendor_signs(KNOWN_DIGEST, signing_private))
    )

    with pytest.raises(PackRefused) as refused:
        verify_release(release, public_key=other_public)

    assert refused.value.reason is PackRefusal.SIGNATURE_UNVERIFIABLE


def test_a_signed_release_with_no_pinned_key_is_refused() -> None:
    """Fail closed, unchanged: a signature nobody can check installs nothing.

    A deployment that has not pinned the vendor's public key is in exactly
    the position this refusal always described — the authorship claim is
    unverifiable to it, so the release is refused rather than trusted on
    its digest alone.
    """
    private, _unused_public = _keypair()
    release = parse_pack_release(
        release_document(signature=_vendor_signs(KNOWN_DIGEST, private))
    )

    with pytest.raises(PackRefused) as refused:
        verify_release(release)

    assert refused.value.reason is PackRefusal.SIGNATURE_UNVERIFIABLE


def test_a_signed_release_whose_payload_was_altered_is_refused() -> None:
    """The signature binds the digest; the digest binds the payload.

    An attacker who keeps the claimed digest but swaps the payload still
    fails the recompute below — the two checks close each other's gap.
    """
    private, public = _keypair()
    release = parse_pack_release(
        release_document(
            payload={"rules": [{"code": "nar1_due", "offset_days": 41}]},
            signature=_vendor_signs(KNOWN_DIGEST, private),
        )
    )

    with pytest.raises(PackRefused) as refused:
        verify_release(release, public_key=public)

    assert refused.value.reason is PackRefusal.SHA256_MISMATCH


def test_an_unsigned_release_still_installs_on_its_digest_alone() -> None:
    """The pinned key changes nothing about releases that carry no signature."""
    _, public = _keypair()

    verify_release(parse_pack_release(release_document()), public_key=public)


def test_a_cached_release_is_accepted_after_a_round_trip() -> None:
    """What a stored payload is re-checked against: its own recorded digest."""
    release = PackRelease(
        pack_type=PackType.PROMPT_PACK,
        version=1,
        sha256=payload_sha256({"prompts": [{"body": "陳大文", "name": "reply_parser"}]}),
        payload=json.loads(json.dumps({"prompts": [{"body": "陳大文", "name": "reply_parser"}]})),
        published_at=datetime.now(UTC),
    )

    verify_release(release)


def test_only_a_licence_without_packs_stops_the_refresh() -> None:
    """Two ways to lose the capability, each with its own reason."""
    assert pack_refusal_reason(tier=Tier.STANDARD, license_state=LicenseState.ACTIVE) is None
    assert pack_refusal_reason(tier=Tier.STANDARD, license_state=LicenseState.GRACE) is None
    assert (
        pack_refusal_reason(tier=Tier.ENTERPRISE_OFFLINE, license_state=LicenseState.ACTIVE)
        is None
    )
    assert (
        pack_refusal_reason(tier=Tier.STANDARD, license_state=LicenseState.FREE)
        == "license_state_free"
    )
    assert (
        pack_refusal_reason(tier=Tier.FREE, license_state=LicenseState.ACTIVE)
        == "tier_not_licensed"
    )
    # Unknown values fail closed, with the tier the reason.
    assert (
        pack_refusal_reason(tier="premium", license_state=LicenseState.ACTIVE)
        == "tier_not_licensed"
    )


def test_the_refresh_vocabulary_is_closed() -> None:
    assert {member.value for member in RefreshAction} == {
        "installed",
        "up_to_date",
        "not_published",
        "refused",
        "not_attempted",
    }
    assert {member.value for member in PackType} == {
        "form_mapping",
        "deadline_rules",
        "fee_schedule",
        "prompt_pack",
    }
