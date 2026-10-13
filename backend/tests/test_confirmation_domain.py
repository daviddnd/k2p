"""Domain tests for the confirmation-request rules (scope §2.5, §7;
data-model §2.9, §5.1.8).

Pure and database-free: the master data arrives as the application layer's own
resolved snapshot, the clock and the mailbox arrive as arguments, and nothing
here loads, sends, or falls back to a model — the copy is a static template,
so a deployment's mailbox is never touched by this suite.

The company is the Phase 0 corpus' own — Bright Lantern Design Limited /
明燈設計有限公司 — with the identifiers a Hong Kong company actually carries.
The record objects are the real ORM rows and the snapshot is the real
``CompanyMasterSnapshot``: the fact table names a snapshot attribute and each
reader names the record's columns, and both are held to the shape the
application layer really resolves rather than to a convenient stand-in.

The anti-drift tests are the load-bearing ones: the fact table *references*
two vocabularies it must never restate — ``prior_filing.TARGETS`` and the
snapshot's own fields — so each is asserted against the real thing, and the
table itself is pinned row by row so that a rename or a re-pointing is a
deliberate, visible edit.

The client-facing wording has its own golden pin: the founder approved the
copy on 2026-09-24, so the test the message section opens with states the
approved subject and body verbatim and fails on any drift. A change to that
copy is a re-approval, not a test update.
"""

# This file quotes the bilingual copy it checks: the approved letter stands
# as one verbatim block (so its lines run as long as the letter's), and its
# Traditional-Chinese punctuation is the fullwidth CJK that ruff's RUF001
# reads as an ambiguous look-alike. Both file-wide suppressions are deliberate
# — the RUF001 one mirrors the module that writes the copy.
# ruff: noqa: E501, RUF001

import re
from dataclasses import fields
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from email import message_from_bytes
from email.policy import default as default_policy
from uuid import UUID

import pytest

from app.adapters.persistence.company_master import (
    BusinessNatureRecord,
    CompanyContactFact,
    CompanyFilingFact,
    CompanyNameRecord,
    RecordsKeptRecord,
    RegisteredOfficeRecord,
    ShareCapitalClassRecord,
)
from app.adapters.persistence.models import Company
from app.application.company_master import CompanyMasterSnapshot
from app.domain.compliance import DueDateRuleNotEstablishedError, nar1_due_date
from app.domain.confirmation import (
    FACTS,
    FORM_TEXTS,
    ChecklistItem,
    CompanyNameUnavailableError,
    NoClientReplyWindowError,
    RenderedMessage,
    RequestStatus,
    UnsupportedFormError,
    build_checklist,
    form_text,
    render_message,
    reply_deadline,
    subject_line,
)
from app.domain.documents import MESSAGE_CONTENT_TYPE, validate_upload
from app.domain.milestones import (
    ANCHOR_ANNIVERSARY,
    Milestones,
    default_milestones,
    validate_milestones,
)
from app.domain.prior_filing import TARGETS, FormCode

NAME_ID = UUID("11111111-1111-1111-1111-111111111111")
OFFICE_ID = UUID("22222222-2222-2222-2222-222222222222")
NATURE_ID = UUID("33333333-3333-3333-3333-333333333333")
CONTACT_ID = UUID("44444444-4444-4444-4444-444444444444")
FILING_ID = UUID("55555555-5555-5555-5555-555555555555")
RECORDS_ID = UUID("66666666-6666-6666-6666-666666666666")
SHARE_ID = UUID("77777777-7777-7777-7777-777777777777")

ANNIVERSARY = date(2026, 3, 1)
# The date the *copy* tests render: an input, not a derivation — the approved
# wording is pinned verbatim and the founder's ruling of 2026-09-24 (§7) leaves
# it exactly as approved, so this stays the date it was approved against.
REPLY_BY = date(2026, 4, 12)
# What the milestone panel derives for the same anniversary: the anniversary
# minus the shipped client-reply offset of 20 days (the reply-by rule's own
# tests). The two are deliberately different constants.
CLIENT_REPLY_BY = date(2026, 2, 9)
CR_NUMBER = "2345678"
BRN = "12345678"
FROM_ADDRESS = "secretary@tcsp.example.hk"
TO_ADDRESSES = ("director@brightlantern.example.hk", "chair@brightlantern.example.hk")
MESSAGE_ID = "<confirmation-1@tcsp.example.hk>"
SENT_AT = datetime(2026, 4, 1, 9, 30, tzinfo=UTC)
TENANT_NAME = "Example TCSP Limited"
OFFICE_ADDRESS = "Flat A, 9/F, Foo Building, 1 Foo Street, Central, Hong Kong"

