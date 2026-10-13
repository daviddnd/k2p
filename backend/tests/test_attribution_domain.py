"""Attribution domain tests (ADR-0019 §1; the attribution slices' task 1s).

Pure rules, no I/O: the four-way attribution decision over thread evidence
and the sender's registered-channel status; the confirm grammar of ruling
R5's notation half; the plain path's two automatic ask classes (the
clarification loop, ruling D4) with their Letters A/B and the answer
grammar (ruling D6); and the founder-gated bilingual ask drafts — which
identify the company by name and CR/BRN in both language halves (scope §7)
and quote envelope facts only (ruling R2), except the company-less Letters
A/B, which name no company at all (D5/C10). The case row, the asks'
delivery and the confirmation's authorising effect live in the
application/DB suites.
"""

# This file quotes the bilingual copy it checks: each draft stands as one
# verbatim block (so its lines run as long as the letter's), and its
# Traditional-Chinese punctuation is the fullwidth CJK that ruff's RUF001
# reads as an ambiguous look-alike. Both file-wide suppressions are deliberate
# — the RUF001 one mirrors the module that writes the copy, and the E501 one
# is the confirmation-copy tests' own precedent for a verbatim block.
# ruff: noqa: E501, RUF001

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from app.domain.attribution import (
    CONFIRM_LITERALS,
    AttributionAction,
    AttributionAskKind,
    AttributionCaseStatus,
    AttributionDecision,
    CompanyReference,
    UnthreadedAction,
    ask_subject,
    clarification_ask_letter,
    companyless_ask_subject,
    confirmation_ask_letter,
    decide,
    decide_unthreaded,
    first_contact_ask_letter,
    is_confirmation,
    match_company_reference,
    match_company_references,
    registration_ask_letter,
)

COMPANY = "Kairos Trading Limited"
CR = "1234567"
BRN = "87654321"
SENDER = "personal@example.test"
ORIGINAL_SUBJECT = "Fwd: appointment of a director"
RECEIVED = datetime(2026, 10, 8, 6, 30, tzinfo=UTC)
TCSP = "Example TCSP Limited"


# --- The vocabularies ---------------------------------------------------------


def test_the_action_vocabulary_is_the_four_actions_the_slice_names() -> None:
    assert [action.value for action in AttributionAction] == [
        "plain",
        "process",
        "ask_registered_channel",
        "ask_confirmation",
    ]


def test_the_case_status_vocabulary_is_the_six_states_the_slices_name() -> None:
    assert [status.value for status in AttributionCaseStatus] == [
        "awaiting_registered_channel",
        "awaiting_confirmation",
        "awaiting_clarification",
        "resolved",
        "closed",
        "dismissed",
    ]


def test_the_ask_kind_vocabulary_is_the_three_asks_the_slices_name() -> None:
    assert [kind.value for kind in AttributionAskKind] == [
        "registered_channel",
        "confirmation",
        "clarification",
    ]


# --- The decision table -------------------------------------------------------


def test_an_unthreaded_message_runs_todays_flow() -> None:
    """Not threaded → ``plain``: the message is not on a stored outbound
    exchange, so today's flow (quarantine, resolver, staff) runs unchanged —
    whatever the sender's channels and the copy look like."""
    assert decide(
        threaded=False,
        sender_is_channel=True,
        registered_channels_in_copy=["director@example.test"],
    ) == AttributionDecision(action=AttributionAction.PLAIN, recipients=())
    assert decide(
        threaded=False, sender_is_channel=False, registered_channels_in_copy=[]
    ) == AttributionDecision(action=AttributionAction.PLAIN, recipients=())


def test_a_registered_sender_on_a_thread_is_processed() -> None:
    """The thread fixes the chain's company; a sender who is its registered
    channel follows the ordinary processing path — the thread is the evidence
    that resolves the multi-company sender, not an ask."""
    decision = decide(threaded=True, sender_is_channel=True, registered_channels_in_copy=[])
    assert decision == AttributionDecision(action=AttributionAction.PROCESS, recipients=())


