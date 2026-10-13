"""Confirmation-request domain rules (scope §2.5, §7; data-model §2.9, §5.1.8).

The request the product writes to a client when an annual return is coming
due: which facts it asks about, what the client is asked, the bilingual
message that asks it, and — since slice M2 — the request's **send lifecycle**
(``RequestStatus`` and the transition table below, which is where "sent" is
defined). It is deliberately **static** — scope §2.5 says confirmation emails
go out "bilingually (English + Traditional Chinese) from static templates in
v1.0" (ruled 2026-09-23) — so nothing here calls a model, and nothing here
sends: the composed message is stored in the records registry, the release is
a status move this module's table governs, and the delivery itself lives in
``app.application.mail_send``.

**The fact vocabulary** (``FACTS``) is this module's own: one entry per
question subject, in the fixed §3 order, each naming the master-data group it
reads and the ``prior_filing.TARGETS`` paths its value can be written to — the
paths are *referenced*, never restated, and a test holds them to that
vocabulary. Two kinds of fact share the table:

- a **readable** fact (``FactRead``): the product holds a value, and the
  client is asked to confirm it — "is it still X?", or, when nothing is in
  force, to supply it;
- an **open** fact (``read`` is ``None``): the checklist asks without a held
  value to confirm it against. NAR1's officer questions are these (ruled
  2026-09-23): officers and the company secretary have been master data since
  F1 (migration ``0025``, 2026-09-28), and a reply naming a change lands as
  change-event proposals through the reply parser (M5b, §5.1.14) — the
  questions stay open because the founder-approved copy asks the client to
  give the list and any change, not to confirm a single value.

**A checklist item carries a pointer, never a copy.** ``expected_value`` is
the value *as the client is asked about it* — the thing a "confirmed" verdict
is about — and ``source_record_id`` is the effective-dated row it came from;
the authoritative value stays in its own fact table (the same discipline
``record_fact``'s audit entry follows). Nothing here writes master data.

**The copy was approved by the founder on 2026-09-24.** The English and
Traditional Chinese wording below is regulated client communication, so
**a change to it needs the founder's or a company-secretary reviewer's
approval again before any deployment sends it** (ruled 2026-09-23, plan §7
item 6). The wording is neutral by design — a generic salutation, no
per-recipient flattery, nothing the product has not been told to say — and
``tests/test_confirmation_domain.py`` pins the approved rendering verbatim, so
an unapproved edit fails the suite.

**The ids are coordinates, never ordinals.** ``fact_id`` is
``entity.group``, derived from the fact's subject, so a regenerated request
whose checklist changed cannot silently re-key a reply onto a different fact
— which is what the reply parser will join on (``Communication.checklist[]``
with durable ``fact_id``s, contract §2.9).

Pure and framework-free per ADR-0006: the standard library's ``email`` package
and nothing else — no I/O, no database, no model.
"""

# This module *is* the client-facing bilingual copy. Its Traditional-Chinese
# sentences are written with fullwidth CJK punctuation, which ruff's RUF001
# reads as an ambiguous look-alike — a rule that guards code, not a letter a
# client reads. The suppression is deliberate and file-wide; the only
# non-ASCII characters in this module are the copy itself.
# ruff: noqa: RUF001

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from email.message import EmailMessage
from email.policy import SMTP
from email.utils import format_datetime
from enum import StrEnum
from typing import Any, Protocol
from uuid import UUID

from app.domain.company_master import CompanyType
from app.domain.compliance import anniversary_in_year, nar1_due_date
from app.domain.milestones import Milestones
from app.domain.prior_filing import FormCode

# The month names the copy is written with, spelled out rather than taken from
# ``strftime``: the subject and the body are the client's own letter, and a
# deployment's process locale must not be able to change its words.
_MONTHS = (
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
)