NAME = CompanyNameRecord(
    id=NAME_ID, name_en="Bright Lantern Design Limited", name_zh="明燈設計有限公司"
)
OFFICE = RegisteredOfficeRecord(
    id=OFFICE_ID,
    flat="Flat A",
    building="9/F, Foo Building",
    street="1 Foo Street",
    district="Central",
    country="Hong Kong",
)
NATURE = BusinessNatureRecord(
    id=NATURE_ID,
    business_nature_code="47190",
    business_nature_description="Retail sale of clothing",
)
CONTACT = CompanyContactFact(
    id=CONTACT_ID, email="info@brightlantern.example.hk", contact_phone_hk="2345 6789"
)
FILING = CompanyFilingFact(
    id=FILING_ID, mortgages_and_charges_total=Decimal("1234.50"), member_count=3
)
RECORDS = RecordsKeptRecord(
    id=RECORDS_ID,
    records_kept=[
        {
            "description": "Register of members",
            "address": {
                "flat": "Flat A",
                "building": "9/F, Foo Building",
                "street": "1 Foo Street",
                "district": "Central",
                "country": "Hong Kong",
            },
        }
    ],
)
SHARE_CAPITAL = ShareCapitalClassRecord(
    id=SHARE_ID,
    classes=[
        {
            "class_name": "Ordinary",
            "currency": "HKD",
            "total_shares": 10000,
            "total_amount": "10000.00",
            "total_paid_up": "10000.00",
        }
    ],
)


def make_snapshot(**groups: object) -> CompanyMasterSnapshot:
    """One resolved master-data snapshot with the groups a test names.

    The ``company`` anchor is the real row and each group is the real record
    type; only the groups a test is about are in force, so "nothing in force"
    is the default rather than a special case.
    """
    by_group: dict[str, object] = {
        "name": None,
        "registered_office": None,
        "status": None,
        "business_nature": None,
        "contact": None,
        "filing": None,
        "records_kept": None,
        "share_capital_classes": None,
    }
    by_group.update(groups)
    return CompanyMasterSnapshot(
        company=Company(
            br_number=BRN,
            cr_number=CR_NUMBER,
            date_of_incorporation=date(2010, 3, 1),
            anniversary_date=ANNIVERSARY,
            type="private",
        ),
        **by_group,
    )


def facts_in_force() -> CompanyMasterSnapshot:
    """Every group a NAR1 request asks about, in force — no share capital,
    so the item-10 question applies too."""
    return make_snapshot(
        name=NAME,
        registered_office=OFFICE,
        business_nature=NATURE,
        contact=CONTACT,
        filing=FILING,
        records_kept=RECORDS,
    )


def item(checklist: tuple[ChecklistItem, ...], fact_id: str) -> ChecklistItem:
    matching = [entry for entry in checklist if entry.fact_id == fact_id]
    assert len(matching) == 1, f"{fact_id} is not in the checklist exactly once"
    return matching[0]


# --- The fact vocabulary ---


def test_the_fact_table_is_the_reviewed_one() -> None:
    """The whole table, pinned row by row.

    This is the deliberate edit: a renamed ``fact_id``, a re-pointed source,
    a dropped or moved target, a re-numbered item. Nothing about it is a
    formatting detail — each row is one of the subjects a reply can come back
    about, and the ids are what that reply is joined on.
    """
    assert [
        (
            spec.fact_id,
            spec.nar1_item,
            spec.read.source if spec.read is not None else None,
            spec.targets,
        )
        for spec in FACTS
    ] == [
        (
            "company.name",
            "1, 2",
            "name",
            ("company.name_en", "company.name_zh", "company.business_name"),
        ),
        (
            "company.business_nature",
            "3A",
            "business_nature",
            ("company.business_nature_code", "company.business_nature_description"),
        ),
        (
            "company.registered_office",
            "6",
            "registered_office",
            (
                "company.registered_office.flat",
                "company.registered_office.building",
                "company.registered_office.street",
                "company.registered_office.district",
            ),
        ),
        ("company.contact", "7, 8", "contact", ("company.email", "company.contact_phone_hk")),
        (
            "company.mortgages_and_charges",
            "9",
            "filing",
            ("company.mortgages_and_charges_total",),
        ),
        ("company.member_count", "10", "filing", ("company.member_count",)),
        ("company.share_capital", "11", "share_capital_classes", ()),
        ("company.records_kept", "15 / CS-E", "records_kept", ()),
        ("officers.directors", "13", None, ()),
        ("officers.company_secretary", "12", None, ()),
    ]