def test_a_registered_channel_in_the_copy_asks_that_channel_to_confirm() -> None:
    """ADR-0019 §1's boss-on-copy case: a registered channel among To/Cc is
    asked to confirm the information; the decision names it."""
    decision = decide(
        threaded=True,
        sender_is_channel=False,
        registered_channels_in_copy=["director@example.test"],
    )
    assert decision == AttributionDecision(
        action=AttributionAction.ASK_CONFIRMATION,
        recipients=("director@example.test",),
    )


def test_no_registered_channel_anywhere_asks_the_sender_to_instruct() -> None:
    """ADR-0019 §1's other ask: with no registered channel on the message at
    all, the reply asks that the instruction be sent from one. That ask goes
    back to the sender, so the decision carries no recipients — the caller
    supplies it."""
    decision = decide(threaded=True, sender_is_channel=False, registered_channels_in_copy=[])
    assert decision == AttributionDecision(
        action=AttributionAction.ASK_REGISTERED_CHANNEL, recipients=()
    )


def test_copy_channels_are_deduped_case_insensitively_in_first_seen_order() -> None:
    """The caller passes every To/Cc address that matched a registered
    channel; the same address twice (To and Cc, either case) is one
    recipient, ordered by first appearance."""
    decision = decide(
        threaded=True,
        sender_is_channel=False,
        registered_channels_in_copy=[
            "  Director@Example.test ",
            "secretary@example.test",
            "DIRECTOR@example.test",
            "Secretary@Example.Test",
        ],
    )
    assert decision.action is AttributionAction.ASK_CONFIRMATION
    assert decision.recipients == ("director@example.test", "secretary@example.test")


def test_a_whitespace_only_entry_is_not_a_registered_channel() -> None:
    decision = decide(
        threaded=True, sender_is_channel=False, registered_channels_in_copy=["", "   "]
    )
    assert decision == AttributionDecision(
        action=AttributionAction.ASK_REGISTERED_CHANNEL, recipients=()
    )


# --- The plain path's two ask classes (ruling D4) -------------------------------


def test_the_unthreaded_action_vocabulary_is_the_three_outcomes_the_ruling_names() -> None:
    assert [action.value for action in UnthreadedAction] == [
        "quarantine",
        "ask_first_contact",
        "ask_clarification",
    ]


def test_an_unregistered_sender_with_an_anchor_is_asked_to_use_a_registered_channel() -> None:
    """B-class: the sender is a registered channel of no company and the
    message carries its own RFC ``Message-ID`` (the anchor the case hangs
    on) — the first-contact ask replaces the ``UNATTRIBUTED_SENDER``
    quarantine."""
    assert (
        decide_unthreaded(registered_company_count=0, resolver_matched=False, has_anchor=True)
        is UnthreadedAction.ASK_FIRST_CONTACT
    )
    # The resolver cannot have matched a company the sender holds no channel
    # for; the decision asks either way — the count and the anchor are the
    # class's whole definition.
    assert (
        decide_unthreaded(registered_company_count=0, resolver_matched=True, has_anchor=True)
        is UnthreadedAction.ASK_FIRST_CONTACT
    )


def test_a_multi_company_sender_the_resolver_matches_none_is_asked_which_company() -> None:
    """A-class: the sender is a registered channel of ≥2 companies and the
    deterministic resolver matched none of them in the content — the
    clarification ask replaces the ``AMBIGUOUS_SENDER``/``MULTIPLE_COMPANIES``
    quarantine."""
    for count in (2, 3):
        assert (
            decide_unthreaded(
                registered_company_count=count, resolver_matched=False, has_anchor=True
            )
            is UnthreadedAction.ASK_CLARIFICATION
        )


@pytest.mark.parametrize(
    ("count", "resolver_matched", "has_anchor"),
    [
        # No anchor, either class: ①'s anchor-less rule extends verbatim — a
        # message without its own RFC Message-ID opens no case, so both
        # classes fall back to today's quarantine.
        (0, False, False),
        (2, False, False),
        # The resolver matched one of the sender's companies: today's flow
        # processes the message — no ask.
        (2, True, True),
        (3, True, True),
        # A single-company sender is not ambiguous: today's flow attributes
        # or quarantines on the resolver's own answer, never an ask.
        (1, False, True),
        (1, True, True),
        (1, False, False),
    ],
)
def test_every_other_unthreaded_shape_is_todays_handling(
    count: int, resolver_matched: bool, has_anchor: bool
) -> None:
    assert (
        decide_unthreaded(
            registered_company_count=count,
            resolver_matched=resolver_matched,
            has_anchor=has_anchor,
        )
        is UnthreadedAction.QUARANTINE
    )


