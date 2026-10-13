"""The reply-parse grammar as data, the deterministic post-checks, the field
scoring and routing, and the proposal mapping onto the change-event grammar
(scope §2.5, §7; data-model §2.7; the M5b slice; contract §5.1.13).

A client reply arrives as free text; the model proposes a structured parse
against the closed ``reply-parse.schema.json`` contract, and this module is the
deterministic half of what happens next — the part counsel's Q1.2/Q1.3 posture
requires to be code, never model discretion:

**The grammar, transcribed.** ``parse_violations`` is the closed schema as
data: the six ``status`` values, the ``language`` vocabulary, the
``confirmations[]`` / ``change_events[]`` / ``multi_company_signals`` shapes
with their exact key sets, the boolean strictness (``uncertain: 1`` is not a
boolean), and the ISO *shape* of a date. The event-type enum is the
*enterable* ``ChangeEventType``s only — ``company_name_change`` is reserved for
Wave 2/NNC2 (``app.domain.change_events.RESERVED_TYPES``) and a parse that
proposes it is refused, not trusted. Anything outside the grammar fails
closed: the application layer routes it to staff review and proposes nothing.

**The post-checks.** Three, all deterministic: every ``evidence_span`` must be
a verbatim substring of the reply — the spike's anti-fabrication check as
code, with one documented normalization, the ``INFERRED:`` prefix (a span
that is not itself verbatim is acceptable only as ``INFERRED:`` plus a
remainder that *is* verbatim; anything else fails); every
``confirmations[].fact_id`` must exist in the request's checklist (the parser
consumes the checklist, never writes it); and every date that carries the ISO
shape must also be a real calendar day — ``2026-13-45`` matches the pattern
and fools nothing here.

**Scoring and routing.** A field starts at 1.0 and is downgraded by fixed,
named constants: the model's own ``uncertain`` flag, and each failed
post-check. ``overall_score`` is the minimum over fields — a reply is only as
strong as its weakest parsed field — and the band mapping is
``app.domain.confidence``'s one rule, reused, never restated. Two rules then
bend the result for events, and both only ever *downward*. **The no-guessing
rule:** a change-event proposal that cannot satisfy the ``change_event.*``
payload grammar without guessing (a cessation the client gave no reason for,
a null effective date, a key outside the closed grammar, the §1 protected
class) is *never* auto-accepted — it routes to ``client_clarification``
regardless of score, so the client is asked rather than a high-confidence
mis-extraction entering the record (scope §7; DoD item 22). **Ruling R-D:**
a proposal that names a natural person the company's appointments do not
carry (``unregistered_person_names``) caps an ``auto_accept`` band at
``staff_review`` — the client's own register is the evidence for who belongs
in the record, so a name it has never heard of is a fact only a person may
accept. Every channel the landed payload can carry a name in is read (the
party and ``new_values``) and the comparison is exact after
``normalize_for_matching`` (ruling R-T4a): the tokenizer's one-edit radius is
not shared, because this gate denies by default and a false match here would
remove a glance rather than cost one. A split ``surname_en``/``other_names_en``
pair is one person, not two fragments (ruling R-T4b): it is joined by
``joined_english_name`` — the register's own join — and only the joined form is
compared, so a correctly split appointment of a registered person still
auto-accepts. The cap is a floor, not a target: a band the score or the
no-guessing rule already put below ``auto_accept`` is left where it is.

**The proposal mapping.** ``payload_proposal`` is the mechanical bridge from a
parsed change event onto the typed-payload grammar: the role the event type
names (``TYPE_ROLES`` carries both director roles for a director event, but a
free-text reply cannot distinguish a reserve directorship — the plain role is
proposed and staff adjust), the party's split names, the effective date as the
appointment/cessation date, and the ``new_values`` entries the shape
recognizes (anything it does not is refused by the grammar itself, by name).
Completeness is judged by ``app.domain.change_events.payload_violations`` —
the payload grammar's one owner — so the proposal and a hand-entered event
cannot diverge.

**Withdrawal and correction matching.** A ``withdrawal`` or ``correction``
parse refers to an earlier event; the match is the earlier event of the same
type naming the same party, in a status a client withdrawal can still land on
— every §4.1 row carrying a ``client_withdraw`` action, which is what "any
non-``submitted``" means precisely (``submitted`` cannot be withdrawn once
filed, and ``withdrawn``/``deleted`` are terminals). Zero matches, or more
than one, refuse: a guessed link would withdraw or correct the wrong event —
the ``match_request`` precedent (``app.domain.reminders``).

Nothing here contacts a model, reads a session, or writes a row; the
application pipeline (``app.application.reply_parsing``) drives this. Pure and
framework-free per ADR-0006: the standard library and ``app.domain`` only.
"""

import re
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from typing import Any
from uuid import UUID

