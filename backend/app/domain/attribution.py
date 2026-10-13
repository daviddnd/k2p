"""The attribution decision, the confirm grammar, and the two ask drafts
(ADR-0019 §1; the attribution registration/confirmation slice's pure half).

ADR-0019 §1 settles the instruction-authority question this module carries
the vocabulary and rules for:

- **Thread/tag metadata is attribution evidence.** A message whose provider
  thread id or ``In-Reply-To``/``References`` matches exactly one stored
  outbound exchange binds to that exchange's company — also resolving an
  address-ambiguous sender. The exactly-one match itself belongs to the
  ingestion cycle (``app.domain.reminders``' matcher, generalized per ruling
  R1); the cycle resolves it and hands this table the one fact it needs —
  whether the message is ``threaded``.
- **Instruction authority stays with the registered channels.** For a
  message from an address that is not a registered channel of the chain's
  company: when no registered channel is anywhere on the message — neither
  From nor To/Cc — the reply asks that the instruction be sent from a
  registered channel (``registration_ask_letter``); when a registered
  channel is among the To/Cc addresses (the boss-on-copy case), the reply
  asks that channel to confirm the information
  (``confirmation_ask_letter``), and a confirmation reply from a registered
  channel of the chain's company is the authorising act, after which the
  message is processed normally.
- **The unregistered sender's body is not read before the confirmation**
  (envelope/headers only, ruling R2), which is why the ask copy quotes
  envelope facts only.
- **Ambiguity is resolved by the client, not by staff and not by a model.**
  The model fallback is retired; attribution never calls a model.

**The decision table** is ``decide``: one pure function over three facts the
ingestion cycle resolved from metadata — the message's thread evidence, the
sender's registered-channel status, and the registered channels found among
the To/Cc addresses — in precedence order:

- ``plain`` — not threaded: the message is not on a stored outbound
  exchange, so today's flow (quarantine, resolver, staff) runs unchanged.
- ``process`` — threaded and the sender is a registered channel of the
  chain's company: ordinary registered-sender processing, the thread fixing
  the company.
- ``ask_confirmation`` — threaded, the sender is not a channel, and a
  registered channel sits in To/Cc: the ask goes to that channel — the
  deduped, first-seen-order recipients this decision carries.
- ``ask_registered_channel`` — threaded, the sender is not a channel and no
  registered channel is on the message: the ask goes back to the sender, so
  ``recipients`` is empty — the caller supplies it.

**The confirm grammar** is ``is_confirmation``: a reply's body confirms when
its **first non-empty line**, normalized (NFKC — fullwidth Latin included —
case-folded, stripped), equals one of ``CONFIRM_LITERALS``. Ruling R5's
other two halves — the sender is a registered channel of the case's company
and the reply matches the case's thread anchor — are the ingestion cycle's
checks; this module owns only the words of the act. Anything but that bare
first line is not a confirmation: a ``confirm`` buried in prose falls
through to normal processing or staff, never a silent authorisation.

**The case vocabulary** ``AttributionCaseStatus`` names the six states the
two slices give the ``attribution_cases`` row, and ``AttributionAskKind``
the three asks. The row and its lifecycle (``open_case``, ``enqueue_ask``,
``confirm_case``, ``process_case`` — rulings R3/R6/R7), the asks' delivery
(R4: notices, outbox kind ``attribution_ask``, no §5.1.9 release gate), the
API surface (R8) and migrations ``0033``/``0034`` (R10, D2) belong to the
application/schema/API tasks; this module only names them so both halves
speak one vocabulary.

**The clarification loop** (workstream ②, rulings D4-D6) automates the two
unambiguously deterministic-but-unattributed classes of *unthreaded* mail
that today land in staff quarantine. ``decide_unthreaded`` is the plain
path's own decision table over three facts the ingestion cycle already
holds — how many of the tenant's companies the sender is a registered
channel of, whether the deterministic resolver matched one of them, and
whether the message carries its own RFC ``Message-ID`` (the thread anchor a
case hangs on; anchor-less mail opens no case, ①'s rule extends verbatim).
An unregistered sender with an anchor is asked to use a registered channel
(``first_contact_ask_letter``, Letter B); a sender registered for ≥2
companies the resolver matched none of is asked which company the message
concerns (``clarification_ask_letter``, Letter A). Every other shape —
resolver matched, a single company, no anchor — is today's handling
(quarantine or ordinary processing), which is why the table's third word is
``quarantine``. Both ask classes' cases open with no company (D2); Letter A's
answer grammar is ``match_company_reference``: the reply's first non-empty
line, normalized, naming exactly one of the sender's companies by
normalized full name or CR/BRN digit sequence resolves the case; zero or
several matches stay with staff.

**The copy** — ``registration_ask_letter``, ``confirmation_ask_letter``,
``ask_subject``, ``clarification_ask_letter``, ``first_contact_ask_letter``,
``companyless_ask_subject`` — is a static bilingual draft, and
**founder-gated copy: approval before any deployment sends it** (ruling R9;
the M3 precedent). The §1 drafts identify the company by name and number in
both language halves (scope §7's rule), with the CR number falling back to
the BRN exactly as the confirmation-request family does; Letters A/B are
company-less (C10) — they name no company anywhere and lead the subject
with the TCSP's own name instead — and all four letters quote envelope
facts only (ruling R2): the sender's address, the original subject and the
received time — never the unregistered sender's body, which is not read
before the confirmation. The tests pin the current drafts verbatim, so an
edit re-opens that approval rather than shipping unapproved.

Pure and framework-free per ADR-0006: the standard library (``unicodedata``
for NFKC, plus ``dataclasses``/``enum``/``datetime``) and ``app.domain``
only.
"""

