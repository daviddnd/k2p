"""The reconfirmer's decisions and the deterministic bilingual restatement
letter (scope §2.5, §7; the M5b slice).

Scope §7's guardrail — "Every extracted change is sent back to the client as
a structured reconfirmation before it is treated as approved" — and ruling R2:
the letter is a **deterministic template** composed from the structured parse,
not model prose. This module owns the two pure halves of that: the decision
table (which reply asks for which letter) and the letter's restatement
content. The sending half — records registry, outbox row, the client
destination's ``SendMode`` release — is ``app.application.reconfirmation``'s,
reusing the M2 machinery; there is no second send path.

**The decision table** is one pure function over what the parse cycle knows:
the parsed status, the §2.7 routing band, and the client destination's send
mode, plus the facts that decide between the four rows — whether the parse was
schema-valid, how many change events it proposed, and whether any parsed field
carries the model's ``uncertain`` flag. Four decisions, in precedence order:

- ``staff_review`` — the parse failed the closed grammar, or the gateway
  refused (``parse_valid=False``): anything outside the grammar is never
  trusted with a client letter; a wholly ``unclear``/``off_topic`` reply with
  nothing parsed and nothing uncertain has no question to ask either — staff
  read the reply itself; and the leftover cases (``changes_reported`` with no
  events; a staff_review-band ``confirmed_no_change`` whose failed post-check
  left nothing quotable) are contradictions a person resolves.
- ``no_letter`` — a clean ``confirmed_no_change`` in the ``auto_accept`` band
  (or with nothing scored) with no events and no uncertainties (ruling R6):
  the confirmations are the record, and asking again would be noise. A
  staff_review-band no-change reply is *not* clean — its failed post-check
  left evidence unquotable — and falls through to staff.
- ``clarification_letter`` — uncertainties anywhere, or a
  ``client_clarification`` routing: the restating letter plus per-field
  questions quoting the client's own words (the evidence spans, which the
  post-checks have already proven verbatim).
- ``reconfirmation_letter`` — everything else with proposed change events in
  the ``auto_accept``/``staff_review`` band: each confirmation answer and each
  proposed change restated, asking the client to reply CONFIRM or correct by
  line (scope §2.5's exact shape).

The send mode is taken because the caller's record of the decision carries
everything the sending half needs — and pinned by the tests: **the decision is
mode-independent**. Ruling R7's holding (``automatic`` releases,
``review_required`` waits for staff) is the outbox's behavior, not a different
decision; one decision vocabulary, one release mechanism.

**The letter** names the company by name and CR number in both language
halves (scope §7's identification rule), restates each confirmation by the
asked question and its ``fact_id`` with the client's answer, each proposed
change with its type, party and effective date, and each clarification with
the client's own words in quotes. The CR number falls back to the BRN exactly
as the confirmation request's letter does — the same CR-else-BRN fallback the
subject line and the resolver use. **The copy was approved by the founder on
2026-10-05.** Like the confirmation and reminder copy it is regulated client
communication, **so a change to it needs the founder's or a company-secretary
reviewer's approval again before any deployment sends it**. The tests below
pin the approved rendering verbatim, so an unapproved edit fails the suite.

Pure and framework-free per ADR-0006: the standard library and ``app.domain``
only.
"""

# This module *is* the client-facing bilingual copy. Its Traditional-Chinese
# sentences are written with fullwidth CJK punctuation, which ruff's RUF001
# reads as an ambiguous look-alike — a rule that guards code, not a letter a
# client reads. The suppression is deliberate and file-wide; the only
# non-ASCII characters in this module are the copy itself.
# ruff: noqa: RUF001

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from enum import StrEnum

from app.domain.change_events import ChangeEventType
from app.domain.confidence import ConfidenceBand
from app.domain.reply_parsing import ConfirmationAnswer, ReplyStatus
from app.domain.send_modes import SendMode


class ReconfirmationDecision(StrEnum):
    """What the reconfirmer does with one parsed reply (the four-row table)."""

    NO_LETTER = "no_letter"
    RECONFIRMATION_LETTER = "reconfirmation_letter"
    CLARIFICATION_LETTER = "clarification_letter"
    STAFF_REVIEW = "staff_review"