def test_fact_ids_are_coordinates_never_ordinals() -> None:
    """``entity.group``, lowercase — the grammar the reply parser joins on.

    A positional id (``F1`` …) would re-key a reply onto whatever fact
    happened to land in that position after a regeneration, which is exactly
    the failure a coordinate makes impossible.
    """
    pattern = re.compile(r"^[a-z_]+(\.[a-z_]+)*$")
    for spec in FACTS:
        assert pattern.match(spec.fact_id), spec.fact_id
    assert len({spec.fact_id for spec in FACTS}) == len(FACTS)


def test_every_named_target_is_a_real_extraction_target() -> None:
    """The named paths are ``prior_filing``'s own, not a parallel copy.

    If that vocabulary ever drops or renames a path, this fact table has to
    be revisited rather than silently pointing at nothing.
    """
    named = {target for spec in FACTS for target in spec.targets}
    assert sorted(named - set(TARGETS)) == []
    assert len(named) == 13


def test_a_fact_naming_no_target_is_one_of_the_four_that_have_none() -> None:
    # Non-vacuity: the empty tuples are deliberate. Share capital and records
    # kept are collections the single-column extraction vocabulary has no
    # path for, and the officer questions have no master data to write to at
    # all — so a fact that quietly *lost* its targets is not hidden here.
    assert {spec.fact_id for spec in FACTS if not spec.targets} == {
        "company.share_capital",
        "company.records_kept",
        "officers.directors",
        "officers.company_secretary",
    }


def test_every_named_source_is_a_field_of_the_master_snapshot() -> None:
    """The sources are the snapshot's own attributes, resolved at run time.

    ``build_checklist`` reaches the records with ``getattr``, so a source
    that is not a real field of the resolved snapshot is an ``AttributeError``
    on the generation path, not a question that quietly asks nothing.
    """
    assert {field.name for field in fields(CompanyMasterSnapshot)} >= {
        spec.read.source for spec in FACTS if spec.read is not None
    }
    assert {spec.read.source for spec in FACTS if spec.read is not None} == {
        "name",
        "business_nature",
        "registered_office",
        "contact",
        "filing",
        "share_capital_classes",
        "records_kept",
    }
    # The open facts: the product holds nothing for these by design.
    assert {spec.fact_id for spec in FACTS if spec.read is None} == {
        "officers.directors",
        "officers.company_secretary",
    }


def test_confirm_questions_state_the_value_and_open_questions_do_not() -> None:
    """A confirm question carries ``{value}``; an open one has no placeholder.

    The pair is what keeps the two phrasings honest: a confirm question
    without the placeholder would ask the client to confirm nothing, and an
    open question with one would render a literal ``{value}`` into the
    letter.
    """
    for spec in FACTS:
        assert "{value}" not in spec.question.en
        assert "{value}" not in spec.question.zh
        if spec.read is not None:
            assert "{value}" in spec.read.confirm_question.en
            assert "{value}" in spec.read.confirm_question.zh


def test_the_status_vocabulary_is_the_contracts() -> None:
    """The four states of the send lifecycle (§5.1.8, rewritten 2026-09-25).

    ``draft`` and ``superseded`` were the generator's whole vocabulary until
    slice M2 added the release and the delivery; the contract pins all four,
    and the transition table that governs them lives in the same module.
    """
    assert {status.value for status in RequestStatus} == {
        "draft",
        "queued",
        "sent",
        "superseded",
    }


def test_the_form_vocabulary_is_the_contracts_own() -> None:
    """One ``form_type`` vocabulary across the product: the confirmation's
    supported forms are keyed by the extraction slice's own ``FormCode``, so
    a form cannot be named one way here and another way there."""
    assert set(FORM_TEXTS) <= set(FormCode)
    assert form_text(FormCode.NAR1).code == "NAR1"
    assert form_text("nar1").subject_phrase == "Annual Return Confirmation"


# --- The checklist ---


def test_the_checklist_asks_every_applicable_fact_in_the_fixed_order() -> None:
    checklist = build_checklist(facts_in_force())

    assert [entry.fact_id for entry in checklist] == [
        "company.name",
        "company.business_nature",
        "company.registered_office",
        "company.contact",
        "company.mortgages_and_charges",
        "company.member_count",
        "company.share_capital",
        "company.records_kept",
        "officers.directors",
        "officers.company_secretary",
    ]


def test_each_fact_is_asked_with_the_value_in_force() -> None:
    checklist = build_checklist(facts_in_force())

    assert item(checklist, "company.name").expected_value == (
        "Bright Lantern Design Limited / 明燈設計有限公司"
    )
    assert item(checklist, "company.business_nature").expected_value == (
        "47190 — Retail sale of clothing"
    )
    assert item(checklist, "company.registered_office").expected_value == OFFICE_ADDRESS
    assert item(checklist, "company.contact").expected_value == (
        "info@brightlantern.example.hk / 2345 6789"
    )
    assert item(checklist, "company.mortgages_and_charges").expected_value == "HK$1,234.50"
    assert item(checklist, "company.member_count").expected_value == "3"
    assert item(checklist, "company.records_kept").expected_value == (
        f"Register of members — {OFFICE_ADDRESS}"
    )
    assert item(checklist, "company.share_capital").expected_value is None


