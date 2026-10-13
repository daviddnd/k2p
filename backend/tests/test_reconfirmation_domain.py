"""Reconfirmation domain tests (scope §2.5, §7; the M5b slice).

Pure rules, no I/O: the four-way decision table over (parse outcome, routing
band, client send mode) — nothing-to-ask / reconfirmation letter /
clarification letter / routed to staff — and the deterministic bilingual
restatement letter composed from a structured parse. The sending half (outbox,
release, delivery) lives in ``test_reconfirmation_scenarios_db.py``.
"""

# These tests pin the letter's bilingual copy verbatim — the Traditional-
# Chinese strings carry fullwidth CJK punctuation, which ruff's RUF001 reads as
# ambiguous look-alikes. The rule guards code, not a letter a client reads.
# ruff: noqa: RUF001

from datetime import date

import pytest

from app.domain.change_events import ChangeEventType
from app.domain.confidence import ConfidenceBand
from app.domain.reconfirmation import (
    ClarificationLine,
    ConfirmationLine,
    ParseOutcome,
    ProposedChangeLine,
    ReconfirmationDecision,
    decide,
    restatement_letter,
)
from app.domain.reply_parsing import ConfirmationAnswer, ReplyStatus
from app.domain.send_modes import SendMode

BOTH_MODES = (SendMode.AUTOMATIC, SendMode.REVIEW_REQUIRED)


def _outcome(
    *,
    status: ReplyStatus = ReplyStatus.CONFIRMED_NO_CHANGE,
    routing: ConfidenceBand | None = ConfidenceBand.AUTO_ACCEPT,
    send_mode: SendMode = SendMode.AUTOMATIC,
    parse_valid: bool = True,
    change_event_count: int = 0,
    has_uncertainties: bool = False,
) -> ParseOutcome:
    return ParseOutcome(
        status=status,
        routing=routing,
        send_mode=send_mode,
        parse_valid=parse_valid,
        change_event_count=change_event_count,
        has_uncertainties=has_uncertainties,
    )


def test_the_decision_vocabulary_is_the_four_the_slice_names() -> None:
    assert [decision.value for decision in ReconfirmationDecision] == [
        "no_letter",
        "reconfirmation_letter",
        "clarification_letter",
        "staff_review",
    ]


# --- The decision table ------------------------------------------------------


@pytest.mark.parametrize("send_mode", BOTH_MODES)
def test_a_clean_no_change_reply_gets_no_letter(send_mode: SendMode) -> None:
    """R6: a clean ``confirmed_no_change`` — no events, no uncertainties — asks
    nothing; the confirmations are the record."""
    outcome = _outcome(status=ReplyStatus.CONFIRMED_NO_CHANGE, send_mode=send_mode)
    assert decide(outcome) is ReconfirmationDecision.NO_LETTER


@pytest.mark.parametrize("send_mode", BOTH_MODES)
def test_proposed_changes_ask_for_a_reconfirmation_letter(send_mode: SendMode) -> None:
    """Design (b): events proposed in the auto_accept or staff_review band get
    the restating letter — the client confirms or corrects line by line."""
    for routing in (ConfidenceBand.AUTO_ACCEPT, ConfidenceBand.STAFF_REVIEW):
        outcome = _outcome(
            status=ReplyStatus.CHANGES_REPORTED,
            routing=routing,
            send_mode=send_mode,
            change_event_count=1,
        )
        assert decide(outcome) is ReconfirmationDecision.RECONFIRMATION_LETTER


@pytest.mark.parametrize("send_mode", BOTH_MODES)
def test_uncertainties_ask_for_a_clarification_letter(send_mode: SendMode) -> None:
    """Design (c): uncertainties anywhere — or a client_clarification routing —
    get the same letter plus per-field clarification questions."""
    outcome = _outcome(
        status=ReplyStatus.CHANGES_REPORTED,
        routing=ConfidenceBand.AUTO_ACCEPT,
        send_mode=send_mode,
        change_event_count=1,
        has_uncertainties=True,
    )
    assert decide(outcome) is ReconfirmationDecision.CLARIFICATION_LETTER

    outcome = _outcome(
        status=ReplyStatus.CHANGES_REPORTED,
        routing=ConfidenceBand.CLIENT_CLARIFICATION,
        send_mode=send_mode,
        change_event_count=1,
    )
    assert decide(outcome) is ReconfirmationDecision.CLARIFICATION_LETTER

    # uncertainties on the confirmations alone — no events — still ask.
    outcome = _outcome(
        status=ReplyStatus.CONFIRMED_NO_CHANGE,
        routing=ConfidenceBand.STAFF_REVIEW,
        send_mode=send_mode,
        has_uncertainties=True,
    )
    assert decide(outcome) is ReconfirmationDecision.CLARIFICATION_LETTER


def test_an_invalid_parse_is_routed_to_staff() -> None:
    """Design (d): schema-invalid output, a gateway refusal — anything outside
    the closed grammar is never trusted with a client letter."""
    outcome = _outcome(
        status=ReplyStatus.CONFIRMED_NO_CHANGE,
        routing=ConfidenceBand.AUTO_ACCEPT,
        parse_valid=False,
    )
    assert decide(outcome) is ReconfirmationDecision.STAFF_REVIEW