# --- The confirm grammar (ruling R5) ------------------------------------------


def test_the_confirm_literals_are_the_three_the_ruling_names() -> None:
    assert CONFIRM_LITERALS == ("confirm", "確認", "确认")


@pytest.mark.parametrize("body", ["Confirm", "confirm", "CONFIRM", "  confirm  ", "確認", "确认"])
def test_a_bare_confirm_word_on_the_first_line_confirms(body: str) -> None:
    assert is_confirmation(body)


@pytest.mark.parametrize("body", ["Ｃｏｎｆｉｒｍ", "ｃｏｎｆｉｒｍ", "ＣＯＮＦＩＲＭ"])
def test_full_width_latin_normalizes_to_a_confirmation(body: str) -> None:
    """NFKC folds fullwidth Latin to ASCII before the comparison, so a client
    typing through a fullwidth IME still confirms."""
    assert is_confirmation(body)


def test_ideographic_whitespace_around_the_word_is_stripped() -> None:
    assert is_confirmation("　確認　")


def test_leading_blank_lines_are_skipped_to_the_first_non_empty_line() -> None:
    assert is_confirmation("\n\n\t\n確認\n\n--\nAlice Chan")


def test_a_trailing_signature_and_quoted_history_below_the_first_line_do_not_matter() -> None:
    body = (
        "Confirm\r\n"
        "\r\n"
        "Best regards,\r\n"
        "Alice Chan, Director\r\n"
        "\r\n"
        "On 8 October 2026 at 14:30, Example TCSP Limited wrote:\r\n"
        "> Please reply to confirm the details below.\r\n"
    )
    assert is_confirmation(body)


@pytest.mark.parametrize(
    "body",
    [
        "",
        "   \n\n\t",
        "please confirm the attached",
        "確認しますか",
        "confirm?",
        "confirmation",
        "Noted, thanks.\nCONFIRM",
    ],
)
def test_anything_but_the_bare_first_line_is_not_a_confirmation(body: str) -> None:
    """The slice's review focus: a ``confirm`` buried in prose must not
    authorise processing — only the first non-empty line counts, and it must
    be exactly one of the literals."""
    assert not is_confirmation(body)


# --- The ask drafts (ruling R9: founder-gated copy) ---------------------------
#
# The drafts the tests pin verbatim. They are regulated client communication:
# before any deployment sends them, this exact wording needs the founder's
# approval, recorded; an edit re-opens that approval (ruling R9, the M3
# precedent).

COPY_APPROVAL_REQUIRED = (
    "The ask drafts no longer match the wording recorded for the founder's "
    "approval (plan ruling R9). This is regulated client communication: before "
    "any deployment sends it, the wording needs the founder's verbatim "
    "approval, recorded, before the test is updated to match the new rendering."
)

DRAFT_REGISTRATION_ASK = """\
Dear Sir or Madam / 敬啟者：

We received a message that appears to concern Kairos Trading Limited (CR Number 1234567). To protect the company, we act on instructions only when they come from an email address registered with us for that company. Please send your instruction from a registered email address; if you have not registered an address with us, please contact our staff.

本公司收到一則看來與Kairos Trading Limited（公司註冊編號1234567）有關的訊息。為保障公司，本公司僅會處理由已登記電郵地址發出的指示。請使用 貴台已向本公司登記的電郵地址重新發送指示；如 貴台尚未向本公司登記任何電郵地址，請與本公司職員聯絡。

Company / 公司：Kairos Trading Limited
CR Number / 公司註冊編號：1234567

Yours faithfully / 敬啟
"""

