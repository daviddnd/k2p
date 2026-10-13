"""Document-store domain rules (ADR-0009; data-model §2.9 ``Document``).

Single owner of the document vocabularies and the pure rules behind the
records registry:

- **Closed vocabularies** — ``DocumentKind`` (contract §2.9),
  ``RetentionClass`` (ADR-0009: tagged from day one so lifecycle purging
  can land later without reclassifying a legacy corpus), ``DocumentOrigin``,
  and ``ScanStatus`` (ADR-0009's records registry: the interim posture that
  let uploads register ``pending`` for internal deployments closed with the
  Phase 3 ingestion slice — ClamAV now scans every stored file, and
  ``pending`` means "not successfully scanned").
- **Upload validation** (scope §8: inbound files are validated by type and
  size) — the declared content type must be in the allowed set, the magic
  bytes must match it, and the body must be within the size cap. Three types
  carry no magic bytes and are checked structurally instead: the stored
  inbound *message* (``message/rfc822``), whose first line is an RFC 5322
  header field; the export's api-ready artifact (``application/json``),
  which must parse as JSON; and its companion documents
  (``text/markdown``), which must be text.
- **Derivations** — kind → ``{DocType}`` (§5.3 vocabulary), kind → default
  retention class, the sensitive-class rule deciding which downloads are
  audit-logged (ADR-0009: director ID documents and signed drafts), and the
  usability rule deciding which documents may be served or processed at all.
- **Storage-key token assembly** for ``app.domain.naming_templates.render_key``.

Pure and framework-free per ADR-0006: the import-linter contract forbids
framework and I/O imports in this layer.
"""

import json
import re
from datetime import date
from enum import StrEnum

# Per-file upload cap (scope §8 "validated by size"). Sized well above a
# scanned signed NAR1 pack; the API layer enforces the same cap on the wire.
MAX_UPLOAD_BYTES = 25 * 1024 * 1024  # 25 MiB

# §5.3 {Form} fallback when the uploader does not name a form.
DEFAULT_FORM_TOKEN = "DOC"


class DocumentKind(StrEnum):
    """Contract §2.9 ``Document.kind`` vocabulary."""

    PRIOR_FILING = "prior_filing"
    CLIENT_REPLY = "client_reply"
    SIGNED_DRAFT = "signed_draft"
    GENERATED_DRAFT = "generated_draft"
    EXPORT_PACKAGE = "export_package"
    CLIENT_DOCUMENT = "client_document"


class RetentionClass(StrEnum):
    """ADR-0009 retention classes — legal categories, tagged from day one."""

    STATUTORY_FILING = "statutory_filing"
    CLIENT_COMMUNICATION = "client_communication"
    CDD_ID_DOCUMENT = "cdd_id_document"
    GENERATED_DRAFT = "generated_draft"
    EXPORT_PACKAGE = "export_package"


class DocumentOrigin(StrEnum):
    """ADR-0009 origin vocabulary."""

    UPLOAD = "upload"
    EMAIL_ATTACHMENT = "email_attachment"
    GENERATED = "generated"


class ScanStatus(StrEnum):
    """The document's malware-scan state (ADR-0009).

    ``clean`` is the only state that may be served or processed. ``pending``
    is the quarantined state: not yet scanned, or a scan that failed — the
    retro-scan resolves both. ``infected`` is a positive detection, kept as
    the record of what arrived and never served or processed.
    """

    PENDING = "pending"
    CLEAN = "clean"
    INFECTED = "infected"


# The stored inbound message, exactly as the provider hands it back (ADR-0005
# ingress, ADR-0009 registration). It has no magic bytes, so it is validated
# by its first line instead: an RFC 5322 header field.
MESSAGE_CONTENT_TYPE = "message/rfc822"

# The generated artifacts the export stores beside the PDF packages (P6-4):
# the api-ready filing JSON and the companion helper/checklist documents.
# Neither has magic bytes — JSON is checked by parsing, markdown by being
# text — the structural-check precedent the message type set.
JSON_CONTENT_TYPE = "application/json"
MARKDOWN_CONTENT_TYPE = "text/markdown"

