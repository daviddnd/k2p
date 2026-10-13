"""Prior-filing extraction domain rules (scope §2.1; data-model §2.7, §2.9
``ImportBatch``).

Single owner of:

- **The master-data target vocabulary** (``TARGETS``) — the registry paths
  this slice may propose for review and the write path each one takes:
  effective-dated facts through ``record_fact``, anchor statics through the
  Administrator-only statics correction, or read-only *context* (the
  filing's made-up date dates its records but is not master data). Targets
  are the contract §1 dot paths, one column each; composite registry
  targets and collections are not part of this slice.
- **The confidence ladder** — how a value was read (an exact AcroForm
  field, text found inside the pinned template's widget rectangle, or OCR
  over a scan) sets the base score, and honesty signals cap it: a split
  bilingual box, a repeated header field whose occurrences disagree, a read
  that does not normalise. The numbers are deliberately coarse: the review
  queue only needs the band, not a calibrated probability. For scanned
  filings the engine's own word confidence is the base, and **validation**
  (`value_is_validated`) is what opens the auto-accept band: a value whose
  kind has no machine check — free text, a tick read as a mark — stays in
  the staff-review band however sure the engine was.
- **Pure value normalisation** — the canonical stored form of a field
  value (text, BRN, integer, decimal, company type) and the bilingual
  name-box script split, shared by the extraction path (which drops values
  it cannot normalise) and the confirm path (which must not fail at the
  database).

Deliberately absent: any I/O, any widget or PDF knowledge. Widget names,
page geometry, and the option semantics of a checkbox group are form-
registry data and live in ``app.adapters``.

The §2.7 confidence bands and their routing are owned by
``app.domain.confidence`` — extraction is one of two consumers now, so the
rule no longer lives beside either of them. They are imported and
re-exported here, because that is the name the extraction callers already
know.

Pure and framework-free per ADR-0006: the import-linter contract forbids
framework and I/O imports in this layer.
"""

import re
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from enum import StrEnum

from app.domain.company_master import CompanyType, InvalidBrnError, normalize_brn

# Re-exported (``X as X``) for the extraction callers that already import the
# §2.7 bands from this module; the rule's home is app.domain.confidence.
from app.domain.confidence import AUTO_ACCEPT_THRESHOLD as AUTO_ACCEPT_THRESHOLD
from app.domain.confidence import STAFF_REVIEW_THRESHOLD as STAFF_REVIEW_THRESHOLD
from app.domain.confidence import ConfidenceBand as ConfidenceBand
from app.domain.confidence import band_for as band_for
from app.domain.confidence import overall_score as overall_score
from app.domain.confidence import routing_for as routing_for

# The confidence ladder. Exact AcroForm reads are the template's own data;
# text found inside a widget rectangle is inferred from glyph positions and
# never reaches the auto-accept band on its own.
CONFIDENCE_EXACT_FIELD = 0.99
CONFIDENCE_TEXT_MATCH = 0.70
# A bilingual name box split by script is a heuristic (a mixed-script token
# lands on the wrong side); a repeated header field whose 15 occurrences
# disagree means at least one page is wrong, so staff must look.
CONFIDENCE_BILINGUAL_SPLIT = 0.80
CONFIDENCE_REPEATED_DISAGREEMENT = 0.55
# The OCR rungs (E2). A scanned value the reader can machine-check scores on
# the engine's own word confidence, capped below an exact field read; a value
# it cannot check — free text, a tick found by ink rather than by glyph
# recognition — is capped into the staff-review band whatever the engine
# thought, and a scan that cannot be recognised is refused outright.
CONFIDENCE_OCR_VALIDATED = 0.90
CONFIDENCE_OCR_UNVALIDATED = 0.60
CONFIDENCE_OCR_CHECKMARK = 0.60
# A word the engine is less sure of than this is noise, not text: it is not
# proposed as part of a value at all.
MIN_OCR_WORD_CONFIDENCE = 0.30