DRAFT_CONFIRMATION_ASK = """\
Dear Sir or Madam / 敬啟者：

We received a message about Kairos Trading Limited (CR Number 1234567) from personal@example.test; it reached us at 2026-10-08T06:30:00+00:00. That address is not registered with us for this company, so we cannot act on the message yet. An email address registered with us for this company is among the message's recipients: please confirm that the message reflects your instruction by replying to this email with the single word CONFIRM on the first line, and we will process it. If you do not recognise the message, please tell us.

本公司收到一則關於Kairos Trading Limited（公司註冊編號1234567）的訊息，由personal@example.test發出，收到時間為2026-10-08T06:30:00+00:00。該發件地址並非已向本公司登記的電郵地址，故本公司暫未能處理該訊息。訊息收件人中有 貴台已向本公司登記的電郵地址：請回覆本郵件，並在首行寫上「確認」二字，確認該訊息代表 貴台的指示，本公司將據此處理。如 貴台並不認識該訊息，請告知本公司。

Company / 公司：Kairos Trading Limited
CR Number / 公司註冊編號：1234567
From / 寄件人：personal@example.test
Subject / 原郵件主題：Fwd: appointment of a director
Received / 收到時間：2026-10-08T06:30:00+00:00

Yours faithfully / 敬啟
"""


def registration_letter() -> str:
    return registration_ask_letter(company_name=COMPANY, cr_number=CR, br_number=BRN)


def confirmation_letter() -> str:
    return confirmation_ask_letter(
        company_name=COMPANY,
        cr_number=CR,
        br_number=BRN,
        sender_address=SENDER,
        original_subject=ORIGINAL_SUBJECT,
        original_received_at=RECEIVED,
    )


def test_the_registration_ask_is_the_draft_recorded_for_approval() -> None:
    assert registration_letter() == DRAFT_REGISTRATION_ASK, COPY_APPROVAL_REQUIRED


def test_the_confirmation_ask_is_the_draft_recorded_for_approval() -> None:
    assert confirmation_letter() == DRAFT_CONFIRMATION_ASK, COPY_APPROVAL_REQUIRED


def test_the_registration_ask_identifies_the_company_in_both_language_halves() -> None:
    """Scope §7's guardrail: every client-facing message identifies the
    company by name and CR number — asserted for both language halves."""
    body = registration_letter()
    assert "(CR Number 1234567)" in body
    assert "（公司註冊編號1234567）" in body
    assert "Company / 公司：Kairos Trading Limited" in body
    assert "CR Number / 公司註冊編號：1234567" in body


def test_the_confirmation_ask_identifies_the_company_in_both_language_halves() -> None:
    body = confirmation_letter()
    assert "(CR Number 1234567)" in body
    assert "（公司註冊編號1234567）" in body
    assert "Company / 公司：Kairos Trading Limited" in body
    assert "CR Number / 公司註冊編號：1234567" in body


def test_the_letters_fall_back_to_the_brn_when_no_cr_number_is_held() -> None:
    registration = registration_ask_letter(company_name=COMPANY, cr_number=None, br_number=BRN)
    assert "(BRN 87654321)" in registration
    assert "（商業登記號碼87654321）" in registration
    assert "BRN / 商業登記號碼：87654321" in registration

    confirmation = confirmation_ask_letter(
        company_name=COMPANY,
        cr_number=None,
        br_number=BRN,
        sender_address=SENDER,
        original_subject=ORIGINAL_SUBJECT,
        original_received_at=RECEIVED,
    )
    assert "(BRN 87654321)" in confirmation
    assert "（商業登記號碼87654321）" in confirmation
    assert "BRN / 商業登記號碼：87654321" in confirmation


def test_the_confirmation_ask_quotes_the_envelope_facts_only() -> None:
    """R2: the ask quotes the sender, the original subject and the received
    time — the envelope facts — and nothing else of the message. The sender
    is the only email address anywhere in the letter, and the client is asked
    for the very word the confirm grammar accepts."""
    body = confirmation_letter()
    assert SENDER in body
    assert ORIGINAL_SUBJECT in body
    assert RECEIVED.isoformat() in body
    # The sender is the only email address anywhere in the letter (repeated
    # per language half and the header block, but nothing else is quoted).
    assert body.replace(SENDER, "").count("@") == 0
    assert "the single word CONFIRM on the first line" in body
    assert "「確認」" in body