# Allowed declared content types mapped to their magic-byte prefixes
# (scope §8 type validation: the body must match the declaration).
_ALLOWED_CONTENT_TYPES: dict[str, tuple[bytes, ...]] = {
    "application/pdf": (b"%PDF",),
    "image/png": (b"\x89PNG\r\n\x1a\n",),
    "image/jpeg": (b"\xff\xd8\xff",),
}

# A header field name is printable ASCII except colon (RFC 5322 §3.6.8); the
# message begins with one, and nothing else this registry accepts does.
_HEADER_FIELD = re.compile(rb"^[!-9;-~]+:")

# The extension each stored type carries in a rendered storage key (contract
# §5.3 ``{Ext}``). Keys are never rewritten (ADR-0009), so this is fixed at
# registration time — and it is fixed from the type, never from what the
# uploader called the file: the extension must name what the bytes are.
_CONTENT_TYPE_EXTENSIONS: dict[str, str] = {
    "application/pdf": "pdf",
    "image/png": "png",
    "image/jpeg": "jpg",
    MESSAGE_CONTENT_TYPE: "eml",
    JSON_CONTENT_TYPE: "json",
    MARKDOWN_CONTENT_TYPE: "md",
}


def content_type_extension(content_type: str) -> str:
    """The §5.3 ``{Ext}`` value for a stored type.

    Every type :func:`validate_upload` admits has one; a type outside that set
    has none, and rendering must fail loudly rather than key the object under
    an extension that misnames it (``UnsupportedContentTypeError``, the same
    refusal the validator raises).
    """
    extension = _CONTENT_TYPE_EXTENSIONS.get(content_type)
    if extension is None:
        raise UnsupportedContentTypeError(
            f"content type {content_type!r} has no extension to render"
        )
    return extension


class UnsupportedContentTypeError(ValueError):
    """The declared content type is outside the allowed set."""


class ContentTypeMismatchError(ValueError):
    """The body's magic bytes do not match the declared content type."""


class UploadTooLargeError(ValueError):
    """The body exceeds MAX_UPLOAD_BYTES."""


def validate_upload(*, content_type: str, data: bytes) -> None:
    """Validate one upload by declared type, magic bytes, and size (§8).

    Three types carry no magic bytes, and each is checked structurally
    instead, the message type's own precedent: the stored inbound message
    must begin with an RFC 5322 header field; the api-ready filing JSON
    (P6-4) must parse as JSON; a markdown document must be text — UTF-8
    decodable with no NUL byte, because prose is what the type declares.
    """
    if len(data) > MAX_UPLOAD_BYTES:
        raise UploadTooLargeError(f"upload exceeds the {MAX_UPLOAD_BYTES}-byte cap")
    if content_type == MESSAGE_CONTENT_TYPE:
        if not _HEADER_FIELD.match(data):
            raise ContentTypeMismatchError(
                f"body does not look like a {MESSAGE_CONTENT_TYPE} message"
            )
        return
    if content_type == JSON_CONTENT_TYPE:
        try:
            json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise ContentTypeMismatchError(
                f"body does not look like a {JSON_CONTENT_TYPE} document"
            ) from None
        return
    if content_type == MARKDOWN_CONTENT_TYPE:
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            raise ContentTypeMismatchError(
                f"body does not look like a {MARKDOWN_CONTENT_TYPE} document"
            ) from None
        if "\x00" in text:
            raise ContentTypeMismatchError(
                f"body does not look like a {MARKDOWN_CONTENT_TYPE} document"
            )
        return
    prefixes = _ALLOWED_CONTENT_TYPES.get(content_type)
    if prefixes is None:
        raise UnsupportedContentTypeError(f"content type {content_type!r} is not allowed")
    if not any(data.startswith(prefix) for prefix in prefixes):
        raise ContentTypeMismatchError(f"body does not match declared type {content_type!r}")


