"""Licensed packs on the execution plane: what a release is, and what it takes
to install one (ADR-0012; contract §2.2 ``PackCache``).

The deployment's side of the pack channel. Three rules live here, because all
three have to hold before anything is written to the cache:

- **A release is only installed after its payload is checked.** ``verify``
  recomputes the SHA-256 over the canonical payload and refuses a mismatch —
  a corrupted or substituted body installs nothing and is audited as a
  refusal. The digest is the same wire contract the control plane computes
  (sorted keys, no insignificant whitespace, escaped non-ASCII); the two
  definitions must agree byte for byte, so a test on each side pins one
  known-answer digest.
- **An unverifiable signature is refused, never ignored.** The
  ``signature`` field is Ed25519 over the release digest, signed by the
  vendor at publish and checked here against this deployment's pinned copy
  of the vendor public key (the key is deployment configuration, passed in —
  this module stays pure). A release that *carries* a signature installs
  only if the signature verifies; with no pinned key, or a signature that
  fails, it is refused (``signature_unverifiable``) rather than trusted on
  its digest alone. A release with no signature still installs on the
  digest — corruption detection, unchanged since the channel opened.
- **The licence decides whether packs may refresh at all** (§2.2, §4.3):
  a Free tier never carried them and a lapsed lease has lost them, so either
  freezes what is cached — frozen content stays readable, because ADR-0012's
  safety line is that a lapsed customer is never locked out of what they
  already hold — and refresh resumes when the licence recovers.

Pure and framework-free per ADR-0006: no framework or I/O imports here.
"""

import base64
import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from app.domain.entitlement import LicenseState, Tier, coerce_tier

# The same bounds the control plane publishes under (``app.domain.packs``);
# a release outside them is one this build cannot have been sent.
MAX_PAYLOAD_BYTES = 1_048_576
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SIGNATURE = re.compile(r"^[A-Za-z0-9+/=_-]{40,256}$")
_RELEASE_KEYS = frozenset(
    {
        "pack_type",
        "version",
        "sha256",
        "payload",
        "published_at",
        "published_by",
        "signature",
    }
)


class PackType(StrEnum):
    """The licensed artifacts, in the order a refresh asks for them."""

    FORM_MAPPING = "form_mapping"
    DEADLINE_RULES = "deadline_rules"
    FEE_SCHEDULE = "fee_schedule"
    PROMPT_PACK = "prompt_pack"


class RefreshAction(StrEnum):
    """What one refresh did about one pack type — the report's vocabulary.

    ``not_published`` is not a failure: a vendor who has never shipped a fee
    schedule is the normal state of a young catalog. ``not_attempted`` is
    what the remaining types read as once the control plane stops answering,
    so a report never implies a check that did not happen.
    """

    INSTALLED = "installed"
    UP_TO_DATE = "up_to_date"
    NOT_PUBLISHED = "not_published"
    REFUSED = "refused"
    NOT_ATTEMPTED = "not_attempted"


class PackRefusal(StrEnum):
    """Why a downloaded release was not installed."""

    # The payload does not hash to the digest the release carries.
    SHA256_MISMATCH = "sha256_mismatch"
    # The release carries a signature that does not verify against the
    # deployment's pinned vendor public key — or no key is pinned at all, in
    # which case every signed release lands here (fail closed).
    SIGNATURE_UNVERIFIABLE = "signature_unverifiable"
    # The release is not this wire's shape at all.
    MALFORMED_RELEASE = "malformed_release"
    # A version already cached with a different digest: the vendor re-cut a
    # release, and this deployment will not silently swap content under a
    # version it has already read.
    VERSION_CONFLICT = "version_conflict"


class PackReleaseInvalid(Exception):
    """The downloaded body is not a release this build understands.

    ``reason`` is a stable machine code naming a field (never its value).
    """

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class PackRefused(Exception):
    """A release was downloaded but not installed, for one typed reason."""

    def __init__(self, reason: PackRefusal) -> None:
        self.reason = reason
        super().__init__(reason.value)


@dataclass(frozen=True)
class PackRelease:
    """One release as the control plane served it."""

    pack_type: PackType
    version: int
    sha256: str
    payload: Mapping[str, object]
    published_at: datetime
    signature: str | None = None


def canonical_payload(payload: Mapping[str, object]) -> bytes:
    """The canonical JSON the release digest is computed over (wire contract)."""
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    ).encode("utf-8")