# This module carries the client-facing bilingual copy. Its Traditional-
# Chinese sentences are written with fullwidth CJK punctuation, which ruff's
# RUF001 reads as an ambiguous look-alike — a rule that guards code, not a
# letter a client reads. The suppression is deliberate and file-wide.
# ruff: noqa: RUF001

import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from app.domain.communications import normalized_address

# The three words the confirm act accepts, which is also the wording the
# confirmation ask invites (the tests hold the two together). ``confirm``
# covers fullwidth Latin through NFKC; the two Chinese spellings cover the
# traditional and simplified ways of writing it.
CONFIRM_LITERALS = ("confirm", "確認", "确认")


class AttributionAction(StrEnum):
    """What the ingestion cycle decides about one message (§1; ``decide``).

    ``plain`` — not threaded: today's flow runs. ``process`` — the sender is
    a registered channel of the chain's company: ordinary processing, the
    thread fixing the company. ``ask_registered_channel`` and
    ``ask_confirmation`` — the two §1 asks, to the sender and to the
    registered channel on copy respectively.
    """

    PLAIN = "plain"
    PROCESS = "process"
    ASK_REGISTERED_CHANNEL = "ask_registered_channel"
    ASK_CONFIRMATION = "ask_confirmation"


class AttributionCaseStatus(StrEnum):
    """The ``attribution_cases`` row's states (rulings R3/D1; migrations
    ``0033``/``0034``).

    One row per original message. ``awaiting_registered_channel`` and
    ``awaiting_confirmation`` are ①'s two open awaits;
    ``awaiting_clarification`` (workstream ②, ruling D1) is the third — an
    ambiguous multi-company sender's case, opened with no company. ``resolved``
    is the original processed after the confirmation; ``closed`` is R7's
    closure (a registered channel's own message arrived on the thread);
    ``dismissed`` is staff deciding the case. The transitions are the
    application layer's — named here so both halves speak one vocabulary.
    """

    AWAITING_REGISTERED_CHANNEL = "awaiting_registered_channel"
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    AWAITING_CLARIFICATION = "awaiting_clarification"
    RESOLVED = "resolved"
    CLOSED = "closed"
    DISMISSED = "dismissed"


class AttributionAskKind(StrEnum):
    """Which of the asks a case carries (rulings R4/D5).

    ``registered_channel`` is the ask that the instruction be sent from a
    registered channel (no registered channel on the message);
    ``confirmation`` is the ask to the registered channel found on copy to
    confirm the information; ``clarification`` (workstream ②) is the
    which-company ask to a sender registered for several. The kind selects
    the subject phrase; the caller composes the matching letter.
    """

    REGISTERED_CHANNEL = "registered_channel"
    CONFIRMATION = "confirmation"
    CLARIFICATION = "clarification"