from app.domain.change_events import (
    ADDRESS_REQUIRED_KEYS,
    CHANGE_EVENT_TRANSITIONS,
    NAME_HISTORY_KEYS,
    RESERVED_TYPES,
    ChangeEventAction,
    ChangeEventStatus,
    ChangeEventType,
    OfficerRole,
    payload_violations,
)
from app.domain.confidence import ConfidenceBand, routing_for

# Ruling R-D compares a proposed person against the company's register, and the
# comparison form is ``redaction.py``'s one definition of "the same spelling"
# (width, case, whitespace and the bounded traditional↔simplified folds), reused
# so both consumers fold a name identically. Only the form is shared: ruling
# R-T4a makes this gate exact, and deliberately leaves the tokenizer's one-edit
# radius behind — see ``_name_off_register``.
from app.domain.redaction import normalize_for_matching


class ReplyStatus(StrEnum):
    """What the reply says, as the schema's six values order it.

    ``confirmed_no_change`` / ``changes_reported`` are the straight cases;
    ``correction`` and ``withdrawal`` refer to an event the client already
    proposed (``match_earlier_event`` finds it); ``unclear`` and ``off_topic``
    are the model naming its own failure — a deterministic post-check's verdict
    may still override either.
    """

    CONFIRMED_NO_CHANGE = "confirmed_no_change"
    CHANGES_REPORTED = "changes_reported"
    CORRECTION = "correction"
    WITHDRAWAL = "withdrawal"
    UNCLEAR = "unclear"
    OFF_TOPIC = "off_topic"


class ReplyLanguage(StrEnum):
    """The detected reply language (``communications.language``'s vocabulary)."""

    EN = "en"
    ZH_HK = "zh-hk"
    MIXED = "mixed"


class ConfirmationAnswer(StrEnum):
    """The answer vocabulary one confirmation carries."""

    CONFIRMED = "confirmed"
    DENIED = "denied"
    NOT_ADDRESSED = "not_addressed"


class MentionKind(StrEnum):
    """How a company mention in the reply was recognized (the schema's kinds)."""

    NAME_EN = "name_en"
    NAME_ZH = "name_zh"
    BRN = "brn"
    OTHER = "other"


# The event types a parse may propose: the registry's enum minus the reserved
# Wave 2 type — this module's half of the reservation rule (the write path
# owns the other half), so a model cannot sneak a reserved type past the
# grammar that ``parse_violations`` enforces.
ENTERABLE_EVENT_TYPES: frozenset[ChangeEventType] = frozenset(ChangeEventType) - RESERVED_TYPES

# The documented normalization a non-verbatim span needs: the ``INFERRED:``
# prefix, with the verbatim remainder after it (one separating space allowed).
INFERRED_PREFIX = "INFERRED:"

# The bare vocabulary a reply-parse answer may carry without referencing a
# snippet token: the union of the grammar's closed enums and nothing wider.
# ``app.application.ai_gateway.infer`` passes it as the strict restore's
# ``allowed_literals``, so a bare enum word is admissible while every other
# value the answer returns must be a token from the call's own table — a value
# literal the model never saw (``A987654(3)``) is refused as
# ``invented_value`` rather than stored. Free evidence prose and derived ISO
# dates are not literals and pass to this grammar's own checks.
PARSE_LITERALS: frozenset[str] = frozenset(
    {member.value for member in ReplyStatus}
    | {member.value for member in ReplyLanguage}
    | {member.value for member in ConfirmationAnswer}
    | {member.value for member in MentionKind}
    | {member.value for member in ENTERABLE_EVENT_TYPES}
)

# The scoring table: a field starts at 1.0 and each downgrade is a fixed,
# named constant. ``uncertain`` is the model doubting itself; a failed span
# check is fabrication risk (the strongest signal — a quote that is not in the
# reply must never be shown back to the client as the client's words); an
# unknown ``fact_id`` cannot join the checklist at all; a calendar-invalid
# date cannot anchor an effective-dated fact.
UNCERTAIN_PENALTY = 0.3
SPAN_VIOLATION_PENALTY = 0.4
UNKNOWN_FACT_ID_PENALTY = 0.5
INVALID_DATE_PENALTY = 0.3

# The grammar as data: exact key sets, the schema's required lists. A closed
# object is the contract's own shape — an unknown key is a typo or an
# invention, and either is refused by name rather than ignored.
_REPLY_KEYS = frozenset(
    {"status", "language", "confirmations", "change_events", "multi_company_signals"}
)
_CONFIRMATION_KEYS = frozenset({"fact_id", "answer", "evidence_span", "uncertain"})
_CHANGE_EVENT_KEYS = frozenset(
    {
        "type",
        "party",
        "effective_date",
        "effective_date_text",
        "evidence_span",
        "uncertain",
        "informed_date",
        "new_values",
        "notes",
    }
)
_CHANGE_EVENT_REQUIRED = frozenset(
    {"type", "party", "effective_date", "effective_date_text", "evidence_span", "uncertain"}
)
_PARTY_KEYS = frozenset(
    {"name_as_written", "name_en", "name_zh", "partial_id", "evidence_span", "uncertain"}
)
_PARTY_REQUIRED = frozenset({"name_as_written", "evidence_span", "uncertain"})
_SIGNAL_KEYS = frozenset({"company_mentions"})
_MENTION_KEYS = frozenset({"text", "kind"})