class RequestStatus(StrEnum):
    """The confirmation request's own status (contract §2.9; §5.1.8).

    Four states, one lifecycle. ``draft`` is written by the generator: with the
    client destination held for review (the tenant's send mode) that is where
    the request waits for a staff member. ``queued`` is the **release**: the
    message may leave (the send route released it, or the generator released it
    under the automatic mode), and an outbox row is pending for it.
    ``sent`` is the provider having accepted the message. ``superseded`` is
    what a regeneration leaves behind.

    ``draft`` and ``queued`` are the two **live** states — the ones the partial
    unique index covers — so "which request is live?" still has exactly one
    answer, and a regeneration supersedes a released-but-undelivered request
    rather than letting two letters race. A regeneration supersedes a ``sent``
    request too (§5.1.12): the new version ends the old chain, live or sent.
    """

    DRAFT = "draft"
    QUEUED = "queued"
    SENT = "sent"
    SUPERSEDED = "superseded"


class RequestAction(StrEnum):
    """What the send lifecycle can do to a request (ADR-0008's second table).

    ``RELEASE`` is the act that authorises the message (a staff member through
    the send route, or the generator under ``automatic``), ``MARK_SENT`` is the
    delivery's outcome, and ``SUPERSEDE`` is a regeneration closing the live
    request. One table, one guard, and only ``app.application.outbox`` writes a
    request's status through it.
    """

    RELEASE = "release"
    MARK_SENT = "mark_sent"
    SUPERSEDE = "supersede"


# ADR-0008's second transition table, as data. The absent pair is the point:
# there is no `draft + MARK_SENT`, because a message is released before it is
# delivered and no caller may send one that was never released.
REQUEST_TRANSITIONS: Mapping[RequestStatus, Mapping[RequestAction, RequestStatus]] = {
    RequestStatus.DRAFT: {
        RequestAction.RELEASE: RequestStatus.QUEUED,
        RequestAction.SUPERSEDE: RequestStatus.SUPERSEDED,
    },
    RequestStatus.QUEUED: {
        RequestAction.MARK_SENT: RequestStatus.SENT,
        RequestAction.SUPERSEDE: RequestStatus.SUPERSEDED,
    },
    # A regeneration supersedes a sent request too (contract §5.1.12, ruled a
    # Critical fix 2026-09-29): its reminder chain — and any pending reminder
    # rows — ends with it, so a replaced letter cannot be chased by a stale one.
    RequestStatus.SENT: {
        RequestAction.SUPERSEDE: RequestStatus.SUPERSEDED,
    },
    RequestStatus.SUPERSEDED: {},
}

# The two states a request can be worked on from, and the predicate the "one
# live request per company and form" index carries (§5.1.8).
LIVE_REQUEST_STATUSES = frozenset({RequestStatus.DRAFT, RequestStatus.QUEUED})


class UnsupportedFormError(ValueError):
    """The form is not one this slice can confirm.

    Only NAR1 is: every ND2A registry target is a ``change_event.*``, so
    there is no master data an ND2A confirmation request could confirm
    (ruled 2026-09-23, plan §7 item 3). The refusal is the point — a form
    whose questions this product cannot write must not be asked about at all.
    """

    def __init__(self, form_type: object) -> None:
        self.form_type = form_type
        super().__init__(
            f"confirmation requests do not cover form {form_type!r} "
            f"(supported: {', '.join(sorted(code.value for code in FORM_TEXTS))})"
        )


class CompanyNameUnavailableError(ValueError):
    """The company has no name in force, so no message can identify it.

    Scope §7's guardrail is that every client-facing message identifies the
    company by name and CR number, and the subject is also where a reply's
    attribution begins (§2.5). Refusing is the point: an unnamed
    confirmation request must not be written, let alone sent.
    """


class NoClientReplyWindowError(ValueError):
    """The configured reply-by would fall on or after CR's own deadline.

    Refusing is the point (the founder's ruling of 2026-09-24): a client is
    never asked to reply on the day the return falls due — the 42 days CR allows
    are emergency margin, and a reply-by *on* that last day spends all of it —
    and the minimum reply window is never silently shortened to fit. A milestone
    whose day has passed is a fact the panel shows as overdue; this is the
    separate judgement that asking is no longer worth it — staff then do it by
    hand, which is always available. ``reply_by``/``statutory_due`` are the two
    dates compared.
    """

    def __init__(self, *, reply_by: date, statutory_due: date) -> None:
        self.reply_by = reply_by
        self.statutory_due = statutory_due
        super().__init__(
            f"no client reply window is left before the statutory due date "
            f"{statutory_due.isoformat()}: the configured milestone would put the "
            f"client's reply-by at {reply_by.isoformat()}"
        )