def test_a_confirm_question_states_the_value_the_client_is_asked_about() -> None:
    """The value is *in the question*, not merely recorded beside it.

    A client can only confirm what the letter actually states, and a
    template's ``{value}`` reaching the letter would be a placeholder printed
    into client-facing copy — so both halves are asserted: the value is
    there, and no unfilled placeholder is left behind anywhere.
    """
    checklist = build_checklist(facts_in_force())

    for entry in checklist:
        assert "{value}" not in entry.question
        if entry.expected_value is not None:
            assert entry.expected_value in entry.question


def test_each_item_points_at_the_row_its_value_came_from() -> None:
    """A pointer, never a copy: the authoritative value stays in its own
    effective-dated table, and the item says which row it read."""
    checklist = build_checklist(facts_in_force())

    assert item(checklist, "company.name").source_record_id == NAME_ID
    assert item(checklist, "company.business_nature").source_record_id == NATURE_ID
    assert item(checklist, "company.registered_office").source_record_id == OFFICE_ID
    assert item(checklist, "company.contact").source_record_id == CONTACT_ID
    # Both filing facts come from the one row that was in force.
    assert item(checklist, "company.mortgages_and_charges").source_record_id == FILING_ID
    assert item(checklist, "company.member_count").source_record_id == FILING_ID
    assert item(checklist, "company.records_kept").source_record_id == RECORDS_ID


def test_a_value_and_its_source_record_stand_or_fall_together() -> None:
    """A value the product holds always points at the row it came from, and a
    question asked blind never names one — provenance cannot be half-stated."""
    for checklist in (build_checklist(facts_in_force()), build_checklist(make_snapshot())):
        for entry in checklist:
            assert (entry.expected_value is None) == (entry.source_record_id is None)


def test_the_value_asked_about_is_the_one_in_force_as_of_the_read() -> None:
    """The snapshot decides, not this module: a supersession that changed the
    registered office changes what the client is asked about."""
    checklist = build_checklist(
        make_snapshot(
            registered_office=RegisteredOfficeRecord(
                id=OFFICE_ID,
                flat="Flat B",
                building="10/F, Bar Building",
                street="2 Bar Street",
                district="Wan Chai",
                country=None,
            )
        )
    )

    assert item(checklist, "company.registered_office").expected_value == (
        "Flat B, 10/F, Bar Building, 2 Bar Street, Wan Chai"
    )


def test_a_fact_with_nothing_in_force_is_asked_as_an_open_question() -> None:
    checklist = build_checklist(make_snapshot())

    assert len(checklist) == len(FACTS)
    for entry in checklist:
        assert entry.expected_value is None
        assert entry.source_record_id is None
    # The question changed with it: nothing is confirmed that was not stated.
    assert item(checklist, "company.registered_office").question.startswith(
        "Please give the company's registered office address"
    )
    assert "「" not in item(checklist, "company.registered_office").question


def test_the_member_count_question_is_left_out_when_a_share_capital_is_in_force() -> None:
    """NAR1 item 10 is the companies without a share capital, so a company
    whose capital is on file is not asked for a member count at all."""
    checklist = build_checklist(
        make_snapshot(
            name=NAME, filing=FILING, share_capital_classes=SHARE_CAPITAL
        )
    )

    fact_ids = [entry.fact_id for entry in checklist]
    assert "company.member_count" not in fact_ids
    assert "company.share_capital" in fact_ids


def test_the_share_capital_question_states_the_classes_in_force() -> None:
    checklist = build_checklist(make_snapshot(share_capital_classes=SHARE_CAPITAL))

    assert item(checklist, "company.share_capital").expected_value == (
        "Ordinary — 10000 shares, HKD 10000.00"
    )
    assert item(checklist, "company.share_capital").source_record_id == SHARE_ID


def test_an_item_serializes_into_the_rows_checklist_shape() -> None:
    checklist = build_checklist(facts_in_force())

    assert item(checklist, "company.contact").as_dict() == {
        "fact_id": "company.contact",
        "question": item(checklist, "company.contact").question,
        "expected_value": "info@brightlantern.example.hk / 2345 6789",
        "source_record_id": str(CONTACT_ID),
    }
    assert item(checklist, "officers.directors").as_dict()["expected_value"] is None
    assert item(checklist, "officers.directors").as_dict()["source_record_id"] is None


