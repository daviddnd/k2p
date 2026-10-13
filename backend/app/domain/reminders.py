"""Reminder domain rules (scope §2.5; contract §5.1.12, slice R).

The deterministic follow-through §2.5 promises: once a confirmation request
has left the deployment, the cadence the tenant configured
(``app.domain.milestones``' ``reminder_cadence_days``) says when a reminder
falls due, and this module is the only path that computes that — the sweep
(``app.application.reminders``) selects with ``next_due_instant``, the
dashboard and the client-actions panel read the same state through
``reminder_state``, and nothing else re-derives the arithmetic.

**The semantics (§5.1.12).** A request sent at ``T`` with cadence
``c[0..n)`` falls due at ``T + c[i]``; the chain is **answered** the moment a
reply joins the request's thread, and **exhausted** when
``now >= T + c[n-1]`` with no reply — after the final cadence point no further
reminder will ever be sent, so "items still unanswered after the final
reminder are routed to staff" is knowable at that instant, which is why the
boundary itself is ``EXHAUSTED`` and not one reminder more. An empty cadence
disables the chain: nothing is ever due and nothing ever exhausts. Every
derived value is computed at read time from the immutable rows, never stored.

**The reply join is deterministic metadata matching, not reply parsing.**
``match_outbound`` joins an inbound message to whatever stored outbound
exchange it answers from the headers and thread ids the product already
stores — provider ``thread_id`` equality, or RFC 5322
``In-Reply-To``/``References`` carrying a stored outbound
``mailbox_message_id`` (the ``Message-ID`` on the SMTP leg, the provider id
on the Gmail leg). Exactly one distinct **value** must match across all three
signals; zero or more-than-one both refuse, because a guessed join would end
the wrong chain. Two questions are asked of the one rule, with the caller's
own value type: which request the message answers (``match_request``, the
UUID-valued wrapper, value = ``confirmation_request_id`` — the §5.1.12
join), and which company the thread binds the message to (the ingestion
cycle's chain call, value = ``company_id`` — ADR-0019 §1's attribution
evidence, ruling R1). The reply parser (M5b) later enriches the same rows —
its counsel gating is untouched by this rule.

**The copy was approved by the founder on 2026-09-29.** The English and
Traditional-Chinese wording below is regulated client communication, so a
change to it needs the founder's or a company-secretary reviewer's approval
again before any deployment sends it — ``tests/test_reminders_domain.py``
pins the approved rendering verbatim, so an unapproved edit fails the suite.
Only the subject and body text live here; the MIME multipart assembly is the
application layer's (task R4).

Pure and framework-free per ADR-0006: the standard library and nothing else —
no I/O, no database, no model.
"""

# This module *is* the client-facing bilingual copy. Its Traditional-Chinese
# sentences are written with fullwidth CJK punctuation, which ruff's RUF001
# reads as an ambiguous look-alike — a rule that guards code, not a letter a
# client reads. The suppression is deliberate and file-wide; the only
# non-ASCII characters in this module are the copy itself.
# ruff: noqa: RUF001

from collections.abc import Hashable, Mapping, Sequence
from datetime import date, datetime, timedelta
from enum import StrEnum
from uuid import UUID

# The closed language vocabulary the copy is written in — the same two
# values the request letter and the stored ``Communication.language`` carry.
_LANGUAGES = ("en", "zh-hk")


class ReminderState(StrEnum):
    """Where a request's reminder chain stands (contract §5.1.12).

    ``due`` — the next cadence instant has passed and a reminder may be
    sent; ``pending`` — the chain is live but nothing is due yet (or the
    cadence is empty, which disables the chain); ``exhausted`` — the final
    cadence point passed with no reply, so no automated reminder will ever
    follow and staff take over by hand; ``answered`` — a reply has joined the
    request's thread and the chain is over.
    """

    DUE = "due"
    PENDING = "pending"
    EXHAUSTED = "exhausted"
    ANSWERED = "answered"


def _require_aware(value: datetime, field: str) -> None:
    """A timezone-aware clock reading, or a refusal.

    A naive datetime is a bug this module refuses to arithmetise over: the
    cadence points are instants, and an instant the caller has not pinned to
    a zone is not a fact the sweep may enqueue from.
    """
    if value.tzinfo is None:
        raise ValueError(f"{field} must be timezone-aware, got a naive datetime")


def reminder_state(
    *,
    sent_at: datetime,
    cadence_days: Sequence[int],
    has_reply: bool,
    now: datetime,
) -> ReminderState:
    """The chain's state at ``now``, from the immutable facts alone.

    ``sent_at`` is the moment the request's letter left the deployment (the
    request's outbound ``Communication``), ``cadence_days`` the tenant's
    configured ladder, and ``has_reply`` whether a reply has already joined
    the request's thread — the application layer supplies all three from the
    stored rows, and nothing here reads or writes anything. The precedence is
    deliberate: a reply ends the chain whatever the cadence says
    (``ANSWERED``), an empty cadence disables the chain entirely
    (``PENDING``, never due, never exhausted), and exhaustion is judged
    before due-ness so the final cadence point is the chain's end, not one
    reminder more.
    """
    _require_aware(sent_at, "sent_at")
    _require_aware(now, "now")
    if has_reply:
        return ReminderState.ANSWERED
    if not cadence_days:
        return ReminderState.PENDING
    if now >= sent_at + timedelta(days=cadence_days[-1]):
        return ReminderState.EXHAUSTED
    if now >= sent_at + timedelta(days=cadence_days[0]):
        return ReminderState.DUE
    return ReminderState.PENDING


def cadence_instant(sent_at: datetime, days: int) -> datetime:
    """The instant ``days`` calendar days after ``sent_at``.

    Calendar days, not business days — the cadence is the tenant's plain
    day-count ladder, and a point is the send instant plus the offset,
    exactly.
    """
    _require_aware(sent_at, "sent_at")
    return sent_at + timedelta(days=days)