_CJK_RANGES = ((0x3400, 0x4DBF), (0x4E00, 0x9FFF), (0xF900, 0xFAFF))
_DIGITS_PATTERN = re.compile(r"^[0-9]+$")
_BRN_PATTERN = re.compile(r"^[0-9]{8}$")


class RecordStatus(StrEnum):
    """``ImportBatch`` per-record status (scope §2.1; mirrors the DB check).

    Only ``confirmed`` records become authoritative master data.
    """

    PENDING = "pending"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"


class FormCode(StrEnum):
    """Extractable form registries, matching the contract's ``form_type``.

    The value is also the ``backend/forms/`` directory name; registration
    data itself carries the CR form code (``NAR1``) in ``manifest.json``.
    """

    NAR1 = "nar1"
    ND2A = "nd2a"


class InputShape(StrEnum):
    """Which digital shape a prior filing arrived in (scope §2.1, §7).

    ``needs_ocr`` is a first-class outcome, not an error: scans are the
    common shape, and the OCR path is a separate slice (E2). A document in
    this shape is never guessed at — no records, and the assessment says
    why.
    """

    ACROFORM = "acroform"
    TEXT_LAYER = "text_layer"
    NEEDS_OCR = "needs_ocr"


class AssessmentSubject(StrEnum):
    """§2.7 ``subject_type`` vocabulary (mirrors the DB check).

    The value names the table ``subject_id`` points into: a reader resolving
    an assessment has to land on a real row (``app.application.prior_filing``
    reads them by ``subject_id == <document id>``), so a subject type is only
    ever added together with the table it names. The vocabulary grows as
    subjects appear, the way ``AuditAction`` does.
    """

    CHANGE_EVENT = "change_event"
    COMMUNICATION = "communication"
    DOCUMENT = "document"
    # §2.5's quarantine record (migration 0016): a message that could not be
    # attributed has no Communication to be assessed against, so the subject
    # is the ``quarantined_messages`` row itself.
    QUARANTINE = "quarantine"


class AssessmentComponent(StrEnum):
    """§2.7 ``component`` vocabulary (mirrors the DB check)."""

    REPLY_PARSER = "reply_parser"
    DOCUMENT_EXTRACTOR = "document_extractor"
    MULTI_COMPANY_RESOLVER = "multi_company_resolver"


class ReadOrigin(StrEnum):
    """How a value was obtained — the base of the confidence ladder.

    The first two are the digital shapes; the last two are the scanned
    shape (E2), whose score is the engine's own word confidence rather than
    a fixed rung — ``confidence_for`` refuses those origins, and the OCR
    ladder (``ocr_confidence``) owns them.
    """

    EXACT_FIELD_VALUE = "exact_field_value"
    TEXT_IN_FIELD_RECT = "text_in_field_rect"
    OCR_TEXT_IN_FIELD_RECT = "ocr_text_in_field_rect"
    OCR_CHECKBOX_MARK = "ocr_checkbox_mark"


class ReadSignal(StrEnum):
    """Signals that cap a read's confidence, or explain a field's absence.

    Stored per field in the assessment's ``signal_log`` so the review queue
    can say *why* a value is doubtful or missing (§2.7). The scan-only
    signals are refusals: a page the reader cannot place, a form whose
    printed specification is not the pinned one, a box that yielded no
    text, an engine that is not installed.
    """

    BILINGUAL_BOX_SPLIT = "bilingual_box_split"
    REPEATED_FIELD_DISAGREEMENT = "repeated_field_disagreement"
    CHECKBOX_GROUP_NOT_SINGLE = "checkbox_group_not_single"
    VALUE_UNPARSABLE = "value_unparsable"
    NEEDS_OCR = "needs_ocr"
    FORM_FRAME_NOT_FOUND = "form_frame_not_found"
    UNSUPPORTED_SPEC = "unsupported_spec"
    SPEC_REGION_UNREADABLE = "spec_region_unreadable"
    OCR_FIELD_UNREADABLE = "ocr_field_unreadable"
    OCR_ENGINE_UNAVAILABLE = "ocr_engine_unavailable"
    OCR_VALUE_UNVALIDATED = "ocr_value_unvalidated"