# --- The reply-by date ---
#
# The founder's ruling (2026-09-24): the date the client is asked to reply by is
# a configured number of days *before the anniversary* — never CR's statutory
# deadline, which keeps its 42 days as the emergency margin. The statutory rule
# is still computed, as the guard (a refusal, not a printed date) and as the
# date the request row records beside it.


def test_the_reply_deadline_is_the_anniversary_minus_the_configured_offset() -> None:
    # The anniversary falling in the target date's year, minus the shipped
    # client-reply offset of 20 days — roughly seven weeks before CR's date.
    assert (
        reply_deadline(
            ANNIVERSARY,
            date(2026, 6, 1),
            company_type="private",
            milestones=default_milestones(),
            as_of=date(2025, 12, 1),
        )
        == CLIENT_REPLY_BY
    )
    # The target date picks the year; the anniversary date supplies its month
    # and day (the compliance rule's own contract).
    assert reply_deadline(
        ANNIVERSARY,
        date(2027, 1, 1),
        company_type="private",
        milestones=default_milestones(),
        as_of=date(2025, 12, 1),
    ) == date(2027, 2, 9)


def test_the_printed_reply_by_is_never_the_statutory_deadline() -> None:
    """The founder's objection, as a universal: however late the trigger runs
    and whatever the panel is configured to, the client is never asked to reply
    on CR's own last day — and when the window no longer leaves a day of margin,
    the generator refuses instead. The 42 days are emergency margin; a reply-by
    *on* the deadline spends all of it, which is why the guard is inclusive."""
    statutory = nar1_due_date(ANNIVERSARY, 2026, company_type="private")
    deadline = reply_deadline(
        ANNIVERSARY,
        date(2026, 3, 1),
        company_type="private",
        milestones=default_milestones(),
        as_of=date(2025, 12, 1),
    )

    assert deadline == date(2026, 2, 9)
    assert (statutory - deadline).days == 62

    # Every day of the window, either a printed date strictly inside it or a
    # refusal — never the statutory date itself. Swept across the boundary: the
    # very day the old rule admitted (`statutory - 7`) is now refused, and the
    # day before it still yields a date one day inside.
    printed = []
    for shift in range(-90, 10):
        as_of = statutory + timedelta(days=shift)
        try:
            printed.append(
                reply_deadline(
                    ANNIVERSARY,
                    date(2026, 3, 1),
                    company_type="private",
                    milestones=default_milestones(),
                    as_of=as_of,
                )
            )
        except NoClientReplyWindowError:
            continue
    assert printed, "the sweep must produce at least one printed date"
    assert max(printed) == statutory - timedelta(days=1)
    assert all(day < statutory for day in printed)


def test_a_late_trigger_is_floored_at_the_minimum_reply_window() -> None:
    """The configured offset has passed, so the client gets their minimum
    window from today — while CR's deadline still allows it."""
    deadline = reply_deadline(
        ANNIVERSARY,
        date(2026, 3, 1),
        company_type="private",
        milestones=default_milestones(),
        as_of=date(2026, 3, 20),
    )

    assert deadline == date(2026, 3, 27)  # as_of + 7
    assert deadline < nar1_due_date(ANNIVERSARY, 2026, company_type="private")


def test_the_floor_carries_whatever_the_tenant_configured() -> None:
    custom = validate_milestones(
        {**default_milestones().as_dict(), "minimum_reply_window_days": 21}
    )

    deadline = reply_deadline(
        ANNIVERSARY,
        date(2026, 3, 1),
        company_type="private",
        milestones=custom,
        as_of=date(2026, 3, 20),
    )

    assert deadline == date(2026, 4, 10)  # as_of + 21, still before 12 Apr


@pytest.mark.parametrize("as_of", [date(2026, 4, 7), date(2026, 4, 12), date(2026, 6, 1)])
def test_a_window_that_no_longer_fits_is_refused(as_of: date) -> None:
    """Fewer than the minimum reply window left before CR's deadline (or the
    deadline itself passed): the generator refuses rather than asking a client
    to reply late. A milestone whose day has passed reads as overdue on the
    panel — this is the separate judgement that asking is no longer useful."""
    with pytest.raises(NoClientReplyWindowError) as caught:
        reply_deadline(
            ANNIVERSARY,
            date(2026, 3, 1),
            company_type="private",
            milestones=default_milestones(),
            as_of=as_of,
        )

    # The refusal carries what it compared, for the log and the record.
    assert caught.value.statutory_due == date(2026, 4, 12)
    assert caught.value.reply_by == as_of + timedelta(days=7)


