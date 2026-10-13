"""Unit tests for contact-channel domain rules (data-model §2.4)."""

import pytest

from app.domain.contacts import (
    ChannelType,
    InvalidChannelIdentifierError,
    validate_identifier,
)

# --- email ---


def test_email_with_local_and_domain_is_accepted() -> None:
    assert validate_identifier(ChannelType.EMAIL, "director@example.com") == "director@example.com"


def test_email_is_trimmed() -> None:
    assert validate_identifier(ChannelType.EMAIL, "  director@example.com\n") == (
        "director@example.com"
    )


@pytest.mark.parametrize(
    "identifier",
    [
        "",
        "   ",
        "no-at-sign.example.com",
        "two@at@signs.example.com",
        "@example.com",  # empty local part
        "director@",  # empty domain
    ],
)
def test_email_rejects_malformed_identifiers(identifier: str) -> None:
    with pytest.raises(InvalidChannelIdentifierError):
        validate_identifier(ChannelType.EMAIL, identifier)


# --- phone / whatsapp / wechat ---

NON_EMAIL_CHANNELS = [ChannelType.PHONE, ChannelType.WHATSAPP, ChannelType.WECHAT]


@pytest.mark.parametrize("channel_type", NON_EMAIL_CHANNELS)
def test_non_email_channels_accept_any_non_empty_identifier(channel_type: ChannelType) -> None:
    assert validate_identifier(channel_type, "  +852 2345 6789 ") == "+852 2345 6789"


@pytest.mark.parametrize("channel_type", NON_EMAIL_CHANNELS)
def test_non_email_channels_reject_empty_identifiers(channel_type: ChannelType) -> None:
    with pytest.raises(InvalidChannelIdentifierError):
        validate_identifier(channel_type, "   ")