class TargetWrite(StrEnum):
    """The write path a proposed record's target belongs to.

    ``STATIC`` targets are ``Company`` anchor statics, corrected only
    through the Administrator-only statics path; ``CONTEXT`` targets are
    read for the run's metadata and never proposed as records.
    """

    FACT = "fact"
    STATIC = "static"
    CONTEXT = "context"


class ValueKind(StrEnum):
    """The canonical form a target's value normalises to."""

    TEXT = "text"
    BRN = "brn"
    INTEGER = "integer"
    DECIMAL = "decimal"
    COMPANY_TYPE = "company_type"


class UnknownTargetError(ValueError):
    """The target is outside the proposed-and-writable vocabulary."""


class InvalidFieldValueError(ValueError):
    """The raw value does not normalise for its target."""


@dataclass(frozen=True)
class TargetSpec:
    """One target's home in the master-data schema and how to normalise it.

    ``group``/``column`` name the fact group and column the confirm path
    writes; ``max_length`` mirrors the fact column's width so an over-long
    read is refused here rather than at the database.
    """

    target: str
    write: TargetWrite
    group: str | None
    column: str | None
    value_kind: ValueKind
    max_length: int | None = None


# The E1 vocabulary: registry paths that map to a single master-data column.
# Group names are the application layer's keys for the fact tables (the
# domain stays ORM-free); ``column`` names the fact column or anchor static.
TARGETS: dict[str, TargetSpec] = {
    spec.target: spec
    for spec in (
        TargetSpec(
            "company.br_number", TargetWrite.STATIC, None, "br_number", ValueKind.BRN
        ),
        TargetSpec("company.type", TargetWrite.STATIC, None, "type", ValueKind.COMPANY_TYPE),
        TargetSpec(
            "company.name_en", TargetWrite.FACT, "company_name", "name_en", ValueKind.TEXT, 300
        ),
        TargetSpec(
            "company.name_zh", TargetWrite.FACT, "company_name", "name_zh", ValueKind.TEXT, 300
        ),
        TargetSpec(
            "company.business_name",
            TargetWrite.FACT,
            "company_name",
            "business_name",
            ValueKind.TEXT,
            300,
        ),
        TargetSpec("company.email", TargetWrite.FACT, "contact", "email", ValueKind.TEXT, 320),
        TargetSpec(
            "company.contact_phone_hk",
            TargetWrite.FACT,
            "contact",
            "contact_phone_hk",
            ValueKind.TEXT,
            20,
        ),
        TargetSpec(
            "company.mortgages_and_charges_total",
            TargetWrite.FACT,
            "filing",
            "mortgages_and_charges_total",
            ValueKind.DECIMAL,
        ),
        TargetSpec(
            "company.member_count", TargetWrite.FACT, "filing", "member_count", ValueKind.INTEGER
        ),
        TargetSpec(
            "company.registered_office.flat",
            TargetWrite.FACT,
            "registered_office",
            "flat",
            ValueKind.TEXT,
            200,
        ),
        TargetSpec(
            "company.registered_office.building",
            TargetWrite.FACT,
            "registered_office",
            "building",
            ValueKind.TEXT,
            200,
        ),
        TargetSpec(
            "company.registered_office.street",
            TargetWrite.FACT,
            "registered_office",
            "street",
            ValueKind.TEXT,
            200,
        ),
        TargetSpec(
            "company.registered_office.district",
            TargetWrite.FACT,
            "registered_office",
            "district",
            ValueKind.TEXT,
            100,
        ),
        TargetSpec(
            "company.business_nature_code",
            TargetWrite.FACT,
            "business_nature",
            "business_nature_code",
            ValueKind.TEXT,
            20,
        ),
        TargetSpec(
            "company.business_nature_description",
            TargetWrite.FACT,
            "business_nature",
            "business_nature_description",
            ValueKind.TEXT,
            300,
        ),
        TargetSpec(
            "return.made_up_date", TargetWrite.CONTEXT, None, None, ValueKind.TEXT
        ),
    )
}