def test_the_registration_ask_quotes_no_address_at_all() -> None:
    """The no-registered-channel ask names only the company: the ask goes back
    to the sender (the caller supplies that recipient), and no part of the
    message is quoted."""
    assert "@" not in registration_letter()


def test_the_ask_subject_is_the_family_shape_with_the_kinds_phrase() -> None:
    """``[<name> — CR <number>] <phrase>`` — the confirmation-request
    family's own subject shape, the company identified the way the letters
    identify it."""
    assert (
        ask_subject(
            company_name=COMPANY,
            cr_number=CR,
            br_number=BRN,
            kind=AttributionAskKind.REGISTERED_CHANNEL,
        )
        == "[Kairos Trading Limited — CR 1234567] "
        "Please instruct us from a registered email address"
    ), COPY_APPROVAL_REQUIRED
    assert (
        ask_subject(
            company_name=COMPANY,
            cr_number=CR,
            br_number=BRN,
            kind=AttributionAskKind.CONFIRMATION,
        )
        == "[Kairos Trading Limited — CR 1234567] "
        "Please confirm the message we received on your behalf"
    ), COPY_APPROVAL_REQUIRED


def test_the_ask_subject_falls_back_to_the_brn() -> None:
    subject = ask_subject(
        company_name=COMPANY,
        cr_number=None,
        br_number=BRN,
        kind=AttributionAskKind.CONFIRMATION,
    )
    assert subject == (
        "[Kairos Trading Limited — BRN 87654321] "
        "Please confirm the message we received on your behalf"
    )


# --- The clarification loop's two company-less asks (rulings D4/D5) -------------
#
# Letters A and B are founder-gated copy like the two above: pinned verbatim
# below, and an edit re-opens the approval before any deployment sends them.
# They name no company anywhere (C10) — the TCSP name leads the subject
# instead — and quote envelope facts only.

DRAFT_CLARIFICATION_ASK = """\
Dear Sir or Madam / 敬啟者：

We received your message (subject: Fwd: appointment of a director; received at 2026-10-08T06:30:00+00:00) from your registered email address. The message does not say which company it concerns, so we cannot act on it yet. Please reply to this email with the company's full name, or its CR or BRN number, on the first line, and we will process your message. If it concerns more than one company, please name them all, one per line.

本公司收到由 貴台已登記電郵地址發出之訊息（主題：Fwd: appointment of a director；收到時間：2026-10-08T06:30:00+00:00）。訊息並未註明涉及 貴台哪家公司，故本公司暫未能處理。請回覆本郵件，並在首行寫上所涉公司之全名，或公司註冊編號／商業登記號碼，本公司將據此處理。如涉及多於一家公司，請逐行列明。

From / 寄件人：personal@example.test
Subject / 原郵件主題：Fwd: appointment of a director
Received / 收到時間：2026-10-08T06:30:00+00:00

Yours faithfully / 敬啟
"""

DRAFT_FIRST_CONTACT_ASK = """\
Dear Sir or Madam / 敬啟者：

We received a message from personal@example.test (subject: Fwd: appointment of a director; received at 2026-10-08T06:30:00+00:00). To protect our clients, we act on instructions only when they come from an email address registered with us. If your message concerns a company we serve, please send it from a registered email address, or contact our staff.

本公司收到由 personal@example.test 發出之訊息（主題：Fwd: appointment of a director；收到時間：2026-10-08T06:30:00+00:00）。為保障客戶，本公司僅會處理由已登記電郵地址發出的指示。如該訊息與本公司服務之公司有關，請使用已登記的電郵地址重新發送，或與本公司職員聯絡。

From / 寄件人：personal@example.test
Subject / 原郵件主題：Fwd: appointment of a director
Received / 收到時間：2026-10-08T06:30:00+00:00

Yours faithfully / 敬啟
"""


def clarification_letter() -> str:
    return clarification_ask_letter(
        sender_address=SENDER,
        original_subject=ORIGINAL_SUBJECT,
        original_received_at=RECEIVED,
    )


def first_contact_letter() -> str:
    return first_contact_ask_letter(
        sender_address=SENDER,
        original_subject=ORIGINAL_SUBJECT,
        original_received_at=RECEIVED,
    )