def test_the_offset_bound_does_not_promise_the_generator() -> None:
    """The validator's bound and the generator's guard are different rules, and
    each is stated as what it is.

    A configuration past the bound — which only a hand-edited row can hold,
    because ``validate_milestones`` refuses it — still produces a date on an
    early enough day, and the *shipped* configuration (well inside the bound) is
    refused by a late trigger. So the bound is the panel's coherence rule and
    never a claim that the configuration can be generated: the refusal is a
    function of the day the trigger runs."""
    past_the_bound = Milestones(
        continuation_request_days_before=90,
        data_confirmation_days_before=60,
        client_reply_days_before=45,  # the bound is 42 - 7 = 35, so this is past it
        signature_post_days_before=21,
        minimum_reply_window_days=7,
        reminder_cadence_days=(3, 7, 10),
        anchor=ANCHOR_ANNIVERSARY,
    )

    # Early enough: the nominal date is printed, and nothing is refused.
    assert (
        reply_deadline(
            ANNIVERSARY,
            date(2026, 3, 1),
            company_type="private",
            milestones=past_the_bound,
            as_of=date(2025, 11, 1),
        )
        == date(2026, 1, 15)  # the anniversary minus 45 days
    )

    # Late enough: the same configuration is refused, on the day the floor
    # reaches CR's deadline — the offset is not what decided it.
    with pytest.raises(NoClientReplyWindowError):
        reply_deadline(
            ANNIVERSARY,
            date(2026, 3, 1),
            company_type="private",
            milestones=past_the_bound,
            as_of=date(2026, 4, 5),
        )

    # And the shipped, within-bound configuration is refused on that same day.
    with pytest.raises(NoClientReplyWindowError):
        reply_deadline(
            ANNIVERSARY,
            date(2026, 3, 1),
            company_type="private",
            milestones=default_milestones(),
            as_of=date(2026, 4, 5),
        )


def test_the_statutory_last_day_is_refused_rather_than_printed() -> None:
    """The boundary that used to be admitted: a floor landing exactly on CR's
    last day is a refusal, not a client deadline.

    Seven days before the deadline with the shipped minimum window, the floor is
    ``statutory_due`` itself — a client asked to reply on the day the return is
    due has been given none of the founder's emergency margin. One day earlier
    the same call prints a date one day inside, so the refusal is the boundary
    and not an over-correction."""
    with pytest.raises(NoClientReplyWindowError) as caught:
        reply_deadline(
            ANNIVERSARY,
            date(2026, 3, 1),
            company_type="private",
            milestones=default_milestones(),
            as_of=date(2026, 4, 5),  # floor = 2026-04-12, CR's own last day
        )
    assert caught.value.reply_by == caught.value.statutory_due == date(2026, 4, 12)

    one_day_earlier = reply_deadline(
        ANNIVERSARY,
        date(2026, 3, 1),
        company_type="private",
        milestones=default_milestones(),
        as_of=date(2026, 4, 4),  # floor = 2026-04-11, one day of margin
    )
    assert one_day_earlier == date(2026, 4, 11)


@pytest.mark.parametrize("company_type", ["public", "company_limited_by_guarantee", "trust"])
def test_a_company_type_without_an_established_rule_is_refused(company_type: str) -> None:
    """Never a guessed deadline — and the rule is still needed for that reason
    alone: the printed date no longer comes from the statutory window, but the
    *guard* does, and a guard that cannot verify "this is not past CR's
    deadline" must fail closed rather than ask a client to reply late."""
    with pytest.raises(DueDateRuleNotEstablishedError):
        reply_deadline(
            ANNIVERSARY,
            date(2026, 6, 1),
            company_type=company_type,
            milestones=default_milestones(),
            as_of=date(2025, 12, 1),
        )

# --- The approved copy (founder-approved 2026-09-24) ---
#
# The founder approved this English + Traditional Chinese wording on
# 2026-09-24. It is regulated client communication, so a *change* to it needs
# the founder's or a company-secretary reviewer's approval again — and
# ``test_the_rendered_message_is_the_founder_approved_copy``, below, holds
# the rendered message to this text verbatim, so an unapproved drift fails the
# suite instead of shipping.

COPY_REAPPROVAL_REQUIRED = (
    "The rendered confirmation letter no longer matches the wording the founder "
    "approved on 2026-09-24. This is regulated client communication: a change to "
    "it needs the founder's or a company-secretary reviewer's approval again, "
    "recorded, before the test is updated to match the new rendering."
)

APPROVED_SUBJECT = (
    "[Bright Lantern Design Limited / 明燈設計有限公司 — CR 2345678] "
    "Annual Return Confirmation — Reply by 12 Apr 2026"
)