def target_spec(target: str) -> TargetSpec:
    """The target's spec, or ``UnknownTargetError`` (never silently ignored)."""
    spec = TARGETS.get(target)
    if spec is None:
        raise UnknownTargetError(f"target {target!r} is outside the extraction vocabulary")
    return spec


def is_supported_target(target: str) -> bool:
    """Whether the extraction path may propose this registry path at all."""
    return target in TARGETS


def is_static_target(target: str) -> bool:
    """Whether confirming this target corrects a ``Company`` anchor static.

    Statics are not effective-dated and are corrected only through the
    Administrator-only statics path (option (b) decision 2026-08-24).
    """
    return target_spec(target).write is TargetWrite.STATIC


def individual_paths(registry_target: str) -> tuple[str, ...]:
    """The individual contract paths in one registry target string.

    Registry targets join the paths a box carries with `` + `` (contract
    §1); extraction works on the individual paths, since that is what a
    record and its confirming write are about.
    """
    return tuple(part.strip() for part in registry_target.split(" + ") if part.strip())


def confidence_for(*, origin: ReadOrigin, signals: Collection[ReadSignal]) -> float:
    """The confidence of one *digital* read: its origin's base, capped.

    The ladder is intentionally a floor of a few explicit numbers — enough
    to route a field into the §2.7 bands, no pretence of calibration. OCR
    reads score on the engine's word confidence instead, so asking here for
    one is a bug rather than a default.
    """
    match origin:
        case ReadOrigin.EXACT_FIELD_VALUE:
            base = CONFIDENCE_EXACT_FIELD
        case ReadOrigin.TEXT_IN_FIELD_RECT:
            base = CONFIDENCE_TEXT_MATCH
        case _:
            raise ValueError(f"origin {origin!r} is scored by the OCR ladder")
    cap = cap_for(signals)
    return base if cap is None else min(base, cap)


def cap_for(signals: Collection[ReadSignal]) -> float | None:
    """The lowest cap the signals impose on a read, or ``None`` if none does."""
    caps = [
        cap
        for signal, cap in (
            (ReadSignal.BILINGUAL_BOX_SPLIT, CONFIDENCE_BILINGUAL_SPLIT),
            (ReadSignal.REPEATED_FIELD_DISAGREEMENT, CONFIDENCE_REPEATED_DISAGREEMENT),
        )
        if signal in signals
    ]
    return min(caps) if caps else None


def ocr_confidence(*, word_confidences: Sequence[float], value_validated: bool) -> float:
    """The score of one OCR read: its weakest word, capped by validation.

    The engine's per-word confidence (0-1) is the base — the *minimum*, so
    one doubtful word is not averaged away by its neighbours — and the cap
    comes from whether the value can be machine-checked at all: a validated
    value may reach the auto-accept band, an unvalidated one (free text, or
    any kind this product has no check for) cannot.
    """
    if not word_confidences:
        raise ValueError("an OCR read without words has no score")
    cap = CONFIDENCE_OCR_VALIDATED if value_validated else CONFIDENCE_OCR_UNVALIDATED
    return min(min(word_confidences), cap)


def value_is_validated(target: str, value: str) -> bool:
    """Whether a value passes the machine check for its target's kind.

    Deliberately structural, not substantive: eight digits look like a BRN,
    a short non-negative amount looks like money, a text box containing a
    letter looks like a name. A misread value of the right shape still
    passes — which is why validation opens the auto-accept band only
    together with a high engine confidence, and never for the kinds with no
    check at all (anything outside the text/number kinds above).
    """
    try:
        normalize_value(target, value)
    except InvalidFieldValueError:
        return False
    match target_spec(target).value_kind:
        case ValueKind.BRN:
            return bool(_BRN_PATTERN.match(value))
        case ValueKind.INTEGER:
            return bool(_DIGITS_PATTERN.match(value))
        case ValueKind.DECIMAL:
            return _is_money(value)
        case ValueKind.COMPANY_TYPE:
            return value in {member.value for member in CompanyType}
        case _:
            return any(character.isalpha() or _is_cjk(character) for character in value)


