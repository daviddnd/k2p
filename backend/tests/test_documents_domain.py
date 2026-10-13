"""Unit tests for the document-store domain rules (ADR-0009; §8 inbound
file safety).

Covers the closed vocabularies, the kind → DocType / retention-class
derivations, upload type-and-size validation (including the stored inbound
message, which has no magic bytes and is checked structurally), the
availability rule that decides what may be served or processed, the
storage-key token assembly (including the §5.3 ``{Ext}`` token, which names
the type the stored bytes actually are), and the sensitive-class
download-audit rule. No database or framework imports — this layer is pure
per ADR-0006.
"""

from datetime import date

import pytest

from app.domain.documents import (
    MAX_UPLOAD_BYTES,
    MESSAGE_CONTENT_TYPE,
    ContentTypeMismatchError,
    DocumentKind,
    RetentionClass,
    ScanStatus,
    UnsupportedContentTypeError,
    UploadTooLargeError,
    default_retention_class,
    is_sensitive_class,
    is_usable,
    storage_key_values,
    validate_upload,
)
from app.domain.naming_templates import (
    DEFAULT_DOCUMENT_TEMPLATE,
    DEFAULT_GENERATED_TEMPLATE,
    MissingTokenValueError,
    render_key,
)

PDF = b"%PDF-1.7 fake body"
PNG = b"\x89PNG\r\n\x1a\n fake body"
JPEG = b"\xff\xd8\xff\xe0 fake body"
MESSAGE = b"From: Director <director@client.example>\r\nSubject: NAR1\r\n\r\nbody\r\n"


# --- Upload validation (scope §8: type and size) ---


def test_pdf_upload_is_accepted() -> None:
    validate_upload(content_type="application/pdf", data=PDF)


def test_image_uploads_are_accepted() -> None:
    validate_upload(content_type="image/png", data=PNG)
    validate_upload(content_type="image/jpeg", data=JPEG)


def test_the_generated_textual_types_are_accepted() -> None:
    """P6-4's export artifacts, through the same seam: the api-ready filing
    JSON parses as JSON, and a markdown companion is text — each checked
    structurally, the message type's precedent (neither has magic bytes)."""
    validate_upload(content_type="application/json", data=b'{"form_type": "nar1"}')
    validate_upload(content_type="text/markdown", data=b"# Checklist\n\n- [ ] one\n")


def test_the_generated_textual_types_refuse_bytes_that_are_not_what_they_claim() -> None:
    with pytest.raises(ContentTypeMismatchError):
        validate_upload(content_type="application/json", data=b"{not json")
    with pytest.raises(ContentTypeMismatchError):
        validate_upload(content_type="application/json", data=b"\xff\xfe binary")
    with pytest.raises(ContentTypeMismatchError):
        validate_upload(content_type="text/markdown", data=b"\x89PNG\r\n\x1a\n fake")
    with pytest.raises(ContentTypeMismatchError):
        validate_upload(content_type="text/markdown", data=b"text with \x00 NUL")


def test_disallowed_content_type_is_rejected() -> None:
    with pytest.raises(UnsupportedContentTypeError):
        validate_upload(content_type="application/x-msdownload", data=b"MZ...")


def test_magic_bytes_must_match_the_declared_type() -> None:
    # An executable renamed to .pdf must not pass.
    with pytest.raises(ContentTypeMismatchError):
        validate_upload(content_type="application/pdf", data=b"MZ not a pdf")


def test_oversize_upload_is_rejected() -> None:
    with pytest.raises(UploadTooLargeError):
        validate_upload(content_type="application/pdf", data=b"%PDF" + b"0" * MAX_UPLOAD_BYTES)


def test_a_stored_message_is_validated_structurally() -> None:
    # The one type without magic bytes: the provider assembles it and this
    # deployment declares it, so the check is that it starts with an RFC 5322
    # header field.
    validate_upload(content_type=MESSAGE_CONTENT_TYPE, data=MESSAGE)


def test_a_body_that_is_not_a_message_is_refused_against_the_declaration() -> None:
    with pytest.raises(ContentTypeMismatchError):
        validate_upload(content_type=MESSAGE_CONTENT_TYPE, data=b"just some text\n")
    with pytest.raises(ContentTypeMismatchError):
        validate_upload(content_type=MESSAGE_CONTENT_TYPE, data=b"MZ executable")