_ISO_DATE_SHAPE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")

# The proposal role a free-text reply can support: a secretaryship names a
# company secretary, a directorship a director. ``TYPE_ROLES`` (the payload
# grammar's own mapping) gives a director event both director roles, and a
# reserve directorship is not distinguishable from a plain one in prose — the
# plain role is proposed and staff adjust (stated here because the mapping is
# this module's decision; the grammar accepts either role of the type).
_PROPOSED_ROLE: Mapping[ChangeEventType, OfficerRole] = {
    ChangeEventType.DIRECTOR_APPOINTMENT: OfficerRole.DIRECTOR,
    ChangeEventType.DIRECTOR_CESSATION: OfficerRole.DIRECTOR,
    ChangeEventType.SECRETARY_APPOINTMENT: OfficerRole.COMPANY_SECRETARY,
    ChangeEventType.SECRETARY_CESSATION: OfficerRole.COMPANY_SECRETARY,
}

_APPOINTMENT_TYPES = frozenset(
    {ChangeEventType.DIRECTOR_APPOINTMENT, ChangeEventType.SECRETARY_APPOINTMENT}
)

# Ruling R-D's scope: the proposal types whose payload carries an officer's
# identity — the four ``_PROPOSED_ROLE`` maps, and nothing else. A
# registered-office move names an address and an ``other`` event a summary
# line, so neither introduces a person; the set is derived from the role
# mapping rather than restated, so a new officer type cannot join the grammar
# without joining the register rule. A withdrawal or correction carries one of
# these types too, and is judged the same way: this rule reads the type, and a
# reference that names a stranger is no more fit to auto-accept than a
# stranger's appointment.
_PERSON_EVENT_TYPES: frozenset[ChangeEventType] = frozenset(_PROPOSED_ROLE)

# The keys a landed officer payload can carry a natural person's *name* in, and
# the two channels that reach them. ``payload_proposal`` copies a proposal's
# ``new_values`` straight into the officer block (``officer.update(new_values)``)
# and then fills ``name_en``/``name_zh`` from the party only when the officer
# key is still empty, so both channels land in the record and both must be
# judged: the party's three forms, and — from ``new_values`` — the officer
# grammar's identity keys, the alternate-director text box, and the
# ``{name_zh, name_en}`` entries of the name-history lists. Identifiers are not
# names and are not read: ``partial_id``, ``br_number``, ``hkid_partial``,
# ``passport_partial``.
#
# The split ``surname_en``/``other_names_en`` pair is the one channel that is
# not read key by key: it is one person written in two boxes, so it goes
# through ``joined_english_name`` (ruling R-T4b) instead.
_PARTY_NAME_KEYS: tuple[str, ...] = ("name_as_written", "name_en", "name_zh")
_OFFICER_NAME_KEYS: tuple[str, ...] = ("name_en", "name_zh", "alternate_to")
_OFFICER_NAME_HISTORY_KEYS: tuple[str, ...] = ("previous_names", "aliases")
_SPLIT_NAME_KEYS: tuple[str, ...] = ("surname_en", "other_names_en")

# The statuses a withdrawal or correction can still land on: every §4.1 row
# carrying a ``client_withdraw`` action. That is the precise content of the
# brief's "non-submitted" — a submitted event cannot be withdrawn once filed,
# and ``withdrawn``/``deleted`` are terminals a second withdrawal is not a
# state the product has.
_WITHDRAWABLE_STATUSES: frozenset[ChangeEventStatus] = frozenset(
    status
    for status, actions in CHANGE_EVENT_TRANSITIONS.items()
    if ChangeEventAction.CLIENT_WITHDRAW in actions
)


def _object_violations(
    value: object,
    *,
    path: str,
    keys: frozenset[str],
    required: frozenset[str] = frozenset(),
) -> list[str]:
    """A closed object: a mapping, only the named keys, all the required ones."""
    if not isinstance(value, Mapping):
        return [f"{path}: not an object"]
    violations = [f"{path}.{key}: unknown key" for key in sorted(set(value) - keys)]
    violations.extend(f"{path}.{key}: required" for key in sorted(required - set(value)))
    return violations


def _text_violations(value: object, *, path: str) -> list[str]:
    """A string, as the schema's unconstrained ``type: "string"`` reads."""
    if not isinstance(value, str):
        return [f"{path}: not text"]
    return []