class UnthreadedAction(StrEnum):
    """What the plain path decides about one unthreaded message (ruling D4).

    ``ask_first_contact`` — B-class: the sender is a registered channel of
    no company, so the first-contact ask (Letter B) replaces the
    ``UNATTRIBUTED_SENDER`` quarantine. ``ask_clarification`` — A-class: the
    sender is a registered channel of ≥2 companies and the resolver matched
    none, so the which-company ask (Letter A) replaces the
    ``AMBIGUOUS_SENDER``/``MULTIPLE_COMPANIES`` quarantine. ``quarantine``
    is every other shape: today's handling (quarantine or ordinary
    processing, which the caller's own resolver outcome already decides) runs
    unchanged.
    """

    QUARANTINE = "quarantine"
    ASK_FIRST_CONTACT = "ask_first_contact"
    ASK_CLARIFICATION = "ask_clarification"


@dataclass(frozen=True)
class CompanyReference:
    """The company facts the answer grammar matches a reply's line against.

    The minimal slice of a company the grammar needs: its id (what a match
    resolves to), its full name, and its CR/BRN numbers. The application
    layer assembles these from the sender's own registered companies —
    matching never leaves the sender's set.
    """

    company_id: UUID
    name: str
    cr_number: str | None = None
    br_number: str | None = None


@dataclass(frozen=True)
class AttributionDecision:
    """What ``decide`` decided, and (for the confirmation ask) to whom.

    ``recipients`` is empty for every action but ``ask_confirmation``: the
    registered-channel ask goes back to the sender, which the caller holds;
    ``plain`` and ``process`` send nothing.
    """

    action: AttributionAction
    recipients: tuple[str, ...]


def _copy_channels(registered_channels_in_copy: Sequence[str]) -> tuple[str, ...]:
    """The To/Cc registered channels as recipients: normalized, deduped, in
    first-seen order.

    Addresses compare in ``app.domain.communications.normalized_address``'s
    comparison form (trimmed, case-folded) — one home for that rule — so the
    same channel reached as To and as Cc (any casing) is one recipient: kept
    once, at its first appearance, in that comparison form. A blank entry is
    not a channel.
    """
    seen: set[str] = set()
    channels: list[str] = []
    for address in registered_channels_in_copy:
        normalized = normalized_address(address)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        channels.append(normalized)
    return tuple(channels)


def decide(
    *,
    threaded: bool,
    sender_is_channel: bool,
    registered_channels_in_copy: Sequence[str],
) -> AttributionDecision:
    """The §1 decision table — see the module docstring for the four rows.

    The inputs are the ingestion cycle's metadata-only resolutions: whether
    the message matched exactly one stored outbound exchange (``threaded``),
    whether the sender is a registered channel of that exchange's company
    (``sender_is_channel``), and which of the To/Cc addresses are registered
    channels of that company (``registered_channels_in_copy``). Nothing here
    reads the message.
    """
    if not threaded:
        return AttributionDecision(action=AttributionAction.PLAIN, recipients=())
    if sender_is_channel:
        return AttributionDecision(action=AttributionAction.PROCESS, recipients=())
    recipients = _copy_channels(registered_channels_in_copy)
    if recipients:
        return AttributionDecision(
            action=AttributionAction.ASK_CONFIRMATION, recipients=recipients
        )
    return AttributionDecision(
        action=AttributionAction.ASK_REGISTERED_CHANNEL, recipients=()
    )


def decide_unthreaded(
    *,
    registered_company_count: int,
    resolver_matched: bool,
    has_anchor: bool,
) -> UnthreadedAction:
    """The plain path's decision table (ruling D4) — see the module
    docstring's clarification-loop section for the two ask classes.

    The inputs are the ingestion cycle's own resolutions: how many of the
    tenant's companies the sender is a registered channel of, whether the
    deterministic resolver matched one of them in the content, and whether
    the message carries its own RFC ``Message-ID`` (the anchor a case hangs
    on — ①'s anchor-less rule extends verbatim: no anchor, no case). A
    single-company sender and a resolver that matched are never asks:
    today's flow attributes or quarantines on the resolver's own answer, so
    this table hands them back ``quarantine`` and leaves the caller's
    today's handling to run.
    """
    if not has_anchor:
        return UnthreadedAction.QUARANTINE
    if registered_company_count == 0:
        return UnthreadedAction.ASK_FIRST_CONTACT
    if registered_company_count >= 2 and not resolver_matched:
        return UnthreadedAction.ASK_CLARIFICATION
    return UnthreadedAction.QUARANTINE