@dataclass(frozen=True)
class Question:
    """One question in both languages (scope §2.5's bilingual rule).

    ``en``/``zh`` carry ``{value}`` exactly when the question states a value
    the client is asked to confirm; ``with_value`` fills both halves in, and
    ``bilingual`` renders the pair the way the client reads it — one language
    after the other, in the one numbered item the message prints.
    """

    en: str
    zh: str

    def with_value(self, value: str) -> "Question":
        return Question(en=self.en.format(value=value), zh=self.zh.format(value=value))

    def bilingual(self) -> str:
        return f"{self.en} {self.zh}"


@dataclass(frozen=True)
class FactRead:
    """Where a readable fact's value comes from, and how it is asked about.

    ``source`` is the ``MasterDataSnapshot`` attribute holding the record —
    never the value itself — ``render`` reads the record's own columns, and
    ``confirm_question`` is the phrasing that states the value.
    """

    source: str
    render: Callable[[Any], str | None]
    confirm_question: Question


@dataclass(frozen=True)
class FactSpec:
    """One question subject of a confirmation request.

    ``fact_id`` is the stable coordinate a parsed reply joins on;
    ``nar1_item`` names the NAR1 item the question corresponds to, for a
    reviewer reading the table against the form; ``question`` is what is
    asked when the product holds no value (and is the only question an open
    fact ever asks); ``applies`` decides whether the fact belongs in this
    company's checklist at all.
    """

    fact_id: str
    nar1_item: str
    question: Question
    read: FactRead | None
    targets: tuple[str, ...]
    applies: Callable[[Any], bool] | None = None


class MasterDataSnapshot(Protocol):
    """The resolved master data a checklist reads (structural, ADR-0006).

    ``app.application.company_master.CompanyMasterSnapshot`` satisfies it:
    one attribute per fact group, each either the record in force as of the
    read's ``as_of`` date or ``None``. The attribute types are ``Any`` on
    purpose — the rows themselves are adapter types this layer must not
    import, and the domain only ever reaches them through the ``FactRead``
    that named them. ``company`` and ``status`` are deliberately absent: the
    statics ride the message's header rather than a question, and the
    company's status is not a client question at all.

    The members are read-only properties, which is what lets a frozen
    dataclass satisfy the protocol: a snapshot is a value, and the checklist
    only ever reads it.
    """

    @property
    def name(self) -> Any: ...

    @property
    def registered_office(self) -> Any: ...

    @property
    def business_nature(self) -> Any: ...

    @property
    def contact(self) -> Any: ...

    @property
    def filing(self) -> Any: ...

    @property
    def records_kept(self) -> Any: ...

    @property
    def share_capital_classes(self) -> Any: ...


@dataclass(frozen=True)
class ChecklistItem:
    """One item of a request's checklist (contract §2.9; the Phase 0 spike's
    schema plus provenance).

    ``expected_value`` is ``None`` for a fact the product holds nothing for —
    the question then asks for the information instead of confirming it — and
    ``source_record_id`` is ``None`` exactly then: a value the product does
    not hold has no row to point at.
    """

    fact_id: str
    question: str
    expected_value: str | None
    source_record_id: UUID | None

    def as_dict(self) -> dict[str, Any]:
        """The item as the request row's ``checklist`` JSON stores it.

        A fixed key set, so what the reply parser reads later is the same
        shape that was written — with ``null`` for a value or a source the
        product does not have.
        """
        return {
            "fact_id": self.fact_id,
            "question": self.question,
            "expected_value": self.expected_value,
            "source_record_id": (
                None if self.source_record_id is None else str(self.source_record_id)
            ),
        }


@dataclass(frozen=True)
class FormText:
    """How one supported form is named in the request's copy."""

    code: str
    subject_phrase: str
    title_en: str
    title_zh: str