def test_the_clarification_ask_is_the_draft_recorded_for_approval() -> None:
    assert clarification_letter() == DRAFT_CLARIFICATION_ASK, COPY_APPROVAL_REQUIRED


def test_the_first_contact_ask_is_the_draft_recorded_for_approval() -> None:
    assert first_contact_letter() == DRAFT_FIRST_CONTACT_ASK, COPY_APPROVAL_REQUIRED


def test_the_company_less_letters_name_no_company_anywhere() -> None:
    """C10's guard: unlike the two §1 letters, Letters A/B carry no company
    identification block — the client is never told which companies the
    sender is registered for."""
    for body in (clarification_letter(), first_contact_letter()):
        assert COMPANY not in body
        assert CR not in body
        assert BRN not in body
        assert "Company / 公司" not in body


def test_the_company_less_letters_quote_the_envelope_facts_only() -> None:
    """R2 holds for the new letters too: the sender's address, the original
    subject and the received time — never the message's body. The sender is
    the only email address anywhere in either letter."""
    for body in (clarification_letter(), first_contact_letter()):
        assert SENDER in body
        assert ORIGINAL_SUBJECT in body
        assert RECEIVED.isoformat() in body
        assert body.replace(SENDER, "").count("@") == 0


def test_the_clarification_ask_invites_the_answer_grammar() -> None:
    """The letter asks for exactly what ``match_company_reference`` reads: the
    company's full name or its CR/BRN number on the first line, one per line
    when several — the two halves of the invitation are held together here."""
    body = clarification_letter()
    assert "on the first line" in body
    assert "full name, or its CR or BRN number" in body
    assert "name them all, one per line" in body
    assert "首行" in body
    assert "公司註冊編號／商業登記號碼" in body
    assert "逐行列明" in body


def test_the_company_less_letters_render_their_fill_ins() -> None:
    received = datetime(2026, 11, 2, 10, 15, tzinfo=UTC)
    letter = clarification_ask_letter(
        sender_address="boss@client.example",
        original_subject="Change of registered office",
        original_received_at=received,
    )
    assert "boss@client.example" in letter
    assert "Change of registered office" in letter
    assert received.isoformat() in letter
    letter = first_contact_ask_letter(
        sender_address="cold@prospect.example",
        original_subject="Company setup enquiry",
        original_received_at=received,
    )
    assert "cold@prospect.example" in letter
    assert "Company setup enquiry" in letter
    assert received.isoformat() in letter


def test_the_company_less_subject_is_the_tcsp_name_shape_with_the_kinds_phrase() -> None:
    """C10's subject shape — ``[<TCSP name>] <phrase>``, the company-less
    counterpart of the §1 family shape; the phrase is the kind's own
    bilingual notice, pinned like ``_ASK_PHRASES`` above."""
    assert (
        companyless_ask_subject(tcsp_name=TCSP, kind=AttributionAskKind.CLARIFICATION)
        == "[Example TCSP Limited] Please tell us which company your message concerns "
        "/ 請告知 貴台訊息所涉的公司"
    ), COPY_APPROVAL_REQUIRED
    assert (
        companyless_ask_subject(tcsp_name=TCSP, kind=AttributionAskKind.REGISTERED_CHANNEL)
        == "[Example TCSP Limited] Please instruct us from a registered email address "
        "/ 請以已登記的電郵地址發出指示"
    ), COPY_APPROVAL_REQUIRED


# --- The answer grammar (ruling D6) ---------------------------------------------


def _references(*rows: tuple[str, str | None, str | None]) -> list[CompanyReference]:
    return [
        CompanyReference(company_id=uuid4(), name=name, cr_number=cr, br_number=brn)
        for name, cr, brn in rows
    ]


def test_a_first_line_naming_one_company_by_name_resolves_to_that_company() -> None:
    companies = _references(
        (COMPANY, CR, BRN),
        ("Other Harbour Limited", "7654321", "12345678"),
    )
    resolved = match_company_reference("Kairos Trading Limited", companies)
    assert resolved == companies[0].company_id


