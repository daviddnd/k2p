"""External-contact domain rules (data-model §2.4).

Pure and framework-free per ADR-0006: the import-linter contract forbids
framework and I/O imports in this layer.
"""

from enum import StrEnum


class ChannelType(StrEnum):
    """``ContactChannel.channel_type`` vocabulary (§2.4; mirrors the DB check).

    Email is the only channel active in v1.0; phone/whatsapp/wechat are
    recorded for future use.
    """

    EMAIL = "email"
    PHONE = "phone"
    WHATSAPP = "whatsapp"
    WECHAT = "wechat"


class ChannelStatus(StrEnum):
    """``ContactChannel.status`` vocabulary (§2.4; mirrors the DB check)."""

    ACTIVE = "active"
    UNVERIFIED = "unverified"
    BOUNCED = "bounced"


class InvalidChannelIdentifierError(ValueError):
    """The identifier is not well-formed for its channel type."""


def validate_identifier(channel_type: ChannelType, identifier: str) -> str:
    """The trimmed identifier, or ``InvalidChannelIdentifierError``.

    Deliberately minimal validation (YAGNI): every identifier must be
    non-empty after trimming; an email must contain exactly one ``@`` with
    non-empty local and domain parts. Phone/whatsapp/wechat carry no format
    rules in v1.0 — they are recorded for future use, not dialed.
    """
    trimmed = identifier.strip()
    if not trimmed:
        raise InvalidChannelIdentifierError("identifier must not be empty")
    if channel_type is ChannelType.EMAIL:
        local, at, domain = trimmed.partition("@")
        if at != "@" or not local or not domain or "@" in domain:
            raise InvalidChannelIdentifierError(
                "email identifiers need exactly one @ with non-empty local and domain parts"
            )
    return trimmed