def _bool_violations(value: object, *, path: str) -> list[str]:
    """A JSON boolean — ``isinstance(value, bool)`` first, because an integer
    is not a checkbox and ``True == 1``."""
    if not isinstance(value, bool):
        return [f"{path}: not a boolean"]
    return []


def _enum_violations(value: object, *, path: str, allowed: Collection[str]) -> list[str]:
    if not isinstance(value, str) or value not in allowed:
        return [f"{path}: not one of {', '.join(sorted(allowed))}"]
    return []


def _date_shape_violations(value: object, *, path: str) -> list[str]:
    """The ISO *shape* (``YYYY-MM-DD``) — calendar validity is the post-check's."""
    if value is None:
        return []
    if not isinstance(value, str) or not _ISO_DATE_SHAPE.match(value):
        return [f"{path}: not an ISO date (YYYY-MM-DD)"]
    return []


def _is_iso_date(value: object) -> bool:
    """A real calendar day, which is what a date the grammar's pattern accepted
    must still prove."""
    if not isinstance(value, str):
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def _confirmation_violations(item: object, *, path: str) -> list[str]:
    violations = _object_violations(
        item, path=path, keys=_CONFIRMATION_KEYS, required=_CONFIRMATION_KEYS
    )
    if not isinstance(item, Mapping):
        return violations
    violations.extend(_text_violations(item.get("fact_id"), path=f"{path}.fact_id"))
    violations.extend(
        _enum_violations(
            item.get("answer"),
            path=f"{path}.answer",
            allowed=tuple(member.value for member in ConfirmationAnswer),
        )
    )
    violations.extend(_text_violations(item.get("evidence_span"), path=f"{path}.evidence_span"))
    violations.extend(_bool_violations(item.get("uncertain"), path=f"{path}.uncertain"))
    return violations


def _party_violations(value: object, *, path: str) -> list[str]:
    violations = _object_violations(value, path=path, keys=_PARTY_KEYS, required=_PARTY_REQUIRED)
    if not isinstance(value, Mapping):
        return violations
    for key in ("name_as_written", "evidence_span"):
        violations.extend(_text_violations(value.get(key), path=f"{path}.{key}"))
    # The name forms and the partial identifier are nullable in the schema —
    # a reply that names a party in one language only is the common case.
    for key in ("name_en", "name_zh", "partial_id"):
        if value.get(key) is not None:
            violations.extend(_text_violations(value[key], path=f"{path}.{key}"))
    violations.extend(_bool_violations(value.get("uncertain"), path=f"{path}.uncertain"))
    return violations


def _change_event_violations(item: object, *, path: str) -> list[str]:
    violations = _object_violations(
        item, path=path, keys=_CHANGE_EVENT_KEYS, required=_CHANGE_EVENT_REQUIRED
    )
    if not isinstance(item, Mapping):
        return violations
    violations.extend(
        _enum_violations(
            item.get("type"),
            path=f"{path}.type",
            allowed=tuple(sorted(member.value for member in ENTERABLE_EVENT_TYPES)),
        )
    )
    violations.extend(_party_violations(item.get("party"), path=f"{path}.party"))
    violations.extend(
        _date_shape_violations(item.get("effective_date"), path=f"{path}.effective_date")
    )
    violations.extend(
        _text_violations(item.get("effective_date_text"), path=f"{path}.effective_date_text")
    )
    informed = item.get("informed_date")
    if informed is not None:
        violations.extend(_date_shape_violations(informed, path=f"{path}.informed_date"))
    new_values = item.get("new_values")
    if new_values is not None:
        if not isinstance(new_values, Mapping):
            violations.append(f"{path}.new_values: not an object")
        else:
            for key in sorted(new_values):
                value = new_values[key]
                if value is not None and not isinstance(value, str):
                    violations.append(f"{path}.new_values.{key}: must be a string or null")
    violations.extend(_text_violations(item.get("evidence_span"), path=f"{path}.evidence_span"))
    violations.extend(_bool_violations(item.get("uncertain"), path=f"{path}.uncertain"))
    notes = item.get("notes")
    if notes is not None:
        violations.extend(_text_violations(notes, path=f"{path}.notes"))
    return violations