@pytest.mark.parametrize("status", [ReplyStatus.UNCLEAR, ReplyStatus.OFF_TOPIC])
def test_a_wholly_unclear_or_off_topic_reply_is_routed_to_staff(
    status: ReplyStatus,
) -> None:
    """Design (d): wholly unclear + unparseable — no events, no uncertainties —
    means there is nothing to ask; staff read the reply itself."""
    outcome = _outcome(status=status, routing=None)
    assert decide(outcome) is ReconfirmationDecision.STAFF_REVIEW


def test_changes_reported_without_events_is_routed_to_staff() -> None:
    """A ``changes_reported`` status with no parsed events is a contradiction —
    not something to put in front of the client."""
    outcome = _outcome(status=ReplyStatus.CHANGES_REPORTED, change_event_count=0)
    assert decide(outcome) is ReconfirmationDecision.STAFF_REVIEW


def test_confirmations_with_failed_evidence_are_routed_to_staff() -> None:
    """A staff_review-band parse with no model uncertainties and no events:
    a failed post-check (a span that is not verbatim) is not quotable back to
    the client, so staff read it rather than a letter going out."""
    outcome = _outcome(
        status=ReplyStatus.CONFIRMED_NO_CHANGE, routing=ConfidenceBand.STAFF_REVIEW
    )
    assert decide(outcome) is ReconfirmationDecision.STAFF_REVIEW


def test_a_withdrawal_with_a_match_asks_for_reconfirmation() -> None:
    outcome = _outcome(
        status=ReplyStatus.WITHDRAWAL,
        routing=ConfidenceBand.AUTO_ACCEPT,
        change_event_count=1,
    )
    assert decide(outcome) is ReconfirmationDecision.RECONFIRMATION_LETTER


# --- The restatement letter ---------------------------------------------------


def _letter(**overrides: object) -> str:
    defaults: dict = {
        "company_name": "Kairos Trading Limited",
        "cr_number": "1234567",
        "confirmations": [
            ConfirmationLine(
                fact_id="company.registered_office",
                question="Is the registered office still Unit 5, 1 Kai Chuen Road? / "
                "註冊辦事處是否仍為啟全道1號5室？",
                answer=ConfirmationAnswer.CONFIRMED,
            )
        ],
        "changes": [
            ProposedChangeLine(
                event_type=ChangeEventType.DIRECTOR_CESSATION,
                party_name="Alice Chan",
                effective_date=date(2026, 3, 15),
            )
        ],
        "clarifications": [
            ClarificationLine(field="change_events[0].effective_date", quote="15 March 2026"),
        ],
    }
    defaults.update(overrides)
    return restatement_letter(**defaults)


def test_the_letter_identifies_the_company_by_name_and_cr_number() -> None:
    """Scope §7's guardrail: every client-facing message identifies the company
    by name and CR number — asserted for *both* language halves."""
    body = _letter()
    # The English half: intro, header line.
    assert "(CR Number 1234567)" in body
    assert "Company / 公司：Kairos Trading Limited" in body
    assert "CR Number / 公司註冊編號：1234567" in body
    # The Traditional-Chinese half carries the same identification.
    assert "（公司註冊編號1234567）" in body


def test_the_number_label_falls_back_to_the_brn() -> None:
    body = _letter(cr_number=None, br_number="87654321")
    assert "(BRN 87654321)" in body
    assert "BRN / 商業登記號碼：87654321" in body


def test_the_letter_restates_each_confirmation_by_question_and_fact() -> None:
    body = _letter()
    assert "Is the registered office still Unit 5, 1 Kai Chuen Road?" in body
    assert "註冊辦事處是否仍為啟全道1號5室？" in body
    assert "company.registered_office" in body
    assert "Confirmed / 確認無誤" in body


def test_the_letter_restates_each_change_with_type_party_and_effective_date() -> None:
    body = _letter()
    assert "Cessation of a director / 董事離職" in body
    assert "Alice Chan" in body
    assert "2026-03-15" in body


def test_the_letter_quotes_the_clients_own_words_when_requested() -> None:
    body = _letter()
    assert "「15 March 2026」" in body


def test_a_clarification_without_a_quote_asks_plainly() -> None:
    body = _letter(
        clarifications=[ClarificationLine(field="change_events[0].cessation_reason")]
    )
    assert "change_events[0].cessation_reason" in body
    assert "「15 March 2026」" not in body


def test_a_changes_only_letter_omits_the_orphan_section_headers() -> None:
    """The standard reconfirmation letter (changes, no clarifications): the
    sections the parse did not produce are omitted, not headed-and-empty."""
    body = _letter(confirmations=[], clarifications=[])
    assert "Changes you reported / 貴台通知的變更：" in body
    assert "Cessation of a director / 董事離職" in body
    assert "Confirmations and your answers" not in body
    assert "Clarifications we need" not in body


def test_a_confirmations_only_letter_omits_the_change_and_clarification_headers() -> None:
    body = _letter(changes=[], clarifications=[])
    assert "Confirmations and your answers / 確認事項及 貴台回覆：" in body
    assert "Changes you reported" not in body
    assert "Clarifications we need" not in body


def test_the_letter_rejects_an_empty_restatement() -> None:
    with pytest.raises(ValueError, match="nothing to restate"):
        restatement_letter(company_name="Kairos Trading Limited", cr_number="1234567")