def payload_sha256(payload: Mapping[str, object]) -> str:
    """The hex digest a payload must hash to before it is installed."""
    return hashlib.sha256(canonical_payload(payload)).hexdigest()


def parse_pack_release(document: Mapping[str, object]) -> PackRelease:
    """The release a download body describes, or a refusal naming the field.

    Strict about the field set, like every other document this deployment
    receives from the vendor: a field the transport does not define has not
    been checked against anything, so it is refused rather than ignored.
    """
    unknown = sorted(set(document) - _RELEASE_KEYS)
    if unknown:
        raise PackReleaseInvalid(f"unknown_field:{unknown[0]}")
    for field in ("pack_type", "version", "sha256", "payload", "published_at"):
        if field not in document:
            raise PackReleaseInvalid(f"missing_field:{field}")
    try:
        pack_type = PackType(str(document["pack_type"]))
    except ValueError:
        raise PackReleaseInvalid("unknown_pack_type") from None
    version = document["version"]
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise PackReleaseInvalid("invalid_field:version")
    digest = document["sha256"]
    if not isinstance(digest, str) or _SHA256.match(digest) is None:
        raise PackReleaseInvalid("invalid_field:sha256")
    payload = document["payload"]
    if not isinstance(payload, Mapping):
        raise PackReleaseInvalid("invalid_field:payload")
    signature = document.get("signature")
    if signature is not None and (
        not isinstance(signature, str) or _SIGNATURE.match(signature) is None
    ):
        raise PackReleaseInvalid("invalid_field:signature")
    published_at = document["published_at"]
    if not isinstance(published_at, str):
        raise PackReleaseInvalid("invalid_field:published_at")
    try:
        published = datetime.fromisoformat(published_at)
    except ValueError:
        raise PackReleaseInvalid("invalid_field:published_at") from None
    if published.tzinfo is None:
        raise PackReleaseInvalid("invalid_field:published_at")
    if len(canonical_payload(payload)) > MAX_PAYLOAD_BYTES:
        raise PackReleaseInvalid("payload_too_large")
    return PackRelease(
        pack_type=pack_type,
        version=version,
        sha256=digest,
        payload=payload,
        published_at=published,
        signature=signature,
    )


def verify_signature(digest: str, signature: str, public_key: Ed25519PublicKey) -> bool:
    """Whether ``signature`` is the vendor's Ed25519 signature over ``digest``.

    The control plane signs the digest's ASCII bytes at publish
    (``sign_release`` on the vendor side); anything that is not a valid
    signature — a different digest, a different key, garbage bytes — is a
    plain ``False``, never an exception the install path has to handle.
    """
    try:
        public_key.verify(base64.b64decode(signature, validate=True), digest.encode("ascii"))
    except (InvalidSignature, ValueError, TypeError):
        return False
    return True


def verify_release(release: PackRelease, public_key: Ed25519PublicKey | None = None) -> None:
    """Check a release before it is installed; raises on anything unchecked.

    Order matters: an unverifiable signature is refused before the digest is
    even considered, because a signature is a claim about authorship and
    installing signed content without checking that claim is the failure mode
    this refusal exists to prevent. ``public_key`` is this deployment's
    pinned vendor public key — ``None`` (the default) means none is pinned,
    and then any signed release is refused rather than trusted.
    """
    if release.signature is not None and (
        public_key is None or not verify_signature(release.sha256, release.signature, public_key)
    ):
        raise PackRefused(PackRefusal.SIGNATURE_UNVERIFIABLE)
    if payload_sha256(release.payload) != release.sha256:
        raise PackRefused(PackRefusal.SHA256_MISMATCH)


def pack_refusal_reason(*, tier: Tier | str, license_state: LicenseState) -> str | None:
    """Why licensed packs may not refresh, or ``None`` when they may.

    Two ways the licence can fail this deployment, with their own codes
    because they are different facts about the customer:

    - ``license_state_free`` — the lease lapsed through §4.3, so the packs
      already cached are frozen (this is the downgrade ADR-0012 describes);
    - ``tier_not_licensed`` — the tier itself is Free, so licensed packs never
      were part of it (and, on a downgrade, the vendor's own tier moving to
      Free is what this deployment sees first).

    ``grace`` is licensed: the grace window keeps every capability, exactly
    as the capability set does. An unknown tier fails closed.
    """
    if license_state is LicenseState.FREE:
        return "license_state_free"
    if coerce_tier(tier) is Tier.FREE:
        return "tier_not_licensed"
    return None