def parse_violations(raw: object) -> list[str]:
    """Every place ``raw`` leaves the closed reply-parse grammar; empty accepts.

    The application layer's fail-closed rule: a non-empty answer means the
    model's output is never trusted — no events are proposed, no letter is
    composed, and the reply waits for staff.
    """
    if not isinstance(raw, Mapping):
        return ["reply-parse: not an object"]
    violations = _object_violations(raw, path="reply-parse", keys=_REPLY_KEYS, required=_REPLY_KEYS)
    violations.extend(
        _enum_violations(
            raw.get("status"),
            path="reply-parse.status",
            allowed=tuple(member.value for member in ReplyStatus),
        )
    )
    violations.extend(
        _enum_violations(
            raw.get("language"),
            path="reply-parse.language",
            allowed=tuple(member.value for member in ReplyLanguage),
        )
    )
    confirmations = raw.get("confirmations")
    if not isinstance(confirmations, list):
        violations.append("reply-parse.confirmations: not a list")
    else:
        for index, item in enumerate(confirmations):
            violations.extend(
                _confirmation_violations(item, path=f"reply-parse.confirmations[{index}]")
            )
    change_events = raw.get("change_events")
    if not isinstance(change_events, list):
        violations.append("reply-parse.change_events: not a list")
    else:
        for index, item in enumerate(change_events):
            violations.extend(
                _change_event_violations(item, path=f"reply-parse.change_events[{index}]")
            )
    signals = raw.get("multi_company_signals")
    if not isinstance(signals, Mapping):
        violations.append("reply-parse.multi_company_signals: not an object")
    else:
        signal_violations = _object_violations(
            signals,
            path="reply-parse.multi_company_signals",
            keys=_SIGNAL_KEYS,
            required=_SIGNAL_KEYS,
        )
        violations.extend(signal_violations)
        mentions = signals.get("company_mentions")
        if not isinstance(mentions, list):
            violations.append("reply-parse.multi_company_signals.company_mentions: not a list")
        else:
            for index, mention in enumerate(mentions):
                path = f"reply-parse.multi_company_signals.company_mentions[{index}]"
                violations.extend(
                    _object_violations(
                        mention, path=path, keys=_MENTION_KEYS, required=_MENTION_KEYS
                    )
                )
                if isinstance(mention, Mapping):
                    violations.extend(_text_violations(mention.get("text"), path=f"{path}.text"))
                    violations.extend(
                        _enum_violations(
                            mention.get("kind"),
                            path=f"{path}.kind",
                            allowed=tuple(member.value for member in MentionKind),
                        )
                    )
    return violations


def span_is_verbatim(span: str, reply_text: str) -> bool:
    """Whether ``span`` proves itself against the reply's own characters.

    A span is acceptable when it is a verbatim substring of the reply. The one
    documented normalization is the ``INFERRED:`` prefix: a span that is not
    itself verbatim passes only as the prefix plus a remainder that *is*
    verbatim (one separating space allowed) — the model flagging that it
    composed the span rather than quoting it. Everything else fails, an empty
    span most of all: it proves nothing. The check is case-sensitive; "verbatim"
    means the reply's own characters, not a case-shifted copy of them.
    """
    if not span:
        return False
    if span in reply_text:
        return True
    if span.startswith(INFERRED_PREFIX):
        remainder = span[len(INFERRED_PREFIX) :].lstrip(" ")
        return bool(remainder) and remainder in reply_text
    return False


def field_scores(
    *,
    parse: Mapping[str, Any],
    reply_text: str,
    checklist_fact_ids: Collection[str],
) -> dict[str, float]:
    """The per-field scores a parsed reply earns, keyed for the §2.7
    ``field_scores`` map (``confirmations.<fact_id>``, ``change_events.<n>``).

    A field starts at 1.0; the model's ``uncertain`` flag and each failed
    post-check downgrade it by the named constants, floored at 0.0. An event
    field counts its own flags and spans and its party's — the party is half
    of what the event claims — and a date that carries the ISO shape but not a
    real calendar day. Precondition: ``parse`` passed ``parse_violations``;
    scoring a grammar-invalid object is a caller bug.
    """
    scores: dict[str, float] = {}
    for confirmation in parse["confirmations"]:
        score = 1.0
        if confirmation["uncertain"]:
            score -= UNCERTAIN_PENALTY
        if not span_is_verbatim(confirmation["evidence_span"], reply_text):
            score -= SPAN_VIOLATION_PENALTY
        if confirmation["fact_id"] not in checklist_fact_ids:
            score -= UNKNOWN_FACT_ID_PENALTY
        scores[f"confirmations.{confirmation['fact_id']}"] = max(score, 0.0)
    for index, event in enumerate(parse["change_events"]):
        score = 1.0
        if event["uncertain"]:
            score -= UNCERTAIN_PENALTY
        if not span_is_verbatim(event["evidence_span"], reply_text):
            score -= SPAN_VIOLATION_PENALTY
        party = event["party"]
        if party["uncertain"]:
            score -= UNCERTAIN_PENALTY
        if not span_is_verbatim(party["evidence_span"], reply_text):
            score -= SPAN_VIOLATION_PENALTY
        for key in ("effective_date", "informed_date"):
            value = event.get(key)
            if value is not None and not _is_iso_date(value):
                score -= INVALID_DATE_PENALTY
        scores[f"change_events.{index}"] = max(score, 0.0)
    return scores