# The forms a confirmation request can be written for, keyed by the contract's
# own ``form_type`` vocabulary (``prior_filing.FormCode``, the same values the
# extraction slices use — one vocabulary, not two). One entry: NAR1 is the
# only confirmable form in this slice.
FORM_TEXTS: dict[FormCode, FormText] = {
    FormCode.NAR1: FormText(
        code="NAR1",
        subject_phrase="Annual Return Confirmation",
        title_en="annual return",
        title_zh="周年申報表",
    )
}


def form_text(form_type: FormCode | str) -> FormText:
    """The copy one supported form's request is written with.

    Raises ``UnsupportedFormError`` for anything else — including a form this
    product will handle later, so a new form cannot be asked about before its
    questions have been written and reviewed.
    """
    try:
        code = FormCode(form_type)
    except ValueError:
        raise UnsupportedFormError(form_type) from None
    text = FORM_TEXTS.get(code)
    if text is None:
        raise UnsupportedFormError(form_type)
    return text


def _texts(*values: object) -> list[str]:
    """The non-empty values of a compound value, stripped, in order."""
    texts = []
    for value in values:
        text = "" if value is None else str(value).strip()
        if text:
            texts.append(text)
    return texts


def _join(separator: str, *values: object) -> str | None:
    """The non-empty ones of ``values`` joined for display, or ``None``."""
    return separator.join(_texts(*values)) or None


def _name_value(record: Any) -> str | None:
    """The registered name(s) and any business name (NAR1 items 1 and 2)."""
    return _join(" / ", record.name_en, record.name_zh, record.business_name)


def _business_nature_value(record: Any) -> str | None:
    """Item 3A: the business nature code and description."""
    return _join(" — ", record.business_nature_code, record.business_nature_description)


def _registered_office_value(record: Any) -> str | None:
    """Item 6: the registered office address — the region is omitted for a
    Hong Kong address (§2.5)."""
    return _join(
        ", ", record.flat, record.building, record.street, record.district, record.country
    )


def _contact_value(record: Any) -> str | None:
    """Items 7 and 8: the company's email address and Hong Kong telephone."""
    return _join(" / ", record.email, record.contact_phone_hk)


def _mortgages_value(record: Any) -> str | None:
    """Item 9: the total amount of mortgages and charges, as money."""
    total = record.mortgages_and_charges_total
    return None if total is None else f"HK${total:,.2f}"


def _member_count_value(record: Any) -> str | None:
    """Item 10: the number of members."""
    count = record.member_count
    return None if count is None else str(count)


def _share_capital_value(record: Any) -> str | None:
    """Item 11: one summary per class of shares.

    The item's total row is derived from these classes and never stored
    (§1), so the classes are what the client is asked about.
    """
    summaries = []
    for item in record.classes or ():
        summaries.append(
            _join(
                " — ",
                item.get("class_name"),
                _join(
                    ", ",
                    f"{item.get('total_shares')} shares",
                    _join(" ", item.get("currency"), item.get("total_amount")),
                ),
            )
        )
    return _join("; ", *summaries)


def _records_kept_value(record: Any) -> str | None:
    """Item 15 / CS-E: where the company keeps its records."""
    entries = []
    for item in record.records_kept or ():
        address = item.get("address")
        location = (
            _join(
                ", ",
                address.get("flat"),
                address.get("building"),
                address.get("street"),
                address.get("district"),
                address.get("country"),
            )
            if isinstance(address, dict)
            else None
        )
        entries.append(_join(" — ", item.get("description"), location))
    return _join("; ", *entries)


def _member_count_applies(snapshot: Any) -> bool:
    """NAR1 item 10 is the companies *without* a share capital.

    The question it asks — how many members — is the one a company that has
    issued shares does not answer on its return, so the fact belongs in a
    checklist only while no share capital is in force. A company whose
    capital the product simply has not been told about is asked about it (the
    capital question below), which is how the record gets there.
    """
    return snapshot.share_capital_classes is None