# Rendered by ``render()`` below — the §4.2 sample's inputs: every master-data
# group in force except the share capital, so the member-count question
# applies too. The literal is the copy: nothing here rewraps or normalizes it.
APPROVED_BODY = """\
Dear Sir or Madam / 敬啟者：

We are preparing Companies Registry Form NAR1 (annual return) for the company below. Please reply confirming that each item below is still correct, or tell us what has changed.

本公司現正為下列公司擬備公司註冊處表格 NAR1（周年申報表）。請回覆確認以下各項是否仍然正確，或告知有任何變更。

Company / 公司：Bright Lantern Design Limited / 明燈設計有限公司
CR Number / 公司註冊編號：2345678
Reply by / 回覆期限：12 Apr 2026 / 2026年4月12日

1. Is the company's registered name still "Bright Lantern Design Limited / 明燈設計有限公司"? 公司的註冊名稱是否仍為「Bright Lantern Design Limited / 明燈設計有限公司」？
2. Is the company's business nature still recorded as "47190 — Retail sale of clothing"? 公司的業務性質是否仍為「47190 — Retail sale of clothing」？
3. Is the company's registered office still at "Flat A, 9/F, Foo Building, 1 Foo Street, Central, Hong Kong"? 公司的註冊辦事處是否仍位於「Flat A, 9/F, Foo Building, 1 Foo Street, Central, Hong Kong」？
4. Are the company's contact details (email address and Hong Kong telephone) still "info@brightlantern.example.hk / 2345 6789"? 公司的聯絡資料（電郵地址及香港電話）是否仍為「info@brightlantern.example.hk / 2345 6789」？
5. Is the total amount of the company's mortgages and charges still HK$1,234.50? 公司的抵押及押記總額是否仍為 HK$1,234.50？
6. Is the company's number of members still 3? 公司的成員人數是否仍為 3？
7. Please give the company's share capital by class of shares (class, currency, number of shares and amount). 請提供公司的股本資料（股份類別、貨幣、股份數目及金額）。
8. Are the company's records still kept as follows: Register of members — Flat A, 9/F, Foo Building, 1 Foo Street, Central, Hong Kong? 公司記錄是否仍按以下方式存放：Register of members — Flat A, 9/F, Foo Building, 1 Foo Street, Central, Hong Kong？
9. Please give the company's current directors — name, date of appointment, and any change since the last annual return. 請提供公司現任董事的資料——姓名、委任日期，以及上一次周年申報以來的任何變更。
10. Please give the company's current company secretary — name, date of appointment, and any change since the last annual return. 請提供公司現任公司秘書的資料——姓名、委任日期，以及上一次周年申報以來的任何變更。

If everything is unchanged, simply reply "NO CHANGES". 如所有資料均無變更，請直接回覆「NO CHANGES」。

Yours faithfully / 敬啟
Example TCSP Limited
"""


# --- The subject line ---


def test_the_subject_names_the_company_and_its_cr_number() -> None:
    subject = subject_line(
        name_en=NAME.name_en,
        name_zh=NAME.name_zh,
        cr_number=CR_NUMBER,
        br_number=BRN,
        form_type="nar1",
        reply_by=REPLY_BY,
    )

    assert subject == APPROVED_SUBJECT


def test_the_subject_falls_back_to_the_brn_when_no_cr_number_is_held() -> None:
    # The same CR-else-BRN fallback the storage key and the inbound-mail
    # resolver use: the number that is on file, labelled as what it is.
    subject = subject_line(
        name_en=NAME.name_en,
        name_zh=None,
        cr_number=None,
        br_number=BRN,
        form_type="nar1",
        reply_by=REPLY_BY,
    )

    assert subject == (
        "[Bright Lantern Design Limited — BRN 12345678] "
        "Annual Return Confirmation — Reply by 12 Apr 2026"
    )


def test_the_subject_uses_the_chinese_name_when_it_is_the_only_one() -> None:
    subject = subject_line(
        name_en=None,
        name_zh="明燈設計有限公司",
        cr_number=CR_NUMBER,
        br_number=BRN,
        form_type="nar1",
        reply_by=REPLY_BY,
    )

    assert subject.startswith("[明燈設計有限公司 — CR 2345678]")


@pytest.mark.parametrize(("name_en", "name_zh"), [(None, None), ("", None), (None, "  ")])
def test_a_company_with_no_name_in_force_is_refused(
    name_en: str | None, name_zh: str | None
) -> None:
    """§7's guardrail: every client-facing message identifies the company by
    name. Writing to a client about an unnamed company is not allowed."""
    with pytest.raises(CompanyNameUnavailableError):
        subject_line(
            name_en=name_en,
            name_zh=name_zh,
            cr_number=CR_NUMBER,
            br_number=BRN,
            form_type="nar1",
            reply_by=REPLY_BY,
        )