def is_usable(scan_status: ScanStatus) -> bool:
    """Whether ADR-0009 permits serving or processing this document.

    One rule for both halves: only scanned-clean content is served
    (``issue_download_url``) or processed (an extraction reader). ``pending``
    and ``infected`` are both refusals — a scanner that did not answer is not
    a scanner that said clean.
    """
    return scan_status is ScanStatus.CLEAN


_DOC_TYPE_BY_KIND: dict[DocumentKind, str] = {
    DocumentKind.PRIOR_FILING: "original",
    DocumentKind.CLIENT_DOCUMENT: "original",
    DocumentKind.CLIENT_REPLY: "reply",
    DocumentKind.SIGNED_DRAFT: "signed",
    DocumentKind.GENERATED_DRAFT: "draft",
    DocumentKind.EXPORT_PACKAGE: "export",
}

_DEFAULT_RETENTION_BY_KIND: dict[DocumentKind, RetentionClass | None] = {
    DocumentKind.PRIOR_FILING: RetentionClass.STATUTORY_FILING,
    DocumentKind.SIGNED_DRAFT: RetentionClass.STATUTORY_FILING,
    DocumentKind.CLIENT_REPLY: RetentionClass.CLIENT_COMMUNICATION,
    DocumentKind.GENERATED_DRAFT: RetentionClass.GENERATED_DRAFT,
    DocumentKind.EXPORT_PACKAGE: RetentionClass.EXPORT_PACKAGE,
    # Ambiguous: a generic client document may or may not be a CDD/ID
    # document — the caller must classify it explicitly.
    DocumentKind.CLIENT_DOCUMENT: None,
}


def default_retention_class(kind: DocumentKind) -> RetentionClass | None:
    """The retention class implied by the kind, or ``None`` when the caller
    must choose (``client_document`` is ambiguous)."""
    return _DEFAULT_RETENTION_BY_KIND[kind]


def is_sensitive_class(kind: DocumentKind, retention_class: RetentionClass) -> bool:
    """ADR-0009 access-audit rule: downloads of director ID documents
    (``cdd_id_document``) and signed drafts are written to the audit log."""
    return retention_class is RetentionClass.CDD_ID_DOCUMENT or kind is DocumentKind.SIGNED_DRAFT


def storage_key_values(
    *,
    cr_number: str | None,
    br_number: str,
    form: str | None,
    kind: DocumentKind,
    governing_date: date,
    content_type: str,
    version: int | None = None,
) -> dict[str, str]:
    """The §5.3 token values for rendering a document's storage key.

    ``{CompanyCode}`` and ``{CR}`` both follow the §5.3 fallback chain —
    CR number, else BRN (no company-code column exists; the tenant-config
    slice deferred it to the document store, and the fallback keeps every
    path attributable). ``{Form}`` defaults to ``DOC`` when the uploader
    does not name a form. ``{Ext}`` comes from ``content_type``, which is
    required: a caller that has not decided the type has not decided what
    the object is, and the key must not guess (``.pdf`` for everything was
    the bug this token removes).

    ``{Version}`` (the draft version) is supplied only when the caller
    registers a versioned, system-generated document — it is absent from the
    values otherwise, so a render of a template that does not name it is
    byte-for-byte what it was before this token existed, and a render of the
    ``generated`` template without a version fails loudly in ``render_key``
    (``MissingTokenValueError``) rather than keying the object unversioned.
    """
    code = cr_number or br_number
    values = {
        "CompanyCode": code,
        "CR": code,
        "BRN": br_number,
        "Year": f"{governing_date.year:04d}",
        "YYYY": f"{governing_date.year:04d}",
        "MM": f"{governing_date.month:02d}",
        "DD": f"{governing_date.day:02d}",
        "YYYYMMDD": governing_date.strftime("%Y%m%d"),
        "Form": form or DEFAULT_FORM_TOKEN,
        "DocType": _DOC_TYPE_BY_KIND[kind],
        "Ext": content_type_extension(content_type),
    }
    if version is not None:
        values["Version"] = str(version)
    return values