def is_confirmation(body_text: str) -> bool:
    """Whether a reply's body is the confirm act (ruling R5's notation half).

    The body's **first non-empty line** — leading blank lines skipped, the
    trailing signature and quoted history below it ignored — normalized by
    NFKC (fullwidth Latin becomes ASCII, the ideographic space a space),
    case-folding and stripping, must equal one of ``CONFIRM_LITERALS``
    exactly. Anything else is not a confirmation: prose that merely contains
    the word, another language's question about it (``確認しますか``), or a
    line with anything appended is left for normal processing or staff, and
    never silently authorises the original.
    """
    for line in body_text.splitlines():
        if not line.strip():
            continue
        normalized = unicodedata.normalize("NFKC", line).casefold().strip()
        return normalized in CONFIRM_LITERALS
    return False


def _reference_digits(value: str | None) -> str:
    """The comparison form for a CR/BRN (or a line holding one): its digit
    sequence alone, as a string — formatting is ignored on both sides."""
    if not value:
        return ""
    return "".join(character for character in value if character.isdigit())


def match_company_references(
    first_line: str, companies: Sequence[CompanyReference]
) -> frozenset[UUID]:
    """The matched-set shape of the answer grammar (workstream ③'s E2):
    every company the line names, by normalized full-name equality or
    CR/BRN digit-sequence equality (ruling D6's normalization, verbatim).

    The exactly-one public ``match_company_reference`` wraps this and keeps
    ②'s semantics; the set shape exists because ③ discriminates several
    from zero — several matches are the split handoff, not an unresolved
    answer, and the handoff needs the involved set.
    """
    line = unicodedata.normalize("NFKC", first_line).casefold().strip()
    if not line:
        return frozenset()
    line_digits = _reference_digits(line)
    return frozenset(
        company.company_id
        for company in companies
        if unicodedata.normalize("NFKC", company.name).casefold().strip() == line
        or (
            bool(line_digits)
            and line_digits
            in (_reference_digits(company.cr_number), _reference_digits(company.br_number))
        )
    )


def match_company_reference(
    first_line: str, companies: Sequence[CompanyReference]
) -> UUID | None:
    """Whether a reply's first non-empty line names exactly one of the
    sender's companies (ruling D6's answer grammar) — and which.

    The line is normalized by NFKC (fullwidth Latin and the ideographic
    space become ASCII), case-folding and stripping, and matches a company
    by **normalized full-name equality** or **CR/BRN digit-sequence
    equality** (the digits of the line compared as one string against the
    digits of the stored number). Exactly one distinct company matched
    resolves to that company's id; zero (an unanswerable line) or several
    (workstream ③'s split handoff, which reads the set through
    ``match_company_references``) resolve to ``None`` here and stay with
    staff.
    """
    matched = match_company_references(first_line, companies)
    if len(matched) == 1:
        return next(iter(matched))
    return None


# --- The ask copy (ruling R9) -------------------------------------------------
#
# Founder-gated copy — approval before any deployment sends it. The letters
# and the subject phrases below are static bilingual drafts: regulated client
# communication that a deployment must not send until the founder has
# approved this exact wording, recorded. An edit re-opens that approval (the
# M3 precedent); the tests pin the drafts verbatim, so an unapproved drift
# fails the suite instead of shipping.

_ASK_PHRASES: dict[AttributionAskKind, str] = {
    AttributionAskKind.REGISTERED_CHANNEL: (
        "Please instruct us from a registered email address"
    ),
    AttributionAskKind.CONFIRMATION: (
        "Please confirm the message we received on your behalf"
    ),
}


def ask_subject(
    *,
    company_name: str,
    cr_number: str | None,
    br_number: str = "",
    kind: AttributionAskKind,
) -> str:
    """The subject an ask goes out with (the family's own shape).

    ``[<name> — CR <number>] <phrase>`` — the confirmation request's subject
    shape, with the BRN standing in for a CR number the master data does not
    hold, exactly as the request and restatement subjects fall back. The
    phrase is the kind's own English notice; the letters themselves are
    bilingual.
    """
    label, number = ("CR", cr_number) if cr_number else ("BRN", br_number)
    return f"[{company_name} — {label} {number}] {_ASK_PHRASES[kind]}"