@pytest.mark.parametrize("form_type", ["nd2a", "ND2A", "nar2", ""])
def test_a_form_this_slice_cannot_confirm_is_refused(form_type: str) -> None:
    """ND2A is the ruled refusal: every one of its registry targets is a
    ``change_event.*``, so there is no master data it could confirm."""
    with pytest.raises(UnsupportedFormError):
        subject_line(
            name_en=NAME.name_en,
            name_zh=NAME.name_zh,
            cr_number=CR_NUMBER,
            br_number=BRN,
            form_type=form_type,
            reply_by=REPLY_BY,
        )
    with pytest.raises(UnsupportedFormError):
        form_text(form_type)


# --- The message ---


def render(**overrides: object) -> RenderedMessage:
    args: dict[str, object] = {
        "form_type": "nar1",
        "name_en": NAME.name_en,
        "name_zh": NAME.name_zh,
        "cr_number": CR_NUMBER,
        "br_number": BRN,
        "reply_by": REPLY_BY,
        "checklist": build_checklist(facts_in_force()),
        "from_address": FROM_ADDRESS,
        "to_addresses": TO_ADDRESSES,
        "tenant_name": TENANT_NAME,
        "message_id": MESSAGE_ID,
        "sent_at": SENT_AT,
    }
    args.update(overrides)
    return render_message(**args)


def test_the_rendered_message_is_the_founder_approved_copy() -> None:
    """The approved copy, verbatim.

    The founder approved this wording on 2026-09-24, and ``APPROVED_SUBJECT``
    and ``APPROVED_BODY`` above are that approved text. A failure here means
    the rendered letter no longer matches it: the copy is regulated client
    communication, so the fix is the approval recorded in the failure message
    — not a new literal. The other tests in this section pin the letter's
    shape and its open-question variant, never restating this wording.
    """
    rendered = render()

    assert rendered.subject == APPROVED_SUBJECT, COPY_REAPPROVAL_REQUIRED
    assert rendered.body == APPROVED_BODY, COPY_REAPPROVAL_REQUIRED


def test_the_message_asks_every_item_bilingually_in_one_numbered_list() -> None:
    """Shape only — the wording is pinned verbatim by the test above: one
    numbered item per fact, every item carrying both languages, and a close
    signed with the TCSP's own name."""
    body = render().body

    numbered = [line for line in body.splitlines() if re.match(r"^\d+\. ", line)]
    assert len(numbered) == len(FACTS)
    assert all(
        re.search(r"[A-Za-z]", line) and re.search(r"[\u4e00-\u9fff]", line)
        for line in numbered
    )
    assert body.rstrip().endswith(TENANT_NAME)


def test_the_message_is_stored_as_a_message_the_registry_accepts() -> None:
    """The artifact is a byte-complete RFC 5322 message: it is what the
    records registry stores, what staff download, and what M2 will send."""
    rendered = render()

    # The registry's own structural check for ``message/rfc822``.
    validate_upload(content_type=MESSAGE_CONTENT_TYPE, data=rendered.message_bytes)

    message = message_from_bytes(rendered.message_bytes, policy=default_policy)
    assert message["From"] == FROM_ADDRESS
    assert message["To"] == "director@brightlantern.example.hk, chair@brightlantern.example.hk"
    assert message["Subject"] == rendered.subject
    assert message["Message-ID"] == MESSAGE_ID
    assert message["Date"] == "Wed, 01 Apr 2026 09:30:00 +0000"
    assert message.get_content_type() == "text/plain"
    assert message.get_content_charset() == "utf-8"
    assert message.get_content().replace("\r\n", "\n") == rendered.body


def test_the_same_inputs_compose_the_same_message_twice() -> None:
    """Nothing in the render reads a clock, a session, or the network: only
    the arguments decide the bytes."""
    assert render().message_bytes == render().message_bytes


def test_a_header_value_cannot_inject_a_header() -> None:
    """The company name reaches the subject, so a name with a line break is a
    header-injection attempt; the standard library's own header rules refuse
    it rather than folding it into an injected ``Bcc``."""
    with pytest.raises(ValueError):
        render(name_en="Evil Limited\nBcc: attacker@example.test")
    with pytest.raises(ValueError):
        render(to_addresses=("director@example.test\nBcc: attacker@example.test",))


def test_an_open_question_reaches_the_message_without_a_value() -> None:
    """A company the product knows nothing about still gets a usable letter —
    every question open, nothing confirmed that was never stated."""
    rendered = render(checklist=build_checklist(make_snapshot()))

    assert 'Is the company\'s registered name still' not in rendered.body
    assert "Please give the company's registered name" in rendered.body
    assert "Please give the company's current directors" in rendered.body
    assert "「None」" not in rendered.body