# The §3 fact set, in its fixed order. Adding, removing, or re-pointing a
# fact is a deliberate change: the ids are pinned by a golden test, every
# named target is held to ``prior_filing.TARGETS``, and every named source is
# held to the master-data snapshot's own fields.
FACTS: tuple[FactSpec, ...] = (
    FactSpec(
        fact_id="company.name",
        nar1_item="1, 2",
        question=Question(
            en=(
                "Please give the company's registered name (English and/or Chinese), "
                "and any business name, as they should appear on the annual return."
            ),
            zh="請提供公司的註冊名稱（中文及／或英文）及任何商業名稱，以供填寫周年申報表。",
        ),
        read=FactRead(
            source="name",
            render=_name_value,
            confirm_question=Question(
                en='Is the company\'s registered name still "{value}"?',
                zh="公司的註冊名稱是否仍為「{value}」？",
            ),
        ),
        targets=("company.name_en", "company.name_zh", "company.business_name"),
    ),
    FactSpec(
        fact_id="company.business_nature",
        nar1_item="3A",
        question=Question(
            en=(
                "Please describe the company's business nature, with the business nature "
                "code if you have it."
            ),
            zh="請提供公司的業務性質；如知其業務性質代碼，亦請一併提供。",
        ),
        read=FactRead(
            source="business_nature",
            render=_business_nature_value,
            confirm_question=Question(
                en='Is the company\'s business nature still recorded as "{value}"?',
                zh="公司的業務性質是否仍為「{value}」？",
            ),
        ),
        targets=("company.business_nature_code", "company.business_nature_description"),
    ),
    FactSpec(
        fact_id="company.registered_office",
        nar1_item="6",
        question=Question(
            en=(
                "Please give the company's registered office address (flat, building, "
                "street and district)."
            ),
            zh="請提供公司的註冊辦事處地址（單位、大廈、街道及地區）。",
        ),
        read=FactRead(
            source="registered_office",
            render=_registered_office_value,
            confirm_question=Question(
                en='Is the company\'s registered office still at "{value}"?',
                zh="公司的註冊辦事處是否仍位於「{value}」？",
            ),
        ),
        targets=(
            "company.registered_office.flat",
            "company.registered_office.building",
            "company.registered_office.street",
            "company.registered_office.district",
        ),
    ),
    FactSpec(
        fact_id="company.contact",
        nar1_item="7, 8",
        question=Question(
            en="Please give the company's email address and Hong Kong telephone number.",
            zh="請提供公司的電郵地址及香港電話號碼。",
        ),
        read=FactRead(
            source="contact",
            render=_contact_value,
            confirm_question=Question(
                en=(
                    "Are the company's contact details (email address and Hong Kong "
                    'telephone) still "{value}"?'
                ),
                zh="公司的聯絡資料（電郵地址及香港電話）是否仍為「{value}」？",
            ),
        ),
        targets=("company.email", "company.contact_phone_hk"),
    ),
    FactSpec(
        fact_id="company.mortgages_and_charges",
        nar1_item="9",
        question=Question(
            en=(
                "Please give the total amount of the company's mortgages and charges "
                "(say so if there are none)."
            ),
            zh="請提供公司的抵押及押記總額（如無，亦請說明）。",
        ),
        read=FactRead(
            source="filing",
            render=_mortgages_value,
            confirm_question=Question(
                en="Is the total amount of the company's mortgages and charges still {value}?",
                zh="公司的抵押及押記總額是否仍為 {value}？",
            ),
        ),
        targets=("company.mortgages_and_charges_total",),
    ),
    FactSpec(
        fact_id="company.member_count",
        nar1_item="10",
        question=Question(
            en=(
                "Please give the company's number of members (this item is for a company "
                "without a share capital)."
            ),
            zh="請提供公司的成員人數（本項適用於沒有股本的公司）。",
        ),
        read=FactRead(
            source="filing",
            render=_member_count_value,
            confirm_question=Question(
                en="Is the company's number of members still {value}?",
                zh="公司的成員人數是否仍為 {value}？",
            ),
        ),
        targets=("company.member_count",),
        applies=_member_count_applies,
    ),
    FactSpec(
        fact_id="company.share_capital",
        nar1_item="11",
        question=Question(
            en=(
                "Please give the company's share capital by class of shares (class, "
                "currency, number of shares and amount)."
            ),
            zh="請提供公司的股本資料（股份類別、貨幣、股份數目及金額）。",
        ),
        read=FactRead(
            source="share_capital_classes",
            render=_share_capital_value,
            confirm_question=Question(
                en="Is the company's share capital still {value}?",
                zh="公司的股本是否仍為 {value}？",
            ),
        ),
        # No ``TARGETS`` path: the extraction vocabulary is single-column
        # registry fields, and share capital is a collection (§2.3), so where
        # an answer lands is the reply parser's slice to decide.
        targets=(),
    ),
    FactSpec(
        fact_id="company.records_kept",
        nar1_item="15 / CS-E",
        question=Question(
            en=(
                "Please say where the company's records (register of members, register "
                "of directors and so on) are kept."
            ),
            zh="請提供公司記錄（成員登記冊、董事登記冊等）的存放地點。",
        ),
        read=FactRead(
            source="records_kept",
            render=_records_kept_value,
            confirm_question=Question(
                en="Are the company's records still kept as follows: {value}?",
                zh="公司記錄是否仍按以下方式存放：{value}？",
            ),
        ),
        # The same reason as share capital: a collection, no single-column
        # extraction target to name.
        targets=(),
    ),
    FactSpec(
        fact_id="officers.directors",
        nar1_item="13",
        question=Question(
            en=(
                "Please give the company's current directors — name, date of appointment, "
                "and any change since the last annual return."
            ),
            zh="請提供公司現任董事的資料——姓名、委任日期，以及上一次周年申報以來的任何變更。",
        ),
        read=None,
        # No writable target: the answers land as change-event proposals
        # through the reply parser (M5b, §5.1.14), not as fact-table writes —
        # officers and the company secretary have been master data since F1
        # (migration 0025, 2026-09-28).
        targets=(),
    ),
    FactSpec(
        fact_id="officers.company_secretary",
        nar1_item="12",
        question=Question(
            en=(
                "Please give the company's current company secretary — name, date of "
                "appointment, and any change since the last annual return."
            ),
            zh="請提供公司現任公司秘書的資料——姓名、委任日期，以及上一次周年申報以來的任何變更。",
        ),
        read=None,
        targets=(),
    ),
)