def _is_money(value: str) -> bool:
    """A non-negative amount to the cent (the form's amounts are money)."""
    amount = Decimal(value)
    exponent = amount.as_tuple().exponent
    return amount >= 0 and isinstance(exponent, int) and exponent >= -2


def normalize_value(target: str, raw: str) -> str:
    """The canonical stored value for one target, or ``InvalidFieldValueError``.

    Runs on every read (a value that does not normalise is not proposed)
    and again on the reviewer's value at confirmation, so the write path
    only ever sees the canonical form.
    """
    spec = target_spec(target)
    value = " ".join(raw.split())
    if not value:
        raise InvalidFieldValueError(f"{target} is empty")
    match spec.value_kind:
        case ValueKind.TEXT:
            if spec.max_length is not None and len(value) > spec.max_length:
                raise InvalidFieldValueError(
                    f"{target} is longer than {spec.max_length} characters"
                )
            return value
        case ValueKind.BRN:
            try:
                return normalize_brn(value)
            except InvalidBrnError:
                raise InvalidFieldValueError(f"{target} contains no digits: {raw!r}") from None
        case ValueKind.INTEGER:
            digits = re.sub(r"[\s,]", "", value)
            if not _DIGITS_PATTERN.match(digits):
                raise InvalidFieldValueError(f"{target} is not a whole number: {raw!r}")
            return str(int(digits))
        case ValueKind.DECIMAL:
            return _normalize_decimal(target, value)
        case ValueKind.COMPANY_TYPE:
            try:
                return CompanyType(value).value
            except ValueError:
                raise InvalidFieldValueError(
                    f"{target} is not one of {[member.value for member in CompanyType]}"
                ) from None


def _normalize_decimal(target: str, value: str) -> str:
    """A non-negative amount, tolerating currency prefixes and digit grouping.

    A NAR1 amount box may read ``HK$1,234.50``; anything else (``Nil``, a
    credit balance) is refused rather than interpreted.
    """
    cleaned = re.sub(r"^[A-Za-z]+", "", re.sub(r"[\s,$]", "", value))
    try:
        amount = Decimal(cleaned)
    except InvalidOperation:
        raise InvalidFieldValueError(f"{target} is not an amount: {value!r}") from None
    if not amount.is_finite() or amount < 0:
        raise InvalidFieldValueError(f"{target} must be a non-negative amount: {value!r}")
    return str(amount)


def split_bilingual(text: str) -> tuple[str | None, str | None]:
    """The Latin-script and CJK parts of a bilingual form box.

    NAR1 item 1 carries a company's registered name in one box, Chinese
    and/or English (mapping notes). Splitting by script is the only rule
    available without reading the name: characters in the CJK ranges form
    the Chinese part, everything else the English part; a part that comes
    out empty is ``None``. A Latin fragment inside one CJK run stays with
    the Latin part (``ABC有限公司`` → ``ABC`` / ``有限公司``), which is why a
    split read is capped into the staff-review band.
    """
    cjk = "".join(character for character in text if _is_cjk(character))
    latin = " ".join("".join(character for character in text if not _is_cjk(character)).split())
    return (latin or None, cjk or None)


def _is_cjk(character: str) -> bool:
    return any(start <= ord(character) <= end for start, end in _CJK_RANGES)


def parts_to_date(parts: Sequence[str]) -> date | None:
    """A ``DD, MM, YYYY`` field triplet as a date, or ``None``.

    The mapping's ``date_parts`` items are one date split across three
    boxes in that order (NAR1 items 4 and HDR-DATE). Incomplete triples and
    impossible dates (29 February in a non-leap year) yield ``None`` — the
    reviewer supplies the date at confirmation instead of the extractor
    guessing one.
    """
    if len(parts) != 3:
        return None
    day, month, year = (part.strip() for part in parts)
    if not (day.isdigit() and month.isdigit() and year.isdigit()):
        return None
    try:
        return date(int(year), int(month), int(day))
    except ValueError:
        return None