def next_due_instant(
    *, sent_at: datetime, cadence_days: Sequence[int], reminders_sent: int
) -> datetime | None:
    """The chain's next cadence instant, or ``None`` when the ladder is spent.

    The cadence instant for index ``reminders_sent`` — the application layer
    counts the request's immutable ``reminder`` rows to learn where the
    ladder stands (§5.1.12: which point a reminder answers is derived by
    counting, nothing stores an index). ``None`` when
    ``reminders_sent >= len(cadence_days)`` or the cadence is empty, which is
    what the sweep selects with: due requests are those whose next cadence
    instant is ``≤ now``.
    """
    _require_aware(sent_at, "sent_at")
    if reminders_sent >= len(cadence_days):
        return None
    return cadence_instant(sent_at, cadence_days[reminders_sent])


def match_outbound[V: Hashable](
    *,
    thread_id: str | None,
    in_reply_to: Sequence[str],
    references: Sequence[str],
    message_id_to_value: Mapping[str, V],
    thread_id_to_value: Mapping[str, V],
) -> V | None:
    """The stored outbound value an inbound message's thread evidence names.

    Deterministic metadata matching over headers the product already stores —
    **not** reply parsing: the parser (M5b) later enriches the same rows, and
    its counsel gating is untouched by this rule. The application layer
    supplies the candidate mappings it resolved from the stored outbound
    rows: ``message_id_to_value`` keyed by a stored outbound
    ``mailbox_message_id`` (the RFC 5322 ``Message-ID`` on the SMTP leg, the
    provider id on the Gmail leg), and ``thread_id_to_value`` keyed by the
    provider thread id where the provider uses one. The value is the caller's
    own question — a request id for the §5.1.12 join, a company id for
    ADR-0019 §1's chain call.

    A value matches when **any** of the three signals carries it: thread
    equality, ``In-Reply-To``, or ``References``. The return is the value
    only when **exactly one distinct** value matches across all three
    signals; two different values matching (the thread says A, a header says
    B) is ambiguity, and ambiguity — like no match at all — returns ``None``,
    because a guessed match would end the wrong chain or bind the wrong
    company.
    """
    matched: set[V] = set()
    if thread_id is not None:
        value = thread_id_to_value.get(thread_id)
        if value is not None:
            matched.add(value)
    for header in (*in_reply_to, *references):
        value = message_id_to_value.get(header)
        if value is not None:
            matched.add(value)
    if len(matched) == 1:
        return next(iter(matched))
    return None


def match_request(
    *,
    thread_id: str | None,
    in_reply_to: Sequence[str],
    references: Sequence[str],
    message_id_to_request: Mapping[str, UUID],
    thread_id_to_request: Mapping[str, UUID],
) -> UUID | None:
    """The request an inbound message answers, or ``None`` when not decidable.

    The reminder chain's own question of ``match_outbound`` (§5.1.12): the
    value is the ``confirmation_request_id`` the caller's maps carry. The
    rule — the three signals, and exactly one distinct request or nothing —
    is that function's, and this wrapper exists so the chain speaks its own
    vocabulary.
    """
    return match_outbound(
        thread_id=thread_id,
        in_reply_to=in_reply_to,
        references=references,
        message_id_to_value=message_id_to_request,
        thread_id_to_value=thread_id_to_request,
    )


def _require_language(language: str) -> None:
    """One of the two written languages, or a refusal.

    The vocabulary is closed — the stored ``Communication.language`` carries
    exactly these values — so an unknown one is a caller bug, not a tenant
    choice, and refusing it beats rendering a letter in a language that was
    never approved.
    """
    if language not in _LANGUAGES:
        raise ValueError(
            f"language must be one of {', '.join(_LANGUAGES)}, got {language!r}"
        )


def reminder_subject(*, company_name: str, anniversary_year: int, language: str) -> str:
    """The subject a reminder goes out with (the §5.1.12 letter, approved
    2026-09-29).

    Dates format as ISO ``YYYY-MM-DD``; the MIME multipart assembly around
    this text is the application layer's.
    """
    _require_language(language)
    if language == "en":
        return (
            f"Reminder: confirmation request for {company_name} "
            f"({anniversary_year} return)"
        )
    return f"催辦：{company_name}（{anniversary_year}年度申報表）資料確認請求"


def reminder_body(
    *, company_name: str, sent_date: date, reply_by: date, language: str
) -> str:
    """The reminder letter itself (approved 2026-09-29), one language per call.

    ``sent_date`` restates when the original request went out and ``reply_by``
    the requested reply-by the request named — both printed ISO. Which
    language a given deployment's thread uses is the application layer's
    choice from the stored request's ``language``; this module owns the text
    of each.
    """
    _require_language(language)
    sent = sent_date.isoformat()
    by = reply_by.isoformat()
    if language == "en":
        return (
            f"We wrote to you on {sent} asking you to confirm the details for "
            f"the above return, with a requested reply by {by}. We have not yet "
            f"received your reply. Please reply to this email by {by}. If we "
            f"have not heard from you by then, our staff will contact you directly."
        )
    return (
        f"本公司早於{sent}致函 貴台，就上述申報表請求確認相關資料，並懇請於{by}前回覆。"
        f"惟至今尚未收到 貴台回覆，敬請於{by}前回覆本郵件。倘屆時仍未收到回覆，"
        f"敝公司職員將直接與 貴台聯絡。"
    )


__all__ = [
    "ReminderState",
    "cadence_instant",
    "match_outbound",
    "match_request",
    "next_due_instant",
    "reminder_body",
    "reminder_state",
    "reminder_subject",
]