def _open_item(spec: FactSpec) -> ChecklistItem:
    """The item for a fact the product holds no value for: the open question,
    with nothing to point at."""
    return ChecklistItem(
        fact_id=spec.fact_id,
        question=spec.question.bilingual(),
        expected_value=None,
        source_record_id=None,
    )


def _fact_item(spec: FactSpec, snapshot: MasterDataSnapshot) -> ChecklistItem:
    """One fact's item: the value in force when the product holds one, the open
    question when it does not."""
    read = spec.read
    if read is None:
        return _open_item(spec)
    record = getattr(snapshot, read.source)
    if record is None:
        return _open_item(spec)
    value = read.render(record)
    if value is None:
        return _open_item(spec)
    return ChecklistItem(
        fact_id=spec.fact_id,
        question=read.confirm_question.with_value(value).bilingual(),
        expected_value=value,
        source_record_id=record.id,
    )


def build_checklist(snapshot: MasterDataSnapshot) -> tuple[ChecklistItem, ...]:
    """The request's checklist: one item per applicable fact, in the §3 order.

    ``snapshot`` is the caller's **as-of** read of the master data (the
    application layer resolves it for the generation day), so a value here is
    the value in force that day, and a fact with no record in force is asked
    as an open question rather than left out or guessed at. A fact that does
    not apply to the company at all is left out of the checklist instead of
    being asked and left unanswered (NAR1 item 10).
    """
    return tuple(
        _fact_item(spec, snapshot)
        for spec in FACTS
        if spec.applies is None or spec.applies(snapshot)
    )