@dataclass(frozen=True)
class ParseOutcome:
    """What the parse cycle knows about one reply — the decision table's input.

    ``parse_valid`` is False when the output failed the closed grammar or the
    gateway refused (nothing was parsed). ``change_event_count`` and
    ``has_uncertainties`` (any parsed confirmation, event or party carries the
    model's ``uncertain`` flag) are the caller's projections of the parse;
    grammar-incompleteness already routed the parse to ``client_clarification``
    before this table is asked. ``send_mode`` is the client destination's
    mode; the decision is mode-independent (the module docstring's R7 note).
    """

    status: ReplyStatus
    routing: ConfidenceBand | None
    send_mode: SendMode
    parse_valid: bool = True
    change_event_count: int = 0
    has_uncertainties: bool = False


def decide(outcome: ParseOutcome) -> ReconfirmationDecision:
    """The decision table over one parse outcome — see the module docstring.

    The order is the precedence: a parse that cannot be trusted never writes
    to the client; nothing-to-ask is answered with silence; uncertainties ask
    for clarification; proposed events ask for reconfirmation; whatever is
    left is a contradiction a person resolves.
    """
    if not outcome.parse_valid:
        return ReconfirmationDecision.STAFF_REVIEW
    if (
        outcome.status in (ReplyStatus.UNCLEAR, ReplyStatus.OFF_TOPIC)
        and outcome.change_event_count == 0
        and not outcome.has_uncertainties
    ):
        return ReconfirmationDecision.STAFF_REVIEW
    if (
        outcome.routing is ConfidenceBand.CLIENT_CLARIFICATION
        or outcome.has_uncertainties
    ):
        return ReconfirmationDecision.CLARIFICATION_LETTER
    if (
        outcome.status is ReplyStatus.CONFIRMED_NO_CHANGE
        and outcome.change_event_count == 0
        and outcome.routing in (ConfidenceBand.AUTO_ACCEPT, None)
    ):
        return ReconfirmationDecision.NO_LETTER
    if outcome.change_event_count > 0:
        return ReconfirmationDecision.RECONFIRMATION_LETTER
    return ReconfirmationDecision.STAFF_REVIEW


@dataclass(frozen=True)
class ConfirmationLine:
    """One confirmation restated: the asked question (already bilingual, as
    the checklist stores it), the client's answer, and the ``fact_id``
    coordinate the reply joined on."""

    fact_id: str
    question: str
    answer: ConfirmationAnswer


@dataclass(frozen=True)
class ProposedChangeLine:
    """One proposed change restated: the event type, the party, the effective
    date — with the client's own wording as the fallback when the parse could
    not pin an ISO date."""

    event_type: ChangeEventType
    party_name: str
    effective_date: date | None
    effective_date_text: str = ""


@dataclass(frozen=True)
class ClarificationLine:
    """One clarification question: the field that needs it, and the client's
    own words to quote back — ``None`` when there is nothing quotable (a
    missing fact the client never stated)."""

    field: str
    quote: str | None = None


_ANSWER_TEXT: dict[ConfirmationAnswer, tuple[str, str]] = {
    ConfirmationAnswer.CONFIRMED: ("Confirmed", "確認無誤"),
    ConfirmationAnswer.DENIED: ("Not correct", "不正確"),
    ConfirmationAnswer.NOT_ADDRESSED: ("Not addressed", "未提及"),
}

_EVENT_TYPE_TEXT: dict[ChangeEventType, tuple[str, str]] = {
    ChangeEventType.DIRECTOR_APPOINTMENT: ("Appointment of a director", "委任董事"),
    ChangeEventType.DIRECTOR_CESSATION: ("Cessation of a director", "董事離職"),
    ChangeEventType.SECRETARY_APPOINTMENT: (
        "Appointment of the company secretary",
        "委任公司秘書",
    ),
    ChangeEventType.SECRETARY_CESSATION: (
        "Cessation of the company secretary",
        "公司秘書離任",
    ),
    ChangeEventType.REGISTERED_OFFICE_CHANGE: ("Change of registered office", "更改註冊辦事處"),
    ChangeEventType.OTHER: ("Other change", "其他變更"),
}


def _confirmation_items(confirmations: Sequence[ConfirmationLine]) -> list[str]:
    items = []
    for index, line in enumerate(confirmations, start=1):
        answer_en, answer_zh = _ANSWER_TEXT[line.answer]
        items.append(
            f"{index}. {line.question}\n"
            f"   Your answer / 貴台回覆：{answer_en} / {answer_zh}"
            f"（fact: {line.fact_id}）"
        )
    return items


