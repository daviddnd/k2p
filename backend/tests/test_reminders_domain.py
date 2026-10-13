"""Domain tests for the reminder engine's pure rules (contract §5.1.12).

Pure and database-free: the clock, the cadence, the reply presence and the
join candidates all arrive as arguments, and nothing here loads, sends, or
joins a model — the sweep that selects with these rules and the sender that
delivers what they enqueue are the application layer's slices.

The client-facing wording has its own golden pin: the founder approved the
reminder copy on 2026-09-29, so the letter section states the approved
subject and body verbatim, in both languages, and fails on any drift. A
change to that copy is a re-approval, not a test update — the same
discipline ``tests/test_confirmation_domain.py`` holds the request letter to.
"""

# This file quotes the bilingual copy it checks: the approved letter stands
# as one verbatim block, and its Traditional-Chinese punctuation is the
# fullwidth CJK that ruff's RUF001 reads as an ambiguous look-alike. Both
# file-wide suppressions are deliberate — the RUF001 one mirrors the module
# that writes the copy.
# ruff: noqa: RUF001

from datetime import UTC, date, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from app.domain.reminders import (
    ReminderState,
    cadence_instant,
    match_outbound,
    match_request,
    next_due_instant,
    reminder_body,
    reminder_state,
    reminder_subject,
)

SENT_AT = datetime(2026, 7, 1, 9, 30, tzinfo=UTC)
CADENCE = (3, 7, 10)


class TestReminderState:
    def test_answered_when_reply_present_regardless_of_cadence(self) -> None:
        assert (
            reminder_state(
                sent_at=SENT_AT,
                cadence_days=CADENCE,
                has_reply=True,
                now=SENT_AT + timedelta(days=100),
            )
            is ReminderState.ANSWERED
        )

    def test_answered_even_with_empty_cadence(self) -> None:
        assert (
            reminder_state(
                sent_at=SENT_AT,
                cadence_days=(),
                has_reply=True,
                now=SENT_AT + timedelta(days=100),
            )
            is ReminderState.ANSWERED
        )

    def test_empty_cadence_is_pending_long_after_send(self) -> None:
        # "An empty cadence disables the chain": nothing is ever due and
        # nothing ever exhausts (§5.1.12).
        assert (
            reminder_state(
                sent_at=SENT_AT,
                cadence_days=(),
                has_reply=False,
                now=SENT_AT + timedelta(days=365),
            )
            is ReminderState.PENDING
        )

    def test_due_at_exactly_first_cadence_point(self) -> None:
        assert (
            reminder_state(
                sent_at=SENT_AT,
                cadence_days=CADENCE,
                has_reply=False,
                now=SENT_AT + timedelta(days=3),
            )
            is ReminderState.DUE
        )

    def test_pending_before_first_cadence_point(self) -> None:
        assert (
            reminder_state(
                sent_at=SENT_AT,
                cadence_days=CADENCE,
                has_reply=False,
                now=SENT_AT + timedelta(days=3) - timedelta(microseconds=1),
            )
            is ReminderState.PENDING
        )

    def test_due_between_cadence_points(self) -> None:
        assert (
            reminder_state(
                sent_at=SENT_AT,
                cadence_days=CADENCE,
                has_reply=False,
                now=SENT_AT + timedelta(days=5),
            )
            is ReminderState.DUE
        )

    def test_due_just_before_exhaustion(self) -> None:
        assert (
            reminder_state(
                sent_at=SENT_AT,
                cadence_days=CADENCE,
                has_reply=False,
                now=SENT_AT + timedelta(days=10) - timedelta(microseconds=1),
            )
            is ReminderState.DUE
        )

    def test_exhausted_at_exactly_final_cadence_point(self) -> None:
        # "Exhausted when now >= T + c[n-1] with no reply" — knowable at that
        # instant, so the boundary itself is exhausted, not due.
        assert (
            reminder_state(
                sent_at=SENT_AT,
                cadence_days=CADENCE,
                has_reply=False,
                now=SENT_AT + timedelta(days=10),
            )
            is ReminderState.EXHAUSTED
        )

    def test_single_point_cadence_due_then_exhausted(self) -> None:
        cadence = (3,)
        assert (
            reminder_state(
                sent_at=SENT_AT,
                cadence_days=cadence,
                has_reply=False,
                now=SENT_AT + timedelta(days=3),
            )
            is ReminderState.EXHAUSTED
        )

    def test_naive_sent_at_is_refused(self) -> None:
        with pytest.raises(ValueError, match="timezone-aware"):
            reminder_state(
                sent_at=SENT_AT.replace(tzinfo=None),
                cadence_days=CADENCE,
                has_reply=False,
                now=SENT_AT,
            )

    def test_naive_now_is_refused(self) -> None:
        with pytest.raises(ValueError, match="timezone-aware"):
            reminder_state(
                sent_at=SENT_AT,
                cadence_days=CADENCE,
                has_reply=False,
                now=SENT_AT.replace(tzinfo=None),
            )