def reply_deadline(
    anniversary_date: date,
    target_date: date,
    *,
    company_type: CompanyType | str,
    milestones: Milestones,
    as_of: date,
) -> date:
    """The last day the client is asked to reply by.

    The anniversary falling in ``target_date``'s year minus the tenant's
    ``client_reply_days_before`` (``app.domain.milestones``), floored at
    ``as_of + minimum_reply_window_days`` so a late trigger still gives the
    client their minimum window. The printed date is therefore **never** CR's
    own date — that is the founder's ruling of 2026-09-24 ("不要把寬限的 42 天
    加上去…這 42 天應該是緊急情況的備用時間"): the statutory window stays
    unused margin, and no configuration of the panel can spend it.

    The statutory rule is still computed, as the **guard**: a floored date **on
    or after** CR's own deadline raises ``NoClientReplyWindowError`` rather than
    printing a reply-by that has fallen due or falls due on CR's last day. The
    boundary is inclusive deliberately — the founder's 42 days are emergency
    margin, and a client asked to reply *on* the day the return is due has been
    given none of it (the same principle, one day in from the line the equality
    drew). A company type whose NAR1 rule is not established therefore still
    refuses (``DueDateRuleNotEstablishedError``) even though the printed date no
    longer comes from the statutory arithmetic — a guard that cannot verify
    "this is strictly inside CR's window" must fail closed rather than ask a
    client to reply late. Callers store the statutory date beside the reply-by
    (the request row's ``statutory_due``), so the check leaves a durable record.
    """
    statutory_due = nar1_due_date(anniversary_date, target_date.year, company_type=company_type)
    requested = anniversary_in_year(anniversary_date, target_date.year) - timedelta(
        days=milestones.client_reply_days_before
    )
    reply_by = max(requested, as_of + timedelta(days=milestones.minimum_reply_window_days))
    if reply_by >= statutory_due:
        raise NoClientReplyWindowError(reply_by=reply_by, statutory_due=statutory_due)
    return reply_by


def _english_date(value: date) -> str:
    """``15 Aug 2026`` — the format the scope's worked example uses."""
    return f"{value.day} {_MONTHS[value.month - 1]} {value.year}"


def _chinese_date(value: date) -> str:
    """``2026年8月15日``."""
    return f"{value.year}年{value.month}月{value.day}日"


def _identifying_name(name_en: str | None, name_zh: str | None) -> str:
    """The company's name as every client-facing line shows it.

    Both registered names when both are on file, the one that is registered
    otherwise. Refusing when there is none is the point (scope §7: every
    client-facing message identifies the company by name and CR number).
    """
    name = _join(" / ", name_en, name_zh)
    if name is None:
        raise CompanyNameUnavailableError(
            "the company has no name in force, so a confirmation request cannot "
            "identify it (scope §2.5, §7)"
        )
    return name


def subject_line(
    *,
    name_en: str | None,
    name_zh: str | None,
    cr_number: str | None,
    br_number: str,
    form_type: FormCode | str,
    reply_by: date,
) -> str:
    """The subject a request goes out with (scope §2.5's worked example).

    ``[<name> — CR <number>] <form confirmation> — Reply by <date>``, with the
    BRN standing in for a CR number the master data does not hold — the same
    CR-else-BRN fallback the storage key and the inbound-mail resolver use.
    The subject names the company the way every client-facing line does
    (``_identifying_name``: both registered names for a bilingual company,
    which is deliberately *more* than the worked example's one name — the
    subject is what a reply is attributed by), and refuses a company with no
    name in force rather than writing to it unnamed.
    """
    form = form_text(form_type)
    name = _identifying_name(name_en, name_zh)
    label, number = ("CR", cr_number) if cr_number else ("BRN", br_number)
    return (
        f"[{name} — {label} {number}] {form.subject_phrase} — Reply by {_english_date(reply_by)}"
    )