def payload_proposal(*, event: Mapping[str, Any]) -> Mapping[str, Any]:
    """The ``change_event.*`` payload a parsed event proposes, best-effort.

    The mechanical bridge the review queue completes: the role the type names
    (see ``_PROPOSED_ROLE``), the party's split names, the effective date as
    the appointment/cessation date, and every ``new_values`` entry — the
    payload grammar itself refuses what the shape does not recognize, by name,
    so nothing invented travels silently. ``name_as_written`` and
    ``partial_id`` are deliberately not mapped: the payload's identity is the
    split name pair, and an unidentified partial is a field staff complete,
    not a guess this mapping makes. Precondition: ``event["type"]`` is an
    enterable ``ChangeEventType`` (the grammar guarantees it).
    """
    event_type = ChangeEventType(event["type"])
    new_values = event.get("new_values") or {}
    if event_type is ChangeEventType.OTHER:
        return {"summary": new_values.get("summary")}
    if event_type is ChangeEventType.REGISTERED_OFFICE_CHANGE:
        address = {
            key: new_values[key]
            for key in (*ADDRESS_REQUIRED_KEYS, "country")
            if new_values.get(key) is not None
        }
        return {"registered_office": {"address": address}}
    officer: dict[str, Any] = {"role": _PROPOSED_ROLE[event_type].value}
    officer.update(new_values)
    party = event["party"]
    for key in ("name_en", "name_zh"):
        if officer.get(key) is None and party.get(key) is not None:
            officer[key] = party[key]
    date_key = (
        "appointment_date" if event_type in _APPOINTMENT_TYPES else "cessation_date"
    )
    if officer.get(date_key) is None and event.get("effective_date") is not None:
        officer[date_key] = event["effective_date"]
    slot = "appointment" if date_key == "appointment_date" else "cessation"
    return {slot: {"officer": officer}}


def proposal_violations(
    *, event: Mapping[str, Any], reference_only: bool = False
) -> list[str]:
    """Every reason the proposal cannot become an event without guessing.

    A null effective date is named first — it is the fact the client is asked
    for again — then any date the grammar's ISO *shape* admitted but the
    calendar refuses: the office-change and other payloads carry no date for
    the payload grammar to catch, and no payload check exists for
    ``informed_date`` at all, so the refusal lives here, one home, read by
    the router, the landing, and the letter alike (the scoring post-check
    downgrades these dates too — it explains the score; this refusal is what
    bends the reply instead of crashing on ``date.fromisoformat``). The
    payload grammar's own violations judge the rest. An empty list means the
    proposal is complete; the no-guessing rule reads this list, and so does
    the review queue's letter.

    ``reference_only`` judges a proposal nothing is filed from — a
    withdrawal reference, where the event it points at already exists: the
    completeness legs (a null effective date, the payload grammar) are
    waived, because a withdrawal that states nothing files nothing, but a
    date it does state must still be a real calendar day — the letter
    restates the proposal, and an impossible date must bend the reply to
    clarification rather than crash the restatement. The calendar refusals
    are the one home either way.
    """
    event_type = ChangeEventType(event["type"])
    violations: list[str] = []
    effective = event.get("effective_date")
    if effective is None:
        if not reference_only:
            violations.append(
                "effective_date: null — a proposal without an effective date cannot "
                "satisfy the change_event.* grammar without guessing"
            )
    elif not _is_iso_date(effective):
        violations.append(
            "effective_date: not a calendar day — an impossible effective date "
            "cannot satisfy the change_event.* grammar without guessing"
        )
    informed = event.get("informed_date")
    if informed is not None and not _is_iso_date(informed):
        violations.append(
            "informed_date: not a calendar day — an impossible informed date "
            "cannot enter the record without guessing"
        )
    if not reference_only:
        violations.extend(
            payload_violations(event_type=event_type, payload=payload_proposal(event=event))
        )
    return violations


def joined_english_name(*, surname_en: str | None, other_names_en: str | None) -> str | None:
    """The split English name pair as the register's one joined form.

    ``"<surname_en> <other_names_en>"`` when both parts are stated, and ``None``
    when the pair is not whole — a join around a missing part would invent a
    name, and half a pair is not a person's English name. This is the join
    rule's **one home** (ruling R-T4b): ``app.application.reply_parsing`` builds
    the master-data lexicon and the display name from it (``_person_names`` /
    ``_person_name``), and R-D's register comparison compares the form it
    returns, so what the lexicons call a person's English name and what the gate
    compares cannot drift apart.

    It exists as a public name because the register builder lives in the
    application layer — the domain's callers and the lexicon's caller are on
    opposite sides of that boundary, and the shared convention has to sit where
    both can read it.
    """
    if surname_en and other_names_en:
        return f"{surname_en} {other_names_en}"
    return None


def _text_values(source: Mapping[str, Any], keys: Sequence[str]) -> tuple[str, ...]:
    """The non-empty text values ``source`` carries under ``keys``."""
    values: list[str] = []
    for key in keys:
        value = source.get(key)
        if isinstance(value, str) and value.strip():
            values.append(value)
    return tuple(values)