class TestCadenceInstant:
    def test_cadence_instant_is_calendar_days_after_send(self) -> None:
        assert cadence_instant(SENT_AT, 3) == SENT_AT + timedelta(days=3)

    def test_cadence_instant_across_a_month_boundary(self) -> None:
        sent = datetime(2026, 7, 30, 9, 30, tzinfo=UTC)
        assert cadence_instant(sent, 10) == datetime(2026, 8, 9, 9, 30, tzinfo=UTC)

    def test_naive_sent_at_is_refused(self) -> None:
        with pytest.raises(ValueError, match="timezone-aware"):
            cadence_instant(SENT_AT.replace(tzinfo=None), 3)


class TestNextDueInstant:
    def test_ladder_order_follows_cadence_index(self) -> None:
        assert next_due_instant(
            sent_at=SENT_AT, cadence_days=CADENCE, reminders_sent=0
        ) == SENT_AT + timedelta(days=3)
        assert next_due_instant(
            sent_at=SENT_AT, cadence_days=CADENCE, reminders_sent=1
        ) == SENT_AT + timedelta(days=7)
        assert next_due_instant(
            sent_at=SENT_AT, cadence_days=CADENCE, reminders_sent=2
        ) == SENT_AT + timedelta(days=10)

    def test_spent_ladder_is_none(self) -> None:
        assert (
            next_due_instant(sent_at=SENT_AT, cadence_days=CADENCE, reminders_sent=3)
            is None
        )

    def test_empty_cadence_is_none(self) -> None:
        assert (
            next_due_instant(sent_at=SENT_AT, cadence_days=(), reminders_sent=0) is None
        )


REQUEST_A = UUID("11111111-1111-1111-1111-111111111111")
REQUEST_B = UUID("22222222-2222-2222-2222-222222222222")

MESSAGE_ID_TO_REQUEST = {
    "<request-a@mail.k2p.example>": REQUEST_A,
    "<request-b@mail.k2p.example>": REQUEST_B,
    "<reminder-a1@mail.k2p.example>": REQUEST_A,
}
THREAD_ID_TO_REQUEST = {
    "thread-a": REQUEST_A,
    "thread-b": REQUEST_B,
}


class TestMatchOutbound:
    """The generalized matcher (ADR-0019 §1, ruling R1).

    ``match_outbound`` is the join rule with the caller's own value type, so
    the attribution question (which company does this thread bind to?) and the
    reminders link (which request does it answer?) share one definition.
    ``match_request``'s own suite below stays green unchanged: it is now the
    UUID-valued wrapper.
    """

    def test_one_value_reached_by_two_signals_is_returned(self) -> None:
        value = (uuid4(), "company")
        assert (
            match_outbound(
                thread_id="thread-a",
                in_reply_to=("<request-a@mail.k2p.example>",),
                references=(),
                message_id_to_value={"<request-a@mail.k2p.example>": value},
                thread_id_to_value={"thread-a": value},
            )
            == value
        )

    def test_a_value_reached_by_references_alone_is_returned(self) -> None:
        value = (uuid4(), "company")
        assert (
            match_outbound(
                thread_id=None,
                in_reply_to=(),
                references=("<x@y.example>",),
                message_id_to_value={"<x@y.example>": value},
                thread_id_to_value={},
            )
            == value
        )

    def test_two_distinct_values_are_refused(self) -> None:
        # The thread names one value and a header names another: ambiguity,
        # and an ambiguous match is refused whatever the value type is.
        first, second = (uuid4(), "first"), (uuid4(), "second")
        assert (
            match_outbound(
                thread_id="thread-a",
                in_reply_to=("<request-b@mail.k2p.example>",),
                references=(),
                message_id_to_value={"<request-b@mail.k2p.example>": second},
                thread_id_to_value={"thread-a": first},
            )
            is None
        )

    def test_no_signals_at_all_is_none(self) -> None:
        assert (
            match_outbound(
                thread_id=None,
                in_reply_to=(),
                references=(),
                message_id_to_value={"<x@y.example>": (uuid4(), "x")},
                thread_id_to_value={"thread-a": (uuid4(), "a")},
            )
            is None
        )