def _change_items(changes: Sequence[ProposedChangeLine]) -> list[str]:
    items = []
    for index, line in enumerate(changes, start=1):
        type_en, type_zh = _EVENT_TYPE_TEXT[line.event_type]
        if line.effective_date is not None:
            iso = line.effective_date.isoformat()
            when = f"effective {iso} / 生效日期 {iso}"
        elif line.effective_date_text:
            when = (
                f'effective date as you wrote it — "{line.effective_date_text}" / '
                f"貴台所寫生效日期：「{line.effective_date_text}」"
            )
        else:
            when = "effective date not stated / 生效日期未明"
        items.append(f"{index}. {type_en} / {type_zh} — {line.party_name} — {when}")
    return items


def _clarification_items(clarifications: Sequence[ClarificationLine]) -> list[str]:
    items = []
    for index, line in enumerate(clarifications, start=1):
        if line.quote is not None:
            detail = (
                f'You wrote / 貴台曾寫：「{line.quote}」 — please confirm what you meant. / '
                "請確認 貴台意思。"
            )
        else:
            detail = (
                "Your reply did not state this clearly — please tell us. / "
                "貴台回覆未有清楚說明此項，敬請告知。"
            )
        items.append(f"{index}. {line.field}\n   {detail}")
    return items


def restatement_subject(
    *, company_name: str, cr_number: str | None, br_number: str = ""
) -> str:
    """The subject the restatement letter goes out with (the family's own
    shape — the request's ``[<name> — CR <number>] <phrase>`` line, the
    reminder's fresh descriptive subject rather than a ``Re:`` prefix, since
    the thread is carried by the ``In-Reply-To``/``References`` headers the
    application layer sets).

    The company is identified by name and number exactly the way the body
    identifies it (scope §7); the number falls back to the BRN the same way.
    """
    label, number = ("CR", cr_number) if cr_number else ("BRN", br_number)
    return (
        f"[{company_name} — {label} {number}] "
        "Reconfirmation — please confirm the changes you reported"
    )


def restatement_letter(
    *,
    company_name: str,
    cr_number: str | None,
    br_number: str = "",
    confirmations: Sequence[ConfirmationLine] = (),
    changes: Sequence[ProposedChangeLine] = (),
    clarifications: Sequence[ClarificationLine] = (),
) -> str:
    """The deterministic bilingual restatement, one body in both languages.

    English first, then the Traditional-Chinese paragraphs saying the same
    thing — the confirmation request's own shape — with the numbered
    restatement carrying both languages per line, so one number is one item
    whichever language the client reads. The company is identified by name and
    CR number in both halves (scope §7); the number falls back to the BRN the
    way the request letter does. Sections the parse did not produce are
    omitted; a letter with nothing to restate is a caller bug, refused.
    """
    if not (confirmations or changes or clarifications):
        raise ValueError("a restatement letter has nothing to restate")
    label_en, label_zh, number = (
        ("CR Number", "公司註冊編號", cr_number)
        if cr_number
        else ("BRN", "商業登記號碼", br_number)
    )
    sections = []
    confirmation_items = _confirmation_items(confirmations)
    if confirmation_items:
        header = "Confirmations and your answers / 確認事項及 貴台回覆：\n"
        sections.append(header + "\n".join(confirmation_items))
    change_items = _change_items(changes)
    if change_items:
        header = "Changes you reported / 貴台通知的變更：\n"
        sections.append(header + "\n".join(change_items))
    clarification_items = _clarification_items(clarifications)
    if clarification_items:
        header = "Clarifications we need / 需要澄清之處：\n"
        sections.append(header + "\n".join(clarification_items))
    body = "\n\n".join(sections)
    return (
        "Dear Sir or Madam / 敬啟者：\n"
        "\n"
        f"Thank you for your reply regarding {company_name} ({label_en} {number}). "
        "Please check each numbered line below and reply to this email: write "
        "CONFIRM beside any line that is correct, or write the correction "
        "beside any line that is wrong.\n"
        "\n"
        f"多謝 貴台就{company_name}（{label_zh}{number}）回覆。請核對以下各編號事項，並回覆"
        "本郵件：正確者請註明「確認」，有誤者請直接在該項寫明更正內容。\n"
        "\n"
        f"Company / 公司：{company_name}\n"
        f"{label_en} / {label_zh}：{number}\n"
        "\n"
        f"{body}\n"
        "\n"
        "Yours faithfully / 敬啟\n"
    )


__all__ = [
    "ClarificationLine",
    "ConfirmationLine",
    "ParseOutcome",
    "ProposedChangeLine",
    "ReconfirmationDecision",
    "decide",
    "restatement_letter",
    "restatement_subject",
]