def test_an_oversize_message_is_refused_like_any_other_file() -> None:
    with pytest.raises(UploadTooLargeError):
        validate_upload(
            content_type=MESSAGE_CONTENT_TYPE, data=b"From: a@b\r\n" + b"0" * MAX_UPLOAD_BYTES
        )


# --- Availability (ADR-0009: only scanned-clean content is used) ---


def test_only_a_clean_document_may_be_served_or_processed() -> None:
    assert is_usable(ScanStatus.CLEAN) is True
    # Fail closed both ways: a scan that did not answer and a detection are
    # equally unusable.
    assert is_usable(ScanStatus.PENDING) is False
    assert is_usable(ScanStatus.INFECTED) is False


def test_upload_at_the_cap_is_accepted() -> None:
    validate_upload(
        content_type="application/pdf", data=b"%PDF" + b"0" * (MAX_UPLOAD_BYTES - 4)
    )


# --- Vocabulary derivations ---


def test_default_retention_class_per_kind() -> None:
    assert default_retention_class(DocumentKind.PRIOR_FILING) is RetentionClass.STATUTORY_FILING
    assert default_retention_class(DocumentKind.CLIENT_REPLY) is RetentionClass.CLIENT_COMMUNICATION
    assert default_retention_class(DocumentKind.SIGNED_DRAFT) is RetentionClass.STATUTORY_FILING
    assert default_retention_class(DocumentKind.GENERATED_DRAFT) is RetentionClass.GENERATED_DRAFT
    assert default_retention_class(DocumentKind.EXPORT_PACKAGE) is RetentionClass.EXPORT_PACKAGE
    # A generic client document is ambiguous — the caller must classify it.
    assert default_retention_class(DocumentKind.CLIENT_DOCUMENT) is None


def test_sensitive_class_downloads_are_audited() -> None:
    # ADR-0009: views/downloads of director ID documents and signed drafts
    # are written to the audit log.
    assert is_sensitive_class(DocumentKind.SIGNED_DRAFT, RetentionClass.STATUTORY_FILING)
    assert is_sensitive_class(DocumentKind.CLIENT_DOCUMENT, RetentionClass.CDD_ID_DOCUMENT)
    assert not is_sensitive_class(DocumentKind.PRIOR_FILING, RetentionClass.STATUTORY_FILING)
    assert not is_sensitive_class(DocumentKind.CLIENT_REPLY, RetentionClass.CLIENT_COMMUNICATION)


# --- Storage-key token assembly ---


def test_storage_key_values_use_cr_number_with_brn_fallback() -> None:
    with_cr = storage_key_values(
        cr_number="1234567",
        br_number="12345678",
        form="NAR1",
        kind=DocumentKind.PRIOR_FILING,
        governing_date=date(2026, 8, 25),
        content_type="application/pdf",
    )
    assert with_cr["CompanyCode"] == "1234567"
    assert with_cr["CR"] == "1234567"
    assert with_cr["BRN"] == "12345678"

    without_cr = storage_key_values(
        cr_number=None,
        br_number="12345678",
        form="NAR1",
        kind=DocumentKind.PRIOR_FILING,
        governing_date=date(2026, 8, 25),
        content_type="application/pdf",
    )
    assert without_cr["CompanyCode"] == "12345678"
    assert without_cr["CR"] == "12345678"


def test_storage_key_values_map_kind_to_doc_type_and_split_the_date() -> None:
    values = storage_key_values(
        cr_number=None,
        br_number="12345678",
        form="ND2A",
        kind=DocumentKind.CLIENT_REPLY,
        governing_date=date(2026, 3, 4),
        content_type=MESSAGE_CONTENT_TYPE,
    )
    assert values["DocType"] == "reply"
    assert values["Form"] == "ND2A"
    assert (values["Year"], values["YYYY"]) == ("2026", "2026")
    assert (values["MM"], values["DD"], values["YYYYMMDD"]) == ("03", "04", "20260304")