class TestMatchRequest:
    def test_thread_match(self) -> None:
        assert (
            match_request(
                thread_id="thread-a",
                in_reply_to=(),
                references=(),
                message_id_to_request=MESSAGE_ID_TO_REQUEST,
                thread_id_to_request=THREAD_ID_TO_REQUEST,
            )
            == REQUEST_A
        )

    def test_in_reply_to_match(self) -> None:
        assert (
            match_request(
                thread_id=None,
                in_reply_to=("<request-a@mail.k2p.example>",),
                references=(),
                message_id_to_request=MESSAGE_ID_TO_REQUEST,
                thread_id_to_request=THREAD_ID_TO_REQUEST,
            )
            == REQUEST_A
        )

    def test_references_match(self) -> None:
        assert (
            match_request(
                thread_id=None,
                in_reply_to=(),
                references=(
                    "<request-a@mail.k2p.example>",
                    "<reminder-a1@mail.k2p.example>",
                ),
                message_id_to_request=MESSAGE_ID_TO_REQUEST,
                thread_id_to_request=THREAD_ID_TO_REQUEST,
            )
            == REQUEST_A
        )

    def test_agreeing_signals_on_one_request_win(self) -> None:
        # All three signals carry the same request: a thread id plus headers
        # that name two different outbound messages of the one chain.
        assert (
            match_request(
                thread_id="thread-a",
                in_reply_to=("<request-a@mail.k2p.example>",),
                references=("<reminder-a1@mail.k2p.example>",),
                message_id_to_request=MESSAGE_ID_TO_REQUEST,
                thread_id_to_request=THREAD_ID_TO_REQUEST,
            )
            == REQUEST_A
        )

    def test_ambiguity_across_two_requests_is_none(self) -> None:
        # The thread says A, a header says B — ambiguous, and an ambiguous
        # join is refused rather than guessed.
        assert (
            match_request(
                thread_id="thread-a",
                in_reply_to=("<request-b@mail.k2p.example>",),
                references=(),
                message_id_to_request=MESSAGE_ID_TO_REQUEST,
                thread_id_to_request=THREAD_ID_TO_REQUEST,
            )
            is None
        )

    def test_no_match_is_none(self) -> None:
        assert (
            match_request(
                thread_id="unknown-thread",
                in_reply_to=("<unknown@elsewhere.example>",),
                references=(),
                message_id_to_request=MESSAGE_ID_TO_REQUEST,
                thread_id_to_request=THREAD_ID_TO_REQUEST,
            )
            is None
        )

    def test_no_signals_at_all_is_none(self) -> None:
        assert (
            match_request(
                thread_id=None,
                in_reply_to=(),
                references=(),
                message_id_to_request=MESSAGE_ID_TO_REQUEST,
                thread_id_to_request=THREAD_ID_TO_REQUEST,
            )
            is None
        )

    def test_random_uuid_passes_through(self) -> None:
        request_id = uuid4()
        assert (
            match_request(
                thread_id=None,
                in_reply_to=("<x@y.example>",),
                references=(),
                message_id_to_request={"<x@y.example>": request_id},
                thread_id_to_request={},
            )
            == request_id
        )


class TestReminderSubject:
    def test_english(self) -> None:
        assert (
            reminder_subject(
                company_name="Bright Lantern Design Limited",
                anniversary_year=2026,
                language="en",
            )
            == "Reminder: confirmation request for Bright Lantern Design Limited "
            "(2026 return)"
        )

    def test_chinese(self) -> None:
        assert (
            reminder_subject(
                company_name="Bright Lantern Design Limited",
                anniversary_year=2026,
                language="zh-hk",
            )
            == "催辦：Bright Lantern Design Limited（2026年度申報表）資料確認請求"
        )

    def test_unknown_language_is_refused(self) -> None:
        with pytest.raises(ValueError, match="language"):
            reminder_subject(company_name="Acme", anniversary_year=2026, language="fr")


class TestReminderBody:
    SENT_DATE = date(2026, 7, 1)
    REPLY_BY = date(2026, 8, 15)

    def test_english(self) -> None:
        assert (
            reminder_body(
                company_name="Bright Lantern Design Limited",
                sent_date=self.SENT_DATE,
                reply_by=self.REPLY_BY,
                language="en",
            )
            == "We wrote to you on 2026-07-01 asking you to confirm the details for "
            "the above return, with a requested reply by 2026-08-15. We have not "
            "yet received your reply. Please reply to this email by 2026-08-15. "
            "If we have not heard from you by then, our staff will contact you "
            "directly."
        )

    def test_chinese(self) -> None:
        assert (
            reminder_body(
                company_name="Bright Lantern Design Limited",
                sent_date=self.SENT_DATE,
                reply_by=self.REPLY_BY,
                language="zh-hk",
            )
            == "本公司早於2026-07-01致函 貴台，就上述申報表請求確認相關資料，並懇請於"
            "2026-08-15前回覆。惟至今尚未收到 貴台回覆，敬請於2026-08-15前回覆本郵件。"
            "倘屆時仍未收到回覆，敝公司職員將直接與 貴台聯絡。"
        )

    def test_unknown_language_is_refused(self) -> None:
        with pytest.raises(ValueError, match="language"):
            reminder_body(
                company_name="Acme",
                sent_date=self.SENT_DATE,
                reply_by=self.REPLY_BY,
                language="fr",
            )