def registration_ask_letter(
    *, company_name: str, cr_number: str | None, br_number: str = ""
) -> str:
    """The ask that an instruction be sent from a registered channel.

    ADR-0019 §1's first ask: no registered channel is on the message at all,
    so the reply names the company and asks the sender to re-send the
    instruction from an address registered with the TCSP for it. The letter
    quotes no part of the message — the sender is the ask's recipient, and
    the body was not read (R2). English first, then the Traditional-Chinese
    paragraphs saying the same thing, the company identified in both halves
    (§7) with the CR-else-BRN fallback.
    """
    label_en, label_zh, number = (
        ("CR Number", "公司註冊編號", cr_number)
        if cr_number
        else ("BRN", "商業登記號碼", br_number)
    )
    return (
        "Dear Sir or Madam / 敬啟者：\n"
        "\n"
        f"We received a message that appears to concern {company_name} "
        f"({label_en} {number}). To protect the company, we act on instructions "
        "only when they come from an email address registered with us for that "
        "company. Please send your instruction from a registered email address; "
        "if you have not registered an address with us, please contact our staff.\n"
        "\n"
        f"本公司收到一則看來與{company_name}（{label_zh}{number}）有關的訊息。"
        "為保障公司，本公司僅會處理由已登記電郵地址發出的指示。請使用 貴台已向本公司"
        "登記的電郵地址重新發送指示；如 貴台尚未向本公司登記任何電郵地址，請與本公司職員聯絡。\n"
        "\n"
        f"Company / 公司：{company_name}\n"
        f"{label_en} / {label_zh}：{number}\n"
        "\n"
        "Yours faithfully / 敬啟\n"
    )


def confirmation_ask_letter(
    *,
    company_name: str,
    cr_number: str | None,
    br_number: str = "",
    sender_address: str,
    original_subject: str,
    original_received_at: datetime,
) -> str:
    """The ask to the registered channel found on copy — confirm the message.

    ADR-0019 §1's boss-on-copy ask: the message came from an address that is
    not a registered channel of the company, and a registered channel is
    among its recipients, so that channel is asked whether the message
    reflects its instruction (the confirm act is ``is_confirmation``'s
    grammar, and the letter invites exactly those words). The letter quotes
    envelope facts only (R2) — the sender's address, the original subject
    and the time it was received — and never the message's body, which is
    not read before the confirmation. English first, then the
    Traditional-Chinese paragraphs saying the same thing, the company
    identified in both halves (§7) with the CR-else-BRN fallback.
    """
    label_en, label_zh, number = (
        ("CR Number", "公司註冊編號", cr_number)
        if cr_number
        else ("BRN", "商業登記號碼", br_number)
    )
    received = original_received_at.isoformat()
    return (
        "Dear Sir or Madam / 敬啟者：\n"
        "\n"
        f"We received a message about {company_name} ({label_en} {number}) from "
        f"{sender_address}; it reached us at {received}. That address is not "
        "registered with us for this company, so we cannot act on the message "
        "yet. An email address registered with us for this company is among the "
        "message's recipients: please confirm that the message reflects your "
        "instruction by replying to this email with the single word CONFIRM on "
        "the first line, and we will process it. If you do not recognise the "
        "message, please tell us.\n"
        "\n"
        f"本公司收到一則關於{company_name}（{label_zh}{number}）的訊息，由"
        f"{sender_address}發出，收到時間為{received}。該發件地址並非已向本公司登記的"
        "電郵地址，故本公司暫未能處理該訊息。訊息收件人中有 貴台已向本公司登記的電郵"
        "地址：請回覆本郵件，並在首行寫上「確認」二字，確認該訊息代表 貴台的指示，"
        "本公司將據此處理。如 貴台並不認識該訊息，請告知本公司。\n"
        "\n"
        f"Company / 公司：{company_name}\n"
        f"{label_en} / {label_zh}：{number}\n"
        f"From / 寄件人：{sender_address}\n"
        f"Subject / 原郵件主題：{original_subject}\n"
        f"Received / 收到時間：{received}\n"
        "\n"
        "Yours faithfully / 敬啟\n"
    )


# --- The company-less asks (rulings D4/D5; workstream ②) ------------------------
#
# Letters A and B are founder-gated copy exactly like the two §1 letters
# above: static bilingual drafts pinned verbatim in the tests, so an edit
# re-opens the founder's approval rather than shipping unapproved. They name
# no company (C10) and quote envelope facts only (R2).

_COMPANYLESS_ASK_PHRASES: dict[AttributionAskKind, tuple[str, str]] = {
    AttributionAskKind.REGISTERED_CHANNEL: (
        "Please instruct us from a registered email address",
        "請以已登記的電郵地址發出指示",
    ),
    AttributionAskKind.CLARIFICATION: (
        "Please tell us which company your message concerns",
        "請告知 貴台訊息所涉的公司",
    ),
}


