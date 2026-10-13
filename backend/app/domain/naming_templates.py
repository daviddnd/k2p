"""Naming-template domain rules (contract §5.3; ADR-0013 item 1).

Per-tenant, token-based templates govern the storage keys (filename and
directory layout) of stored documents and generated exports, so the document
store can mirror the TCSP's existing filing conventions. This module is the
single owner of the §5.3 token vocabulary, the PUT-time validation rules,
and the render-time substitution the document store will call when it
writes an object. Template changes apply to new objects only — WORM
originals are never renamed or rewritten (ADR-0009).

Pure and framework-free per ADR-0006: the import-linter contract forbids
framework and I/O imports in this layer.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass

# The §5.3 closed token set. {Form} values come from the form registry and
# {DocType} from the document kinds; both are supplied by the caller at
# render time — the vocabulary here is the token names only.
TOKENS = frozenset(
    {
        "CompanyCode",  # tenant-defined short code per company
        "CR",  # CR number (falls back to BRN at render time)
        "BRN",
        "Year",
        "YYYY",
        "MM",
        "DD",
        "YYYYMMDD",  # date parts of the document's governing date
        "Form",  # NAR1, ND2A, …
        "DocType",  # original, draft, signed, export, reply
        "Ext",  # the stored type's extension: pdf, png, jpg, eml
        "Version",  # draft version
    }
)

# §5.3: {CompanyCode} or {CR} is mandatory — every path must be attributable
# to a company. {BRN} alone does not satisfy this.
_ATTRIBUTION_TOKENS = frozenset({"CompanyCode", "CR"})

MAX_SEGMENT_LENGTH = 128
MAX_KEY_LENGTH = 512

# Cross-platform filename safety (§5.3 rule 2).
_ILLEGAL_CHARS = frozenset('<>:"|?*')

# Shipped defaults (§5.3): the generated template is the §5.3 default
# verbatim; the document template is the same layout without the version
# suffix, since stored originals have no draft version. The extension is a
# token, not a literal: the key is what an operator reads when they use
# independent access to the object store (ADR-0013), and a director's ID
# scan stored as ``.pdf`` would misname the file type in the record.
DEFAULT_DOCUMENT_TEMPLATE = "{CompanyCode}/{Year}/{Form}_{DocType}_{CR}_{YYYYMMDD}.{Ext}"
DEFAULT_GENERATED_TEMPLATE = (
    "{CompanyCode}/{Year}/{Form}_{DocType}_{CR}_{YYYYMMDD}_v{Version}.{Ext}"
)

_TOKEN_PATTERN = re.compile(r"\{([A-Za-z]+)\}")


class TemplateValidationError(ValueError):
    """One or more §5.3 validation rules failed.

    Carries every violation found (not just the first) so a PUT response can
    report them all; each message is prefixed with the template name
    (``document:`` / ``generated:``) when validating a pair.
    """

    def __init__(self, errors: list[str]) -> None:
        self.errors = tuple(errors)
        super().__init__("; ".join(errors))


class MissingTokenValueError(ValueError):
    """A token in the template has no value supplied at render time."""


class InvalidTokenValueError(ValueError):
    """A token value is empty, unsafe, or makes the key exceed the caps."""


@dataclass(frozen=True)
class NamingTemplates:
    """The two per-tenant naming templates (contract §2.9 ``TenantConfig``).

    ``document`` covers stored originals/uploads (no draft version);
    ``generated`` covers system-generated drafts and export packages and
    must carry ``{Version}`` (§5.3 rule 3 collision check).
    """

    document: str
    generated: str


def default_naming_templates() -> NamingTemplates:
    """The validated default templates that ship with the product (§5.3)."""
    return NamingTemplates(document=DEFAULT_DOCUMENT_TEMPLATE, generated=DEFAULT_GENERATED_TEMPLATE)


def _tokens_used(template: str) -> list[str]:
    return _TOKEN_PATTERN.findall(template)


def _validate(template: str, *, requires_version: bool) -> list[str]:
    """Every §5.3 violation in one template (empty list = valid)."""
    errors: list[str] = []
    if not template:
        return ["template must not be empty"]
    if len(template) > MAX_KEY_LENGTH:
        errors.append(f"template exceeds the {MAX_KEY_LENGTH}-character key cap")
    if template.startswith("/"):
        errors.append("template must not start with '/'")
    if ".." in template:
        errors.append("template must not contain '..'")
    illegal = sorted(set(template) & _ILLEGAL_CHARS)
    if illegal:
        errors.append(f"template contains illegal characters: {' '.join(illegal)}")
    if any(ord(char) < 32 for char in template):
        errors.append("template contains control characters")
    for segment in template.split("/"):
        if len(segment) > MAX_SEGMENT_LENGTH:
            errors.append(f"segment exceeds the {MAX_SEGMENT_LENGTH}-character cap")
    # Malformed braces: anything '{' or '}' left after removing well-formed
    # tokens is an unclosed brace, a stray brace, or an empty token.
    if "{" in _TOKEN_PATTERN.sub("", template) or "}" in _TOKEN_PATTERN.sub("", template):
        errors.append("template contains malformed braces (unclosed, stray, or empty token)")
    tokens = _tokens_used(template)
    unknown = sorted(set(tokens) - TOKENS)
    if unknown:
        errors.append(f"unknown tokens: {', '.join(unknown)}")
    if not set(tokens) & _ATTRIBUTION_TOKENS:
        errors.append("template must contain {CompanyCode} or {CR} so every path names a company")
    if requires_version and "Version" not in tokens:
        errors.append("template covers drafts/exports and must contain {Version} (§5.3 rule 3)")
    return errors


def validate_naming_templates(*, document: str, generated: str) -> NamingTemplates:
    """Validate both templates per §5.3; the trimmed pair, or
    ``TemplateValidationError`` listing every violation.

    ``generated`` requires ``{Version}``; ``document`` does not (stored
    originals have no draft version). Surrounding whitespace is trimmed.
    """
    document = document.strip()
    generated = generated.strip()
    errors = [f"document: {error}" for error in _validate(document, requires_version=False)]
    errors += [f"generated: {error}" for error in _validate(generated, requires_version=True)]
    if errors:
        raise TemplateValidationError(errors)
    return NamingTemplates(document=document, generated=generated)


def _check_value(token: str, value: str) -> None:
    """Render-time safety for one token value (the storage key's raw material)."""
    if not value:
        raise InvalidTokenValueError(f"{token}: value must not be empty")
    if value in (".", "..") or "/" in value or "\\" in value:
        raise InvalidTokenValueError(f"{token}: value must not be a path segment or separator")
    if set(value) & _ILLEGAL_CHARS or any(ord(char) < 32 for char in value):
        raise InvalidTokenValueError(f"{token}: value contains illegal or control characters")
    if len(value) > MAX_SEGMENT_LENGTH:
        raise InvalidTokenValueError(
            f"{token}: value exceeds the {MAX_SEGMENT_LENGTH}-character segment cap"
        )


def render_key(template: str, values: Mapping[str, str]) -> str:
    """Substitute token values into a validated template, returning the key.

    Defense in depth even though stored templates were validated at PUT:
    malformed braces and unknown tokens raise ``TemplateValidationError``,
    a token with no value raises ``MissingTokenValueError``, and an unsafe
    value (empty, separator, traversal, illegal chars, over-cap) raises
    ``InvalidTokenValueError``. Extra values are ignored. The rendered key
    is re-checked against the length caps, which token expansion can breach
    even when the template itself was within them.
    """
    if "{" in _TOKEN_PATTERN.sub("", template) or "}" in _TOKEN_PATTERN.sub("", template):
        raise TemplateValidationError(["template contains malformed braces"])
    unknown = sorted(set(_tokens_used(template)) - TOKENS)
    if unknown:
        raise TemplateValidationError([f"unknown tokens: {', '.join(unknown)}"])

    def substitute(match: re.Match[str]) -> str:
        token = match.group(1)
        value = values.get(token)
        if value is None:
            raise MissingTokenValueError(f"no value supplied for token {{{token}}}")
        _check_value(token, value)
        return value

    key = _TOKEN_PATTERN.sub(substitute, template)
    if len(key) > MAX_KEY_LENGTH:
        raise InvalidTokenValueError(f"rendered key exceeds the {MAX_KEY_LENGTH}-character cap")
    return key