def _proposal_name_forms(event: Mapping[str, Any]) -> tuple[str, ...]:
    """Every natural-person name form a proposal's *landed payload* carries.

    Both channels, because the payload is built from both: the ``party``
    object's three name forms, and the ``new_values`` the officer block copies
    verbatim — its identity keys, the alternate-director text box, and the
    ``{name_zh, name_en}`` entries of ``previous_names``/``aliases``
    (``NAME_HISTORY_KEYS``). A name written in ``new_values`` is the *same*
    name: the review finding that produced this walk showed a stranger's
    ``new_values.name_en`` auto-accepting beside a party naming a registered
    person (``payload_proposal`` fills the officer's name from the party only
    when ``new_values`` left it empty), and the recorded model run
    (``replies[16]``, R17) emits ``new_values: {"name_zh": ...}``.

    **A split ``surname_en``/``other_names_en`` pair is one person, not two
    fragments** (ruling R-T4b): the officer grammar *expects* English names in
    two boxes while the register holds the joined form ``_person_names`` builds,
    so the pair goes through ``joined_english_name`` and only the joined form is
    compared. Comparing the fragments would route ``staff_review`` for every
    correctly split appointment of a registered person — the over-demotion the
    re-review found. Half a pair is not a pair: whatever part is stated is then
    the only name this channel carries, and it is compared as one.

    Identifiers are not names and are deliberately not read: ``partial_id``,
    and the officer block's ``br_number``/``hkid_partial``/``passport_partial``
    — a stranger's identifier is not a proposed person. Precondition: ``event``
    passed the grammar.
    """
    forms: list[str] = []
    party = event.get("party")
    if isinstance(party, Mapping):
        forms.extend(_text_values(party, _PARTY_NAME_KEYS))
    new_values = event.get("new_values")
    if isinstance(new_values, Mapping):
        forms.extend(_text_values(new_values, _OFFICER_NAME_KEYS))
        joined = joined_english_name(
            surname_en=new_values.get("surname_en"),
            other_names_en=new_values.get("other_names_en"),
        )
        if joined is None:
            forms.extend(_text_values(new_values, _SPLIT_NAME_KEYS))
        else:
            forms.append(joined)
        # The grammar's own name-pair keys, sorted so the collected order is
        # deterministic across processes (``frozenset`` iteration is not).
        history_keys = tuple(sorted(NAME_HISTORY_KEYS))
        for key in _OFFICER_NAME_HISTORY_KEYS:
            entries = new_values.get(key)
            if isinstance(entries, list):
                for entry in entries:
                    if isinstance(entry, Mapping):
                        forms.extend(_text_values(entry, history_keys))
    return tuple(dict.fromkeys(forms))


def _name_off_register(name: str, *, registered_names: Sequence[str]) -> bool:
    """Whether one proposed name form is a name the company's register lacks.

    **The comparison is exact after normalization** (ruling R-T4a). The
    comparison form — ``normalize_for_matching``'s NFKC, casefold, whitespace
    collapse and bounded traditional↔simplified fold — makes two spellings one
    name: ``陈大文`` against a register's ``陳大文`` is the registered person,
    and so is ``CHAN  TAI MAN`` against ``Chan Tai Man``. Nothing else counts.

    The tokenizer's one-edit radius is deliberately **not** reused here, because
    the two consumers' error costs point opposite ways: the tokenizer
    over-matches to protect, where a false match costs a staff glance, while
    this gate denies by default, where a false match *removes* a glance. So
    ``陳大民`` against ``陳大文``, or ``Chan Tay Man`` against
    ``Chan Tai Man``, is a different person to this rule and routes staff
    review — the fail-safe answer, and the reason a register name is never
    guessed at. A form that normalizes to nothing names no one, and takes the
    same fail-safe answer.
    """
    needle = normalize_for_matching(name).strip()
    if not needle:
        return True
    return all(
        normalize_for_matching(entry).strip() != needle for entry in registered_names
    )


def unregistered_person_names(
    *, change_events: Sequence[Mapping[str, Any]], registered_names: Sequence[str]
) -> tuple[str, ...]:
    """The name forms a proposal introduces that the register does not know.

    Ruling R-D in one home, read twice: ``route_parse`` caps the band on a
    non-empty result, and the application records the result as the reason
    staff see (the two readers ``proposal_violations`` has). Every name form
    the proposal's landed payload can carry — both channels,
    ``_proposal_name_forms`` — is compared against the register's appointment
    names, the same script forms the gateway's ``PERSON`` lexicon is built from,
    passed in by the caller that already reads them. The comparison is
    ``_name_off_register``: **exact after normalization** (ruling R-T4a), which
    is deliberately stricter than the tokenizer's bounded-fuzzy radius, because
    this is a deny-by-default gate. Only the proposals that name a person are
    judged (``_PERSON_EVENT_TYPES``).

    An empty tuple means every person named is on the register; the order is
    the parse's own, de-duplicated. Precondition: every event passed the
    closed grammar, and ``registered_names`` are the company's in-office
    appointment names as of the parse's date (an empty sequence is a company
    with no appointments recorded — nobody is on the register, so every named
    person is absent, which is the fail-safe reading).
    """
    missing: list[str] = []
    for event in change_events:
        if ChangeEventType(event["type"]) not in _PERSON_EVENT_TYPES:
            continue
        for name in _proposal_name_forms(event):
            if name not in missing and _name_off_register(name, registered_names=registered_names):
                missing.append(name)
    return tuple(missing)