def _render_body(
    *,
    form: FormText,
    company_name: str,
    number_label: tuple[str, str],
    number: str,
    reply_by: date,
    checklist: Sequence[ChecklistItem],
    tenant_name: str,
) -> str:
    """The letter itself: greeting, framing, header, questions, close, sign-off.

    English first, then the Traditional-Chinese paragraph saying the same
    thing, then the numbered questions — each item carrying both languages,
    so one number is one question whichever language the client reads — and
    the closing instruction the scope's worked example fixes: reply "NO
    CHANGES" if nothing has changed.
    """
    questions = "\n".join(
        f"{index}. {item.question}" for index, item in enumerate(checklist, start=1)
    )
    label_en, label_zh = number_label
    return (
        f"Dear Sir or Madam / 敬啟者：\n"
        f"\n"
        f"We are preparing Companies Registry Form {form.code} ({form.title_en}) for the "
        f"company below. Please reply confirming that each item below is still correct, "
        f"or tell us what has changed.\n"
        f"\n"
        f"本公司現正為下列公司擬備公司註冊處表格 {form.code}（{form.title_zh}）。"
        f"請回覆確認以下各項是否仍然正確，或告知有任何變更。\n"
        f"\n"
        f"Company / 公司：{company_name}\n"
        f"{label_en} / {label_zh}：{number}\n"
        f"Reply by / 回覆期限：{_english_date(reply_by)} / {_chinese_date(reply_by)}\n"
        f"\n"
        f"{questions}\n"
        f"\n"
        f'If everything is unchanged, simply reply "NO CHANGES". '
        f"如所有資料均無變更，請直接回覆「NO CHANGES」。\n"
        f"\n"
        f"Yours faithfully / 敬啟\n"
        f"{tenant_name}\n"
    )


@dataclass(frozen=True)
class RenderedMessage:
    """The composed client-facing message, before anything stores or sends it.

    ``body`` is the letter as the client reads it and ``message_bytes`` is
    the byte-complete RFC 5322 message — the artifact the records registry
    stores and a human sends. ``subject``/``from_address``/``to_addresses``
    are carried alongside so a caller can show or record them without parsing
    the message back out.
    """

    subject: str
    body: str
    from_address: str
    to_addresses: tuple[str, ...]
    message_bytes: bytes


def render_message(
    *,
    form_type: FormCode | str,
    name_en: str | None,
    name_zh: str | None,
    cr_number: str | None,
    br_number: str,
    reply_by: date,
    checklist: Sequence[ChecklistItem],
    from_address: str,
    to_addresses: Sequence[str],
    tenant_name: str,
    message_id: str,
    sent_at: datetime,
) -> RenderedMessage:
    """Compose the whole message, ready to be stored.

    The caller supplies everything this module must not know: the mailbox it
    goes out from (``from_address`` — scope §2.5's own-domain rule), the
    recipients (the company's registered client contacts), the deployment's
    own identities (``tenant_name``, ``message_id``) and the clock
    (``sent_at``). Nothing here reads configuration, a session, or the
    network, so the render is deterministic and testable, and two calls with
    the same inputs compose the same message.

    The message is plain text, UTF-8, and serialized with CRLF line endings
    and RFC 2047-encoded headers (``policy.SMTP``), which is what a stored
    ``.eml`` and a sending mail client expect; the registry accepts it as
    ``message/rfc822``. A name or any other header value carrying a line
    break is refused by the standard library's own header rules rather than
    folded into an injected header.
    """
    form = form_text(form_type)
    subject = subject_line(
        name_en=name_en,
        name_zh=name_zh,
        cr_number=cr_number,
        br_number=br_number,
        form_type=form_type,
        reply_by=reply_by,
    )
    body = _render_body(
        form=form,
        company_name=_identifying_name(name_en, name_zh),
        number_label=("CR Number", "公司註冊編號") if cr_number else ("BRN", "商業登記號碼"),
        number=cr_number or br_number,
        reply_by=reply_by,
        checklist=checklist,
        tenant_name=tenant_name,
    )
    message = EmailMessage(policy=SMTP)
    message["From"] = from_address
    message["To"] = ", ".join(to_addresses)
    message["Subject"] = subject
    message["Date"] = format_datetime(sent_at)
    message["Message-ID"] = message_id
    message.set_content(body)
    return RenderedMessage(
        subject=subject,
        body=body,
        from_address=from_address,
        to_addresses=tuple(to_addresses),
        message_bytes=message.as_bytes(),
    )