def test_storage_key_values_default_form_is_doc() -> None:
    values = storage_key_values(
        cr_number=None,
        br_number="12345678",
        form=None,
        kind=DocumentKind.CLIENT_DOCUMENT,
        governing_date=date(2026, 8, 25),
        content_type="application/pdf",
    )
    assert values["Form"] == "DOC"
    assert values["DocType"] == "original"


# --- The storage key's extension (§5.3 {Ext}) ---


@pytest.mark.parametrize(
    ("content_type", "data", "extension"),
    [
        ("application/pdf", PDF, "pdf"),
        ("image/png", PNG, "png"),
        ("image/jpeg", JPEG, "jpg"),
        (MESSAGE_CONTENT_TYPE, MESSAGE, "eml"),
        ("application/json", b'{"form_type": "nar1"}', "json"),
        ("text/markdown", b"# Checklist\n", "md"),
    ],
)
def test_the_default_template_renders_the_extension_of_the_stored_type(
    content_type: str, data: bytes, extension: str
) -> None:
    """Every type the registry admits renders a key that names that type.

    The key is what an operator reads when using independent access to the
    object store (ADR-0013), so a PNG — or a director's ID scan, the CDD
    class — landing under ``.pdf`` is a wrong fact in a regulated record.
    """
    validate_upload(content_type=content_type, data=data)

    key = render_key(
        DEFAULT_DOCUMENT_TEMPLATE,
        storage_key_values(
            cr_number="1234567",
            br_number="12345678",
            form=None,
            kind=DocumentKind.CLIENT_DOCUMENT,
            governing_date=date(2026, 9, 23),
            content_type=content_type,
        ),
    )

    assert key == f"1234567/2026/DOC_original_1234567_20260923.{extension}"


def test_a_content_type_outside_the_registry_has_no_extension() -> None:
    """Fail loudly rather than render a key that misnames the bytes."""
    with pytest.raises(UnsupportedContentTypeError):
        storage_key_values(
            cr_number=None,
            br_number="12345678",
            form=None,
            kind=DocumentKind.CLIENT_DOCUMENT,
            governing_date=date(2026, 9, 23),
            content_type="image/tiff",
        )


# --- The {Version} token (contract §5.3: the generated template's draft
# version) ---


def test_storage_key_values_carry_no_version_unless_one_is_given() -> None:
    values = storage_key_values(
        cr_number="1234567",
        br_number="12345678",
        form="NAR1",
        kind=DocumentKind.GENERATED_DRAFT,
        governing_date=date(2026, 9, 23),
        content_type=MESSAGE_CONTENT_TYPE,
    )
    assert "Version" not in values


def test_storage_key_values_carry_the_version_that_was_given() -> None:
    values = storage_key_values(
        cr_number="1234567",
        br_number="12345678",
        form="NAR1",
        kind=DocumentKind.GENERATED_DRAFT,
        governing_date=date(2026, 9, 23),
        content_type=MESSAGE_CONTENT_TYPE,
        version=2,
    )
    assert values["Version"] == "2"


def test_the_generated_default_template_renders_the_version() -> None:
    """The §5.3 ``generated`` template with a version in it.

    Its ``{Version}`` is mandatory (rule 3's collision check), so this is
    what keeps two drafts of one company, form and day off one key — and the
    rendered key still names the type the bytes are (``.eml``).
    """
    key = render_key(
        DEFAULT_GENERATED_TEMPLATE,
        storage_key_values(
            cr_number="1234567",
            br_number="12345678",
            form="NAR1",
            kind=DocumentKind.GENERATED_DRAFT,
            governing_date=date(2026, 9, 23),
            content_type=MESSAGE_CONTENT_TYPE,
            version=1,
        ),
    )
    assert key == "1234567/2026/NAR1_draft_1234567_20260923_v1.eml"


def test_rendering_the_generated_template_without_a_version_fails_loudly() -> None:
    # Fail closed: a caller that renders the generated template must supply
    # the version, rather than keying a draft as if it had none.
    with pytest.raises(MissingTokenValueError):
        render_key(
            DEFAULT_GENERATED_TEMPLATE,
            storage_key_values(
                cr_number="1234567",
                br_number="12345678",
                form="NAR1",
                kind=DocumentKind.GENERATED_DRAFT,
                governing_date=date(2026, 9, 23),
                content_type=MESSAGE_CONTENT_TYPE,
            ),
        )