def route_parse(
    *,
    scores: Mapping[str, float],
    change_events: Sequence[Mapping[str, Any]],
    registered_names: Sequence[str],
) -> ConfidenceBand | None:
    """The §2.7 routing for a scored parse, or ``None`` when nothing was scored.

    The band is ``app.domain.confidence``'s one rule over the field scores —
    a reply is only as strong as its weakest parsed field. Two rules then bend
    it for the proposals, and both only ever *downward*:

    - the no-guessing rule: any proposed change event that cannot satisfy the
      payload grammar without guessing routes the whole parse to
      ``client_clarification`` **regardless of score**, because a flawless
      score on an uncompletable proposal is exactly the high-confidence
      mis-extraction the bar forbids (scope §7);
    - ruling R-D: a proposal naming a person the company's register does not
      carry (``unregistered_person_names``) caps an ``auto_accept`` band at
      ``staff_review``, so a name nobody in the company's own records has ever
      held can never be auto-accepted.

    The cap is a floor, not a target: a band the score or the no-guessing rule
    already put below ``auto_accept`` keeps its place — nothing this rule
    cannot see is upgraded. ``registered_names`` is the company's in-office
    appointment name forms as of the parse's date, the same read the caller
    already makes to build the gateway's lexicon; it is passed in so this
    stays a pure function.
    """
    band = routing_for(scores)
    if band is None:
        return None
    if any(proposal_violations(event=event) for event in change_events):
        return ConfidenceBand.CLIENT_CLARIFICATION
    if band is ConfidenceBand.AUTO_ACCEPT and unregistered_person_names(
        change_events=change_events, registered_names=registered_names
    ):
        return ConfidenceBand.STAFF_REVIEW
    return band


def _normalize_party_name(name: str) -> str:
    """Party-name comparison: case and surrounding whitespace are not part of
    a name — internal spacing is left alone (it may carry meaning)."""
    return name.strip().casefold()


@dataclass(frozen=True)
class EarlierEvent:
    """The slice of a stored change event the withdrawal/correction matcher
    reads — the application layer projects its rows into this shape."""

    id: UUID
    event_type: ChangeEventType
    status: ChangeEventStatus
    party_names: tuple[str, ...]


def match_earlier_event(
    *,
    event: Mapping[str, Any],
    earlier_events: Sequence[EarlierEvent],
) -> EarlierEvent | None:
    """The earlier event a ``withdrawal`` or ``correction`` refers to.

    The match is the earlier event of the same type naming the same party —
    any of the party's name forms, normalized — in a status the action can
    still land on (``_WITHDRAWABLE_STATUSES``: every §4.1 row with a
    ``client_withdraw`` action). Zero matches mean the reference is dangling;
    more than one is ambiguity; both refuse, because a guessed link would
    withdraw or correct the wrong event — the ``match_request`` precedent.
    Precondition: ``event`` passed the grammar.
    """
    event_type = ChangeEventType(event["type"])
    party = event["party"]
    proposed = {
        _normalize_party_name(name)
        for name in (party.get("name_en"), party.get("name_zh"), party.get("name_as_written"))
        if isinstance(name, str) and name.strip()
    }
    matched = [
        earlier
        for earlier in earlier_events
        if earlier.event_type is event_type
        and earlier.status in _WITHDRAWABLE_STATUSES
        and bool(proposed & {_normalize_party_name(name) for name in earlier.party_names})
    ]
    if len(matched) == 1:
        return matched[0]
    return None


__all__ = [
    "ENTERABLE_EVENT_TYPES",
    "INFERRED_PREFIX",
    "INVALID_DATE_PENALTY",
    "PARSE_LITERALS",
    "SPAN_VIOLATION_PENALTY",
    "UNCERTAIN_PENALTY",
    "UNKNOWN_FACT_ID_PENALTY",
    "ConfirmationAnswer",
    "EarlierEvent",
    "MentionKind",
    "ReplyLanguage",
    "ReplyStatus",
    "field_scores",
    "joined_english_name",
    "match_earlier_event",
    "parse_violations",
    "payload_proposal",
    "proposal_violations",
    "route_parse",
    "span_is_verbatim",
    "unregistered_person_names",
]