def companyless_ask_subject(*, tcsp_name: str, kind: AttributionAskKind) -> str:
    """The subject a company-less ask goes out with (C10's shape).

    ``[<TCSP name>] <phrase>`` — the company-less counterpart of the §1
    family shape: an A/B-class case names no company, so the TCSP's own
    name leads. The phrase is the kind's own bilingual notice; the letters
    themselves carry the full bilingual text.
    """
    phrase_en, phrase_zh = _COMPANYLESS_ASK_PHRASES[kind]
    return f"[{tcsp_name}] {phrase_en} / {phrase_zh}"


def clarification_ask_letter(
    *,
    sender_address: str,
    original_subject: str,
    original_received_at: datetime,
) -> str:
    """Letter A — the which-company ask (ruling D5).

    The A-class ask: the sender is a registered channel of several of the
    tenant's companies but the message does not say which one it concerns,
    so the reply asks exactly the fact ``match_company_reference`` reads —
    the company's full name or its CR/BRN number on the first line. The
    letter names none of the sender's companies (C10) and quotes envelope
    facts only (R2): the sender's address, the original subject and the
    received time — never the message's body.
    """
    received = original_received_at.isoformat()
    return (
        "Dear Sir or Madam / 敬啟者：\n"
        "\n"
        f"We received your message (subject: {original_subject}; received at {received}) "
        f"from your registered email address. The message does not say which company it "
        "concerns, so we cannot act on it yet. Please reply to this email with the "
        "company's full name, or its CR or BRN number, on the first line, and we will "
        "process your message. If it concerns more than one company, please name them "
        "all, one per line.\n"
        "\n"
        f"本公司收到由 貴台已登記電郵地址發出之訊息（主題：{original_subject}；"
        f"收到時間：{received}）。訊息並未註明涉及 貴台哪家公司，故本公司暫未能處理。"
        "請回覆本郵件，並在首行寫上所涉公司之全名，或公司註冊編號／商業登記號碼，"
        "本公司將據此處理。如涉及多於一家公司，請逐行列明。\n"
        "\n"
        f"From / 寄件人：{sender_address}\n"
        f"Subject / 原郵件主題：{original_subject}\n"
        f"Received / 收到時間：{received}\n"
        "\n"
        "Yours faithfully / 敬啟\n"
    )


def first_contact_ask_letter(
    *,
    sender_address: str,
    original_subject: str,
    original_received_at: datetime,
) -> str:
    """Letter B — the first-contact ask (ruling D5).

    The B-class ask: the sender is not a registered channel of any company
    and the message is on no known thread, so the reply — a company-less
    variant of ①'s registration ask — says the TCSP acts on instructions
    only from registered email addresses and asks the sender to re-send from
    one or contact staff. The letter names no company (C10) and quotes
    envelope facts only (R2): the sender's address, the original subject and
    the received time.
    """
    received = original_received_at.isoformat()
    return (
        "Dear Sir or Madam / 敬啟者：\n"
        "\n"
        f"We received a message from {sender_address} (subject: {original_subject}; "
        f"received at {received}). To protect our clients, we act on instructions only "
        "when they come from an email address registered with us. If your message "
        "concerns a company we serve, please send it from a registered email address, "
        "or contact our staff.\n"
        "\n"
        f"本公司收到由 {sender_address} 發出之訊息（主題：{original_subject}；"
        f"收到時間：{received}）。為保障客戶，本公司僅會處理由已登記電郵地址發出的指示。"
        "如該訊息與本公司服務之公司有關，請使用已登記的電郵地址重新發送，或與本公司"
        "職員聯絡。\n"
        "\n"
        f"From / 寄件人：{sender_address}\n"
        f"Subject / 原郵件主題：{original_subject}\n"
        f"Received / 收到時間：{received}\n"
        "\n"
        "Yours faithfully / 敬啟\n"
    )


__all__ = [
    "CONFIRM_LITERALS",
    "AttributionAction",
    "AttributionAskKind",
    "AttributionCaseStatus",
    "AttributionDecision",
    "CompanyReference",
    "UnthreadedAction",
    "ask_subject",
    "clarification_ask_letter",
    "companyless_ask_subject",
    "confirmation_ask_letter",
    "decide",
    "decide_unthreaded",
    "first_contact_ask_letter",
    "is_confirmation",
    "match_company_reference",
    "match_company_references",
    "registration_ask_letter",
]