def test_the_name_match_is_nfkc_casefolded_and_stripped() -> None:
    """D6's normalization: a client typing through a fullwidth IME, in any
    case, with surrounding whitespace, still names the company."""
    companies = _references((COMPANY, CR, BRN))
    for line in (
        "ＫＡＩＲＯＳ　ＴＲＡＤＩＮＧ　ＬＩＭＩＴＥＤ",
        "  kairos trading limited  ",
        "Ｋａｉｒｏｓ Ｔｒａｄｉｎｇ Ｌｉｍｉｔｅｄ",
    ):
        assert match_company_reference(line, companies) == companies[0].company_id


def test_a_first_line_naming_the_cr_or_brn_digits_resolves_to_that_company() -> None:
    companies = _references(
        (COMPANY, CR, BRN),
        ("Other Harbour Limited", "7654321", "12345678"),
    )
    for line in ("CR 1234567", "cr:1234567", "公司註冊編號 1234567", "BRN 87654321"):
        assert match_company_reference(line, companies) == companies[0].company_id


def test_the_digit_match_compares_digit_sequences_not_formatting() -> None:
    """The comparison form is digits only: a stored number's own punctuation
    is ignored, so "CR 1234567" and "1234567" are one sequence."""
    companies = _references((COMPANY, "CR 1234567", "BR 87654321"))
    assert match_company_reference("1234567", companies) == companies[0].company_id
    assert match_company_reference("8,765,4321", companies) == companies[0].company_id


def test_a_first_line_matching_nothing_resolves_to_none() -> None:
    companies = _references((COMPANY, CR, BRN))
    assert match_company_reference("Some Other Limited", companies) is None
    assert match_company_reference("CR 9999999", companies) is None
    assert match_company_reference("", companies) is None
    assert match_company_reference("   ", companies) is None


def test_a_first_line_matching_several_companies_resolves_to_none() -> None:
    """Zero or several matches are staff's case, never a silent pick — the
    several-match answer is what flags the future ③ handoff."""
    same_name = _references((COMPANY, CR, BRN), (COMPANY, "7654321", "12345678"))
    assert match_company_reference("Kairos Trading Limited", same_name) is None

    name_and_number = _references((COMPANY, CR, BRN), ("Other Harbour Limited", CR, None))
    assert match_company_reference("CR 1234567", name_and_number) is None

    shared_number = _references(
        ("Kairos Trading Limited", CR, None),
        ("Kairos Consulting Limited", None, CR),
    )
    assert match_company_reference("CR 1234567", shared_number) is None


def test_an_empty_company_set_resolves_to_none() -> None:
    assert match_company_reference("Kairos Trading Limited", []) is None


# --- The matched-set shape (workstream ③'s E2 — the several-answer) -----------


def test_match_company_references_returns_the_matched_set() -> None:
    """Workstream ③'s E2 discriminates several from zero: the exactly-one
    public API keeps its shape, and the set-returning internals it wraps name
    what matched — several companies is a split handoff, not an
    ``answer_unresolved`` re-ask."""
    companies = _references(
        (COMPANY, CR, BRN),
        ("Other Harbour Limited", "7654321", "12345678"),
    )
    same_name = CompanyReference(
        company_id=companies[1].company_id,
        name=COMPANY,
        cr_number="7654321",
        br_number=None,
    )
    assert match_company_references("Some Other Limited", companies) == frozenset()
    assert match_company_references("CR 1234567", companies) == frozenset(
        {companies[0].company_id}
    )
    assert match_company_references("Kairos Trading Limited", [companies[0], same_name]) == (
        frozenset({companies[0].company_id, companies[1].company_id})
    )


def test_match_company_reference_stays_the_exactly_one_answer() -> None:
    """The public grammar is unchanged: exactly one match resolves, zero or
    several stay ``None`` — the set shape is the addition, not a change."""
    companies = _references(
        (COMPANY, CR, BRN),
        ("Other Harbour Limited", "7654321", "12345678"),
    )
    several = CompanyReference(
        company_id=companies[1].company_id,
        name=COMPANY,
        cr_number="7654321",
        br_number=None,
    )
    assert (
        match_company_reference("Kairos Trading Limited", [companies[0], several]) is None
    )
    assert match_company_reference("CR 7654321", companies) == companies[1].company_id
