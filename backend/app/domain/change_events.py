"""The change-event lifecycle: vocabularies, the §4.1 transition table, the
typed-payload grammar, and the allowed-actions read (scope §2.7-§2.8;
data-model §2.6; contract §4.1, §5.1).

A change event is what the product exists to carry: a change in a Hong Kong
company's particulars (a director or secretary appointment or cessation, a
registered-office move, anything else staff record by hand) from the moment
the TCSP learns of it to the moment a filing carries it. This module owns the
part of that which is pure: the words, the table, and the shape of what a
payload may say.

**The §4.1 table, as data.** ``CHANGE_EVENT_TRANSITIONS`` is the contract's
table transcribed — every From/Action/To row, the wiring-less ones included,
because ADR-0008 requires the table to encode the spec whole and be tested
against scope §2.8's matrix. The guard over it is the *generic*
``can_transition`` (``app.domain.service_renewals``), reused rather than
copied: ADR-0008 asks for one guard over every lifecycle's table, and this is
the second table behind it. Two rows need a word of explanation:

- **``submitted`` + ``supersede``.** §4.1's target is "(new event)": the
  supersession creates a new event carrying ``relation: supersedes`` and
  leaves the submitted one where it is — so the row's target here is its own
  status, and the side effect (the new event, the draft linkage) is Phase 5's.
  The action is unwired, and refused rather than half-performed.
- **``client_withdraw`` from "any non-submitted".** Spelled out over the
  statuses scope §2.8's matrix gives a client a withdraw at — the six
  non-terminal ones. ``withdrawn`` and ``deleted`` are ends: §2.8's matrix
  has no row for them, and withdrawing a withdrawn event is not a state the
  product has.

**Two legs, one guard.** §4.1's "Actor permission" column is data too
(``ACTION_PERMISSIONS``, keyed by (status, action) because ``edit``/``delete``
and ``revert`` carry different legs at different statuses): a reviewer action
needs ``review_events``, an approver action ``approve_events``, and the three
system rows admit no staff actor at all. "Administrator may perform any
action" is not a row here — it is ``app.domain.rbac.is_allowed``'s
Administrator rule, so this table never has to repeat it, and the API layer's
one decision mechanism stays the only place role is read.

**The review queue's vocabulary.** ``AWAITING_REVIEW_STATUSES`` names the
statuses that count as "awaiting staff review" — ``pending`` and
``under_review`` — for the cross-company queue read and the dashboard's
pending-event-review widget alike; the constant's own note states why
``client_reconfirming`` and ``reviewed`` are not among them.

**Wired, and refused by name.** The actions whose side effects (or entry
statuses) belong to later work are implemented in the table but refused at
the executor with a named reason (``UNWIRED_REASONS``:
``reconfirmation_not_wired``, ``supersede_not_wired``) — never accepted,
never half-performed, and never offered by the allowed-actions read, which
is why the read can be trusted to render buttons (``allowed_actions``).
``reconfirmation_not_wired`` names ``override`` alone since M5b wired the
reconfirmation exchange: ``send_reconfirmation`` is the review queue's
re-ask (a reviewer's row at ``pending`` — the compose-store-release is
``app.application.reconfirmation``'s, M5b task 5), and ``client_reply`` is
§4.1's **system** row, which the reply parser's landing performs when a
reply joins a reconfirmation's thread
(``app.application.change_events.client_reply_from_parse``) — a system row
is nobody's to press, so it is in ``WIRED_ACTIONS`` with no staff leg, and
a staff POST of it answers the executor's system-row refusal, the ``apply``
precedent. P6-4 wired the third submission row: ``submit`` is §4.1's
**system** row the draft's export performs (``app.application.change_events``
' ``submit_applied_events`` moves each applied event to ``submitted``) — in
``WIRED_ACTIONS`` with no staff leg (``ACTION_PERMISSIONS`` gives the row
``None``), never offered by the transitions read, and a staff POST answers
the system-row refusal — and with it ``draft_not_wired`` left the
vocabulary entirely.

**The typed payloads.** ``payload`` is a closed grammar per event type, in the
house style of the pack payload grammar and the command guard: exact key sets,
bounded values, no free-form structure. The shapes are the contract's
``change_event.*`` vocabulary — the registry targets of appendix A, canonical
in ``backend/forms/nd2a/mapping.json``: an appointment is
``payload.appointment.officer`` carrying the §2.4 officer vocabulary plus the
ND2A item's own two fields, a cessation is ``payload.cessation.officer``
carrying the identity fields and the cessation facts. Both officer shapes carry
the registry's **capacity group** — ``capacity`` (the boxes ticked, in the
registry's own values) and ``alternate_to`` (the ``代替`` text box), with §2.4's
rule enforced: the name is required exactly when the alternate box is ticked
and refused otherwise. That is where the **alternate director** lives on this
form: ND2A's 候補董事 is the ``alternate_director`` *capacity*, with
``alternate_to`` naming the director being substituted — the same office NAR1
records through its own 候補董事 checkbox (``cb_2_P.5``) as
``officers[].alternate_to``. ``reserve_director`` is a **different office**:
NAR1's separate Reserve Director block (備任董事, Cap. 622 s.455 — the private
company with one member who is also its sole director), which is not a
capacity, carries no ``alternate_to``, and is no box this form prints.
``registered_office_change``
and ``other`` have no registry target yet (NR1 is v1.1), so their shapes are
this module's decision, stated where they are defined: a §2.5 Address under
``registered_office``, and one bounded line of staff's own words under
``summary`` — the one place the grammar admits prose, because an
unclassifiable change that cannot say what it is is a row nobody can review.
``continuation_sheet_count`` is **derived** (§3) and refused by name wherever
it appears.

**The §1 PII boundary, and the class this grammar refuses.** The public-page
identification values the ND2A's public pages carry — ``hkid_partial``,
``passport_partial``, ``passport_issuing_country_region`` — are ordinary
payload fields, so a real event stays fully enterable. The **protected class**
is not: ``hkid_full`` + ``hkid_check_digit``, ``passport_full`` and
``usual_residential_address`` are refused, wherever they appear, with one
named reason (``protected_class_unavailable``). Contract §1 stores that class
encrypted with per-tenant keys (§8), no field-level encryption exists yet
(Phase 7), no other table in this build holds the class, and its only consumer
(the ND2A PI sheet) is Phase 5's — so a change-event payload must not become
the first place the plaintext lives. Fail-safe: deny by default; the refusal
names the field so a caller knows which one to drop. Lifting it is the
encryption slice's or the founder's call, and it is a one-line table change
here when that ruling comes. Nothing in this module contacts an LLM boundary,
and §7's minimization rule (protected-class fields excluded from every LLM
payload) applies to whatever reads these events later.

Pure and framework-free per ADR-0006: the standard library, ``app.domain.rbac``
for the permission vocabulary, ``app.domain.contacts`` for the one email rule
the contact channels already own, and ``app.domain.service_renewals`` for the
shared guard.
"""

import json
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from typing import Any, NotRequired, TypedDict

from app.domain.contacts import ChannelType, InvalidChannelIdentifierError, validate_identifier
from app.domain.rbac import Permission
from app.domain.service_renewals import can_transition


class ChangeEventStatus(StrEnum):
    """Where an event stands (§4.1). Nine values, and every one of them is a
    row of the transition table — a status with no way in is still a status
    (the reply parser and Phase 5 will reach three of them)."""

    PENDING = "pending"
    CLIENT_RECONFIRMING = "client_reconfirming"
    UNDER_REVIEW = "under_review"
    REVIEWED = "reviewed"
    CONFIRMED = "confirmed"
    APPLIED_TO_DRAFT = "applied_to_draft"
    SUBMITTED = "submitted"
    WITHDRAWN = "withdrawn"
    DELETED = "deleted"


# The statuses the cross-company review queue and the dashboard's
# pending-event-review widget list: work waiting on staff — not started
# (``pending``) or started and not finished (``under_review``). The two
# statuses that were considered and are deliberately **not** here:
# ``client_reconfirming`` is out with the client (a reply returns it to
# ``pending``, which is when staff work exists again), and ``reviewed`` is the
# *approval* gate's (scope §2.3's separate "pending event approval" widget).
# The constant is the one statement of that decision, so the queue's query and
# the widget's count cannot drift apart.
AWAITING_REVIEW_STATUSES: frozenset[ChangeEventStatus] = frozenset(
    {ChangeEventStatus.PENDING, ChangeEventStatus.UNDER_REVIEW}
)


class ChangeEventType(StrEnum):
    """What changed (§2.6; mirrors the DB check).

    The superset of the reply-parse enum: ``company_name_change`` is reserved
    for Wave 2/NNC2 (``RESERVED_TYPES`` — refused at entry, never stored), and
    ``other`` is scope §2.7's escape hatch for what the vocabulary cannot
    classify.
    """

    DIRECTOR_APPOINTMENT = "director_appointment"
    DIRECTOR_CESSATION = "director_cessation"
    SECRETARY_APPOINTMENT = "secretary_appointment"
    SECRETARY_CESSATION = "secretary_cessation"
    REGISTERED_OFFICE_CHANGE = "registered_office_change"
    COMPANY_NAME_CHANGE = "company_name_change"
    OTHER = "other"


class ChangeEventSourceType(StrEnum):
    """How the event got here (§2.6): a client communication, a document, or
    staff's own words. All three land in the same review queue; this slice
    writes ``manual`` only, and the parser's two values are in the vocabulary
    because the column's CHECK admits them."""

    COMMUNICATION = "communication"
    DOCUMENT = "document"
    MANUAL = "manual"
    # Workstream ③ (migration 0036): a split work item's slice — a work-item
    # parse's proposals name their source; the value is in the vocabulary
    # because the column's CHECK admits it, this module's own note below.
    SPLIT_WORK_ITEM = "split_work_item"


class ChangeEventRelation(StrEnum):
    """How a new event refers to an earlier one (§2.6): it corrects it,
    withdraws it, or supersedes it. The contract's reply-parse vocabulary."""

    CORRECTS = "corrects"
    WITHDRAWS = "withdraws"
    SUPERSEDES = "supersedes"


class OfficerRole(StrEnum):
    """An officer's role (§2.4's ``OfficerAppointmentRecord.role``).

    ``reserve_director`` is **NAR1's separate Reserve Director block**
    (備任董事, Cap. 622 s.455 — the private company with one member who is also
    its sole director; ``backend/forms/nar1/mapping.json``). It is **not** an
    alternate directorship and **not** a capacity: that block prints no capacity
    boxes and no ``代替 Alternate to`` box. ND2A does not print the reserve
    director at all. The alternate director is a different office — ND2A ticks
    it as the ``alternate_director`` *capacity* below (候補董事), and NAR1
    records it through its own 候補董事 checkbox (``cb_2_P.5``) as
    ``officers[].alternate_to``.
    """

    COMPANY_SECRETARY = "company_secretary"
    DIRECTOR = "director"
    RESERVE_DIRECTOR = "reserve_director"


class OfficerCapacity(StrEnum):
    """ND2A's capacity checkboxes, in the registry's own values.

    The capacity group prints three boxes and ``backend/forms/nd2a/mapping.json``
    names them exactly: ``company_secretary`` 公司秘書, ``director`` 董事,
    ``alternate_director`` 候補董事 (the form's "Alternate Director" — the office
    NAR1 records through its own 候補董事 checkbox as
    ``officers[].alternate_to``, and not NAR1's separate Reserve Director block,
    which is ``reserve_director`` and a different office). The box's
    ``代替 Alternate to`` text box belongs to the alternate tick alone (§2.4's
    ``alternate_to``).
    """

    COMPANY_SECRETARY = "company_secretary"
    DIRECTOR = "director"
    ALTERNATE_DIRECTOR = "alternate_director"


class CessationReason(StrEnum):
    """Why an officer left, as the ND2A cessation block asks (§2.6)."""

    RESIGNATION_OTHERS = "resignation_others"
    DECEASED = "deceased"


class ChangeEventAction(StrEnum):
    """Every action the §4.1 table names (the request body's vocabulary).

    The full vocabulary, unwired members included: the API must *accept* the
    name to refuse it with its named 409 — an action it could not parse would
    be a 422 about a value the product's own contract prints.
    """

    MARK_UNDER_REVIEW = "mark_under_review"
    SEND_RECONFIRMATION = "send_reconfirmation"
    OVERRIDE = "override"
    CLIENT_REPLY = "client_reply"
    MARK_REVIEWED = "mark_reviewed"
    REVERT = "revert"
    REJECT = "reject"
    CONFIRM = "confirm"
    APPLY = "apply"
    SUBMIT = "submit"
    SUPERSEDE = "supersede"
    EDIT = "edit"
    DELETE = "delete"
    CLIENT_WITHDRAW = "client_withdraw"


class Address(TypedDict):
    """The §2.5 address value object, as the payloads carry it.

    Four core boxes and the one optional part — the CR box composition.
    ``country`` is omitted for HK-only addresses (the secretary's NAR1 boxes
    are four, Region pre-printed), never invented as an empty string.
    """

    flat: str
    building: str
    street: str
    district: str
    country: NotRequired[str]


# The keys each payload shape *recognizes*. A key outside its set is a
# violation, not a silent pass: the payload is what the ND2A draft will read,
# and a field nobody defined travelling with it is a typo or an invention.
#
# The protected class is recognized in both officer shapes — so that its
# refusal is the named one (``protected_class_unavailable``) rather than an
# unknown-key message — and `_protected_class_violations` then refuses every
# one of them. Recognized is not admitted: see the module docstring.
_OFFICER_IDENTITY_KEYS = frozenset({"name_zh", "name_en", "surname_en", "other_names_en"})
PROTECTED_CLASS_FIELDS: tuple[str, ...] = (
    "hkid_full",
    "hkid_check_digit",
    "passport_full",
    "usual_residential_address",
)
_APPOINTMENT_OFFICER_KEYS = frozenset(
    {
        "role",
        "capacity",
        "alternate_to",
        *_OFFICER_IDENTITY_KEYS,
        "previous_names",
        "aliases",
        "email",
        "br_number",
        "correspondence_address",
        "registered_office_address",
        "hkid_partial",
        "passport_partial",
        "passport_issuing_country_region",
        *PROTECTED_CLASS_FIELDS,
        "tcsp_licence",
        "appointment_date",
        "existing_director_at_appointment",
    }
)
_CESSATION_OFFICER_KEYS = frozenset(
    {
        "role",
        "capacity",
        "alternate_to",
        *_OFFICER_IDENTITY_KEYS,
        "hkid_partial",
        "passport_partial",
        *PROTECTED_CLASS_FIELDS,
        "cessation_reason",
        "cessation_date",
        "continues_office_after_cessation",
    }
)
_PAYLOAD_KEY_BY_TYPE: Mapping[ChangeEventType, str] = {
    ChangeEventType.DIRECTOR_APPOINTMENT: "appointment",
    ChangeEventType.SECRETARY_APPOINTMENT: "appointment",
    ChangeEventType.DIRECTOR_CESSATION: "cessation",
    ChangeEventType.SECRETARY_CESSATION: "cessation",
    ChangeEventType.REGISTERED_OFFICE_CHANGE: "registered_office",
    ChangeEventType.OTHER: "summary",
}

# The roles each type can carry: a director-type event names a director (or a
# reserve director), a secretary-type event a company secretary. The role is
# what the ND2A block and the master data are written from, so a mismatch is a
# category error rather than a nuance — and it is what the draft overlay
# (``app.domain.drafts``) matches an event to an officer record by, so the
# grammar and the resolution cannot disagree about which offices a type names.
# Public for that second reader; see the module docstring.
TYPE_ROLES: Mapping[ChangeEventType, frozenset[OfficerRole]] = {
    ChangeEventType.DIRECTOR_APPOINTMENT: frozenset(
        {OfficerRole.DIRECTOR, OfficerRole.RESERVE_DIRECTOR}
    ),
    ChangeEventType.DIRECTOR_CESSATION: frozenset(
        {OfficerRole.DIRECTOR, OfficerRole.RESERVE_DIRECTOR}
    ),
    ChangeEventType.SECRETARY_APPOINTMENT: frozenset({OfficerRole.COMPANY_SECRETARY}),
    ChangeEventType.SECRETARY_CESSATION: frozenset({OfficerRole.COMPANY_SECRETARY}),
}

# Which capacity boxes an officer's role can have ticked. The registry prints
# the same three boxes in every officer block, so the constraint is the office
# rather than the block: a company secretary's block has no alternate box, and a
# directorship can be an alternate directorship. `reserve_director` is a
# directorship here because NAR1's Reserve Director block is a director's
# office (it simply prints no capacity boxes of its own) — it is not the
# alternate office, and nothing in this mapping makes it one.
#
# Public because it is the one home of the rule: the officer master record
# (``app.domain.officers``) imports this mapping rather than restating it.
OFFICE_CAPACITIES: Mapping[OfficerRole, frozenset[OfficerCapacity]] = {
    OfficerRole.COMPANY_SECRETARY: frozenset({OfficerCapacity.COMPANY_SECRETARY}),
    OfficerRole.DIRECTOR: frozenset(
        {OfficerCapacity.DIRECTOR, OfficerCapacity.ALTERNATE_DIRECTOR}
    ),
    OfficerRole.RESERVE_DIRECTOR: frozenset(
        {OfficerCapacity.DIRECTOR, OfficerCapacity.ALTERNATE_DIRECTOR}
    ),
}

# A capacity list is the form's ticks: at most the registry's three boxes, and
# the name-history bound is the same kind of reviewed limit.
MAX_CAPACITIES = 3

# The value objects' key sets, public for the same reason as the mapping: the
# officer master record carries the same address shape and the same name-pair
# list.
NAME_HISTORY_KEYS = frozenset({"name_zh", "name_en"})
_TCSP_LICENCE_KEYS = frozenset({"licence_no", "not_required", "not_required_reason"})
ADDRESS_REQUIRED_KEYS = ("flat", "building", "street", "district")
ADDRESS_KEYS = frozenset({*ADDRESS_REQUIRED_KEYS, "country"})

# The reviewed bounds. Generous for a real officer's particulars, tight enough
# that a payload cannot be a document store or a smuggling route.
MAX_TEXT_CHARS = 200
MAX_ID_CHARS = 40
MAX_EMAIL_CHARS = 320
MAX_SUMMARY_CHARS = 200
MAX_NAME_HISTORY_ENTRIES = 20
MAX_PAYLOAD_BYTES = 65_536

# §1's protected class, and the one name its refusal answers with (contract
# §5.1.3). The class is stored encrypted with per-tenant keys (§1, §8), and no
# field-level encryption exists yet (Phase 7), no other table holds it, and its
# only consumer (the ND2A PI sheet) is Phase 5's — so the grammar refuses the
# four fields (``PROTECTED_CLASS_FIELDS`` above) rather than making a
# change-event payload the first place the plaintext lives (the ruling of
# 2026-09-25). Fail-safe: deny by default. The partial identifiers
# (``hkid_partial``, ``passport_partial``, ``passport_issuing_country_region``)
# are unaffected: they are what the public ND2A pages carry, and a real event
# stays enterable without the class.
PROTECTED_CLASS_UNAVAILABLE = "protected_class_unavailable"


# ADR-0008's table as data: for every status, the actions it admits and the
# status each moves to. A status with no entries is terminal (`withdrawn` and
# `deleted` are ends — §2.8: nothing is hard-deleted, and a withdrawn event
# cannot be withdrawn again).
#
# The rows are the contract §4.1's, in its order. See the module docstring for
# the two rows that need reading (`submitted` + `supersede`, and the
# spell-out of `client_withdraw` over §2.8's non-terminal statuses).
CHANGE_EVENT_TRANSITIONS: Mapping[
    ChangeEventStatus, Mapping[ChangeEventAction, ChangeEventStatus]
] = {
    ChangeEventStatus.PENDING: {
        ChangeEventAction.MARK_UNDER_REVIEW: ChangeEventStatus.UNDER_REVIEW,
        ChangeEventAction.SEND_RECONFIRMATION: ChangeEventStatus.CLIENT_RECONFIRMING,
        ChangeEventAction.EDIT: ChangeEventStatus.PENDING,
        ChangeEventAction.DELETE: ChangeEventStatus.DELETED,
        ChangeEventAction.CLIENT_WITHDRAW: ChangeEventStatus.WITHDRAWN,
    },
    ChangeEventStatus.CLIENT_RECONFIRMING: {
        ChangeEventAction.CLIENT_REPLY: ChangeEventStatus.PENDING,
        ChangeEventAction.OVERRIDE: ChangeEventStatus.UNDER_REVIEW,
        ChangeEventAction.MARK_UNDER_REVIEW: ChangeEventStatus.UNDER_REVIEW,
        ChangeEventAction.EDIT: ChangeEventStatus.CLIENT_RECONFIRMING,
        ChangeEventAction.DELETE: ChangeEventStatus.DELETED,
        ChangeEventAction.CLIENT_WITHDRAW: ChangeEventStatus.WITHDRAWN,
    },
    ChangeEventStatus.UNDER_REVIEW: {
        ChangeEventAction.MARK_REVIEWED: ChangeEventStatus.REVIEWED,
        ChangeEventAction.REVERT: ChangeEventStatus.PENDING,
        ChangeEventAction.REJECT: ChangeEventStatus.DELETED,
        ChangeEventAction.EDIT: ChangeEventStatus.UNDER_REVIEW,
        ChangeEventAction.DELETE: ChangeEventStatus.DELETED,
        ChangeEventAction.CLIENT_WITHDRAW: ChangeEventStatus.WITHDRAWN,
    },
    ChangeEventStatus.REVIEWED: {
        ChangeEventAction.CONFIRM: ChangeEventStatus.CONFIRMED,
        ChangeEventAction.REVERT: ChangeEventStatus.UNDER_REVIEW,
        ChangeEventAction.REJECT: ChangeEventStatus.DELETED,
        ChangeEventAction.CLIENT_WITHDRAW: ChangeEventStatus.WITHDRAWN,
    },
    ChangeEventStatus.CONFIRMED: {
        ChangeEventAction.APPLY: ChangeEventStatus.APPLIED_TO_DRAFT,
        ChangeEventAction.REVERT: ChangeEventStatus.REVIEWED,
        ChangeEventAction.EDIT: ChangeEventStatus.CONFIRMED,
        ChangeEventAction.DELETE: ChangeEventStatus.DELETED,
        ChangeEventAction.CLIENT_WITHDRAW: ChangeEventStatus.WITHDRAWN,
    },
    ChangeEventStatus.APPLIED_TO_DRAFT: {
        ChangeEventAction.REVERT: ChangeEventStatus.CONFIRMED,
        ChangeEventAction.SUBMIT: ChangeEventStatus.SUBMITTED,
        ChangeEventAction.EDIT: ChangeEventStatus.APPLIED_TO_DRAFT,
        ChangeEventAction.DELETE: ChangeEventStatus.DELETED,
        ChangeEventAction.CLIENT_WITHDRAW: ChangeEventStatus.WITHDRAWN,
    },
    ChangeEventStatus.SUBMITTED: {
        ChangeEventAction.SUPERSEDE: ChangeEventStatus.SUBMITTED,
    },
    ChangeEventStatus.WITHDRAWN: {},
    ChangeEventStatus.DELETED: {},
}

# §4.1's "Actor permission" column, keyed by (status, action) because the same
# action carries different legs at different statuses: a reviewer edits a
# `pending` event, an approver edits a `confirmed` one. `None` is the system's
# three rows — no staff account may perform them at all, Administrator
# included (the Administrator rule is `is_allowed`'s and would otherwise have
# to be restated here).
ACTION_PERMISSIONS: Mapping[
    tuple[ChangeEventStatus, ChangeEventAction], Permission | None
] = {
    (ChangeEventStatus.PENDING, ChangeEventAction.MARK_UNDER_REVIEW): (
        Permission.REVIEW_EVENTS
    ),
    (ChangeEventStatus.PENDING, ChangeEventAction.SEND_RECONFIRMATION): (
        Permission.REVIEW_EVENTS
    ),
    (ChangeEventStatus.PENDING, ChangeEventAction.EDIT): Permission.REVIEW_EVENTS,
    (ChangeEventStatus.PENDING, ChangeEventAction.DELETE): Permission.REVIEW_EVENTS,
    (ChangeEventStatus.PENDING, ChangeEventAction.CLIENT_WITHDRAW): (
        Permission.REVIEW_EVENTS
    ),
    (ChangeEventStatus.CLIENT_RECONFIRMING, ChangeEventAction.CLIENT_REPLY): None,
    (ChangeEventStatus.CLIENT_RECONFIRMING, ChangeEventAction.OVERRIDE): (
        Permission.REVIEW_EVENTS
    ),
    (ChangeEventStatus.CLIENT_RECONFIRMING, ChangeEventAction.MARK_UNDER_REVIEW): (
        Permission.REVIEW_EVENTS
    ),
    (ChangeEventStatus.CLIENT_RECONFIRMING, ChangeEventAction.EDIT): Permission.REVIEW_EVENTS,
    (ChangeEventStatus.CLIENT_RECONFIRMING, ChangeEventAction.DELETE): Permission.REVIEW_EVENTS,
    (ChangeEventStatus.CLIENT_RECONFIRMING, ChangeEventAction.CLIENT_WITHDRAW): (
        Permission.REVIEW_EVENTS
    ),
    (ChangeEventStatus.UNDER_REVIEW, ChangeEventAction.MARK_REVIEWED): (
        Permission.REVIEW_EVENTS
    ),
    (ChangeEventStatus.UNDER_REVIEW, ChangeEventAction.REVERT): Permission.REVIEW_EVENTS,
    (ChangeEventStatus.UNDER_REVIEW, ChangeEventAction.REJECT): Permission.APPROVE_EVENTS,
    (ChangeEventStatus.UNDER_REVIEW, ChangeEventAction.EDIT): Permission.REVIEW_EVENTS,
    (ChangeEventStatus.UNDER_REVIEW, ChangeEventAction.DELETE): Permission.REVIEW_EVENTS,
    (ChangeEventStatus.UNDER_REVIEW, ChangeEventAction.CLIENT_WITHDRAW): (
        Permission.REVIEW_EVENTS
    ),
    (ChangeEventStatus.REVIEWED, ChangeEventAction.CONFIRM): Permission.APPROVE_EVENTS,
    (ChangeEventStatus.REVIEWED, ChangeEventAction.REVERT): Permission.REVIEW_EVENTS,
    (ChangeEventStatus.REVIEWED, ChangeEventAction.REJECT): Permission.APPROVE_EVENTS,
    (ChangeEventStatus.REVIEWED, ChangeEventAction.CLIENT_WITHDRAW): Permission.REVIEW_EVENTS,
    (ChangeEventStatus.CONFIRMED, ChangeEventAction.APPLY): None,
    (ChangeEventStatus.CONFIRMED, ChangeEventAction.REVERT): Permission.APPROVE_EVENTS,
    (ChangeEventStatus.CONFIRMED, ChangeEventAction.EDIT): Permission.APPROVE_EVENTS,
    (ChangeEventStatus.CONFIRMED, ChangeEventAction.DELETE): Permission.APPROVE_EVENTS,
    (ChangeEventStatus.CONFIRMED, ChangeEventAction.CLIENT_WITHDRAW): Permission.REVIEW_EVENTS,
    (ChangeEventStatus.APPLIED_TO_DRAFT, ChangeEventAction.REVERT): Permission.APPROVE_EVENTS,
    (ChangeEventStatus.APPLIED_TO_DRAFT, ChangeEventAction.SUBMIT): None,
    (ChangeEventStatus.APPLIED_TO_DRAFT, ChangeEventAction.EDIT): Permission.APPROVE_EVENTS,
    (ChangeEventStatus.APPLIED_TO_DRAFT, ChangeEventAction.DELETE): Permission.APPROVE_EVENTS,
    (ChangeEventStatus.APPLIED_TO_DRAFT, ChangeEventAction.CLIENT_WITHDRAW): (
        Permission.REVIEW_EVENTS
    ),
    (ChangeEventStatus.SUBMITTED, ChangeEventAction.SUPERSEDE): Permission.APPROVE_EVENTS,
}

# The actions this slice implements; everything else is refused with the
# reason its side effects wait on (never accepted, never half-performed).
# ``apply``, ``client_reply`` and — since P6-4 — ``submit`` are wired **as
# system rows** (no staff leg — `ACTION_PERMISSIONS` gives them `None`): the
# transitions read never offers them and a staff POST of any answers the
# executor's system-row refusal, while `app.application.change_events`
# performs each for its own caller: `apply_to_draft` for the draft being
# generated (§4.1 "draft linkage recorded"), `client_reply_from_parse` when a
# reply joins a reconfirmation's thread, and `submit_applied_events` when a
# draft is exported (P6-4 files the events the filing carries).
# ``send_reconfirmation`` is wired with the review leg — the
# compose-store-release rides ``app.application.reconfirmation``.
WIRED_ACTIONS: frozenset[ChangeEventAction] = frozenset(
    {
        ChangeEventAction.MARK_UNDER_REVIEW,
        ChangeEventAction.SEND_RECONFIRMATION,
        ChangeEventAction.CLIENT_REPLY,
        ChangeEventAction.REVERT,
        ChangeEventAction.MARK_REVIEWED,
        ChangeEventAction.REJECT,
        ChangeEventAction.CONFIRM,
        ChangeEventAction.APPLY,
        ChangeEventAction.SUBMIT,
        ChangeEventAction.EDIT,
        ChangeEventAction.DELETE,
        ChangeEventAction.CLIENT_WITHDRAW,
    }
)

# The named refusal for each unwired action (contract §5.1.3). One reason per
# waiting piece of work, because the caller's next step differs:
# ``reconfirmation_not_wired`` names ``override`` alone since M5b wired the
# reconfirmation exchange (``mark_under_review`` covers ``override``'s need);
# ``draft_not_wired`` retired with P6-4, which wired ``submit`` as the system
# row the draft's export performs; and a supersession needs the new-event
# linkage.
UNWIRED_REASONS: Mapping[ChangeEventAction, str] = {
    ChangeEventAction.OVERRIDE: "reconfirmation_not_wired",
    ChangeEventAction.SUPERSEDE: "supersede_not_wired",
}

# §4.1's two "rationale required" rows: a rejection and the staff-recorded
# client withdrawal. Both are refusals or reversals of something a person
# recorded, and the reason is what a reader finds beside them.
_RATIONALE_ACTIONS: frozenset[ChangeEventAction] = frozenset(
    {ChangeEventAction.REJECT, ChangeEventAction.CLIENT_WITHDRAW}
)

# §2.6: `company_name_change` is reserved for Wave 2/NNC2. Entry with it is
# refused by name — the type is in the vocabulary (the registry's enum is the
# superset) and is never stored.
RESERVED_TYPES: frozenset[ChangeEventType] = frozenset({ChangeEventType.COMPANY_NAME_CHANGE})

# §4.1: an edit is a new version of what the event *says*. The linkage
# (`related_event_id`/`relation`) and the provenance (`source_type`/
# `source_id`) are how the event got here, and they do not change under it.
EDIT_FIELDS: frozenset[str] = frozenset(
    {"type", "effective_date", "effective_date_text", "informed_date", "payload"}
)


@dataclass(frozen=True)
class AllowedAction:
    """One action the actor may perform on an event right now.

    What the §5.1 transitions read returns and the frontend renders buttons
    from — so it carries what a button needs: the action, where it moves the
    event, and whether a rationale comes with it.
    """

    action: ChangeEventAction
    to_status: ChangeEventStatus
    rationale_required: bool


def admits(status: ChangeEventStatus, action: ChangeEventAction) -> bool:
    """Whether the §4.1 table admits ``action`` from ``status``.

    The shared guard (``app.domain.service_renewals.can_transition``) asked of
    this lifecycle's table — legality only; *who* may act is the API layer's
    single RBAC decision and the actor's legs below.
    """
    return can_transition(CHANGE_EVENT_TRANSITIONS, status=status, action=action)


def permission_for(
    status: ChangeEventStatus, action: ChangeEventAction
) -> Permission | None:
    """The leg §4.1 gives this row, or ``None`` for the system's.

    ``None`` covers two cases that must not be confused by callers: a system
    row (no staff actor at all), and a row the table does not admit (no leg
    because there is no row) — both are non-pressable, which is what the
    allowed-actions read asks.
    """
    return ACTION_PERMISSIONS.get((status, action))


def is_wired(action: ChangeEventAction) -> bool:
    """Whether this build implements the action (the executor's split)."""
    return action in WIRED_ACTIONS


def unwired_reason(action: ChangeEventAction) -> str | None:
    """The named §5.1.3 refusal for an unwired action, ``None`` when wired."""
    return UNWIRED_REASONS.get(action)


def rationale_required(action: ChangeEventAction) -> bool:
    """Whether §4.1 marks this action "rationale required"."""
    return action in _RATIONALE_ACTIONS


def is_reserved(event_type: ChangeEventType) -> bool:
    """Whether the type is reserved for a later wave (§2.6)."""
    return event_type in RESERVED_TYPES


def is_awaiting_review(status: ChangeEventStatus) -> bool:
    """Whether the review queue lists this status (``AWAITING_REVIEW_STATUSES``)."""
    return status in AWAITING_REVIEW_STATUSES


def allowed_actions(
    *, status: ChangeEventStatus, may_review: bool, may_approve: bool
) -> list[AllowedAction]:
    """The wired, leg-admitted rows from ``status`` — the §5.1 read's model.

    ``may_review``/``may_approve`` are the actor's two event legs as the API
    layer resolved them (``is_allowed`` against the event's company, so an
    Administrator arrives with both true). An unwired row is never offered —
    the executor would refuse it, and a button that always 409s is not an
    allowed action — and a system row is offered to nobody. The order is
    §4.1's own row order, because the table's insertion order is its text.
    """
    offered: list[AllowedAction] = []
    for action, target in CHANGE_EVENT_TRANSITIONS.get(status, {}).items():
        if not is_wired(action):
            continue
        leg = permission_for(status, action)
        if leg is Permission.REVIEW_EVENTS and not may_review:
            continue
        if leg is Permission.APPROVE_EVENTS and not may_approve:
            continue
        if leg is None:
            continue
        offered.append(
            AllowedAction(
                action=action,
                to_status=target,
                rationale_required=rationale_required(action),
            )
        )
    return offered


# --- The typed-payload grammar ----------------------------------------------


def protected_class_fields(payload: object) -> list[str]:
    """The §1 protected-class fields a payload carries, as payload paths.

    The refusal's coordinate: a caller that sends one is answered
    ``protected_class_unavailable: <field>`` and told which field to drop,
    rather than a generic "invalid payload". The walk covers the whole payload
    rather than the officer keys alone — the class is refused wherever it
    appears, whatever the event claims to be — and it is the domain's, so the
    grammar and the application cannot disagree about what the class is.
    """
    found: list[str] = []

    def walk(value: object, path: str) -> None:
        if isinstance(value, Mapping):
            for key, item in value.items():
                child = f"{path}.{key}"
                if key in PROTECTED_CLASS_FIELDS:
                    found.append(child)
                walk(item, child)
        elif isinstance(value, list):
            for index, item in enumerate(value):
                walk(item, f"{path}[{index}]")

    walk(payload, "payload")
    return found


def _protected_class_violations(payload: object) -> list[str]:
    """One refusal per protected-class field present, naming the reason.

    The application promotes these to the dedicated ``protected_class_unavailable``
    answer; here they are part of the grammar, so no caller of this module can
    accept the class by accident.
    """
    return [
        f"{field}: {PROTECTED_CLASS_UNAVAILABLE} "
        "(§1 protected class: stored encrypted, and no field-level encryption "
        "exists yet — §8/Phase 7)"
        for field in protected_class_fields(payload)
    ]


def _object_violations(
    value: object,
    *,
    path: str,
    keys: frozenset[str],
    required: frozenset[str] = frozenset(),
) -> list[str]:
    """A closed object: a mapping, only the named keys, all the required ones.

    Unknown keys are reported singly (sorted, so the list is stable): the
    grammar is a statement of what a payload is, and an unrecognized key
    travelling with it is either a typo the caller wants pointed at or a field
    somebody invented.
    """
    if not isinstance(value, Mapping):
        return [f"{path}: not an object"]
    violations = [
        f"{path}.{key}: unknown key" for key in sorted(set(value) - keys)
    ]
    violations.extend(f"{path}.{key}: required" for key in sorted(required - set(value)))
    return violations


def text_violations(
    value: object, *, path: str, max_chars: int, required: bool = True
) -> list[str]:
    """A bounded, non-blank text — optional when ``required`` is false.

    Public because the officer master record validates the same kinds of value
    against the same bounds (``app.domain.officers``); one rule, one home.
    """
    if value is None:
        return [f"{path}: required"] if required else []
    if not isinstance(value, str):
        return [f"{path}: not text"]
    if not value.strip():
        return [f"{path}: must not be blank"]
    if len(value) > max_chars:
        return [f"{path}: longer than {max_chars} characters"]
    return []


def _date_violations(value: object, *, path: str, required: bool = True) -> list[str]:
    """An ISO date string (``YYYY-MM-DD``), which is how a payload carries
    one: JSON has no date, and the reply-parse path's verbatim text lives in
    ``effective_date_text``."""
    if value is None:
        return [f"{path}: required"] if required else []
    if not isinstance(value, str):
        return [f"{path}: not an ISO date"]
    try:
        date.fromisoformat(value)
    except ValueError:
        return [f"{path}: not an ISO date (YYYY-MM-DD)"]
    return []


def _bool_violations(value: object, *, path: str) -> list[str]:
    """A JSON boolean — ``isinstance(value, bool)`` first, because an integer
    is not a checkbox and ``True == 1``."""
    if not isinstance(value, bool):
        return [f"{path}: not a boolean"]
    return []


def _enum_violations(value: object, *, path: str, vocabulary: type[StrEnum]) -> list[str]:
    if not isinstance(value, str) or value not in {member.value for member in vocabulary}:
        return [f"{path}: not one of {', '.join(sorted(member.value for member in vocabulary))}"]
    return []


def _capacity_violations(
    officer: Mapping[str, Any], *, path: str, role: OfficerRole | None
) -> list[str]:
    """The capacity group: the boxes ticked, and the name the alternate box asks for.

    §2.4's rule is the whole of the second half — ``alternate_to`` exists *only*
    when a capacity is "Alternate to 代替" — so it is required with the
    ``alternate_director`` tick and refused without it: a text box with no
    checkbox behind it, or a checkbox with no name in its box, is a form that
    cannot be printed. The list is the registry's own three values (a
    ``reserve_director`` here is NAR1's separate Reserve Director block — a
    different office, not one of the boxes this form prints) and each value
    must be a box the officer's role can carry.
    """
    violations: list[str] = []
    capacity = officer.get("capacity")
    ticked: list[str] = []
    if capacity is not None:
        if not isinstance(capacity, list):
            violations.append(f"{path}.capacity: not a list")
        elif not capacity:
            violations.append(f"{path}.capacity: must name at least one box")
        elif len(capacity) > MAX_CAPACITIES:
            violations.append(f"{path}.capacity: more than {MAX_CAPACITIES} boxes")
        else:
            for index, value in enumerate(capacity):
                item_path = f"{path}.capacity[{index}]"
                violations.extend(
                    _enum_violations(value, path=item_path, vocabulary=OfficerCapacity)
                )
                if isinstance(value, str) and value in {
                    member.value for member in OfficerCapacity
                }:
                    ticked.append(value)
            if len(set(ticked)) != len(ticked):
                violations.append(f"{path}.capacity: a box is ticked twice")
            if role is not None:
                allowed = OFFICE_CAPACITIES[role]
                for value in sorted(set(ticked)):
                    if OfficerCapacity(value) not in allowed:
                        boxes = ", ".join(sorted(member.value for member in allowed))
                        violations.append(
                            f"{path}.capacity: {value} is not a box a {role.value} "
                            f"officer's block prints ({boxes})"
                        )
    alternate = officer.get("alternate_to")
    if alternate is not None:
        violations.extend(
            text_violations(alternate, path=f"{path}.alternate_to", max_chars=MAX_TEXT_CHARS)
        )
        if not alternate_to_required(ticked):
            violations.append(
                f"{path}.alternate_to: only for an "
                f"{OfficerCapacity.ALTERNATE_DIRECTOR.value} capacity (scope §2.4)"
            )
    elif alternate_to_required(ticked):
        violations.append(
            f"{path}.alternate_to: required with an "
            f"{OfficerCapacity.ALTERNATE_DIRECTOR.value} capacity (scope §2.4)"
        )
    return violations


def alternate_to_required(ticked: Collection[str]) -> bool:
    """Whether the capacity group obliges ``alternate_to`` to be named (§2.4).

    The one statement of the "required exactly when" rule: the 代替 box exists
    only beside the 候補董事 tick, so this predicate is asked by the payload
    grammar above and by the officer master record
    (``app.domain.officers``), which refuses the missing name under its own
    named reason rather than restating the condition.
    """
    return OfficerCapacity.ALTERNATE_DIRECTOR in ticked


def address_violations(value: object, *, path: str) -> list[str]:
    """The §2.5 Address: the four CR boxes required, ``country`` optional.

    The all-or-nothing rule is §5.1.1's (an address the filing does not state
    in full is refused rather than padded), applied here to the payload: an
    event that names a street and no district is not an address a form can
    print.

    Public because the officer master record carries the same shape as filed
    on the appointment (``app.domain.officers``).
    """
    violations = _object_violations(
        value, path=path, keys=ADDRESS_KEYS, required=frozenset(ADDRESS_REQUIRED_KEYS)
    )
    if isinstance(value, Mapping):
        for key in sorted(set(value) & ADDRESS_KEYS):
            violations.extend(
                text_violations(value[key], path=f"{path}.{key}", max_chars=MAX_TEXT_CHARS)
            )
    return violations


def name_history_violations(value: object, *, path: str) -> list[str]:
    """The canonical name-pair list (§3.4): ``{name_zh, name_en}`` entries,
    each naming at least one of the two.

    Public for the same reason as ``text_violations``: the officer master
    record carries the same ``previous_names``/``aliases`` shape.
    """
    if not isinstance(value, list):
        return [f"{path}: not a list"]
    if len(value) > MAX_NAME_HISTORY_ENTRIES:
        return [f"{path}: more than {MAX_NAME_HISTORY_ENTRIES} entries"]
    violations: list[str] = []
    for index, entry in enumerate(value):
        entry_path = f"{path}[{index}]"
        violations.extend(
            _object_violations(entry, path=entry_path, keys=NAME_HISTORY_KEYS)
        )
        if not isinstance(entry, Mapping):
            continue
        for key in sorted(set(entry) & NAME_HISTORY_KEYS):
            violations.extend(
                text_violations(entry[key], path=f"{entry_path}.{key}", max_chars=MAX_TEXT_CHARS)
            )
        if not any(
            isinstance(entry.get(key), str) and str(entry[key]).strip()
            for key in NAME_HISTORY_KEYS
        ):
            violations.append(f"{entry_path}: needs name_zh or name_en")
    return violations


def _tcsp_licence_violations(value: object, *, path: str) -> list[str]:
    """Form note 20's either/or: a licence number, or ``not_required`` with a
    reason. Both branches stated, neither invented."""
    violations = _object_violations(
        value,
        path=path,
        keys=_TCSP_LICENCE_KEYS,
        required=frozenset({"not_required"}),
    )
    if not isinstance(value, Mapping):
        return violations
    not_required = value.get("not_required")
    if not_required is not None and not isinstance(not_required, bool):
        violations.append(f"{path}.not_required: not a boolean")
        return violations
    if not_required is True:
        violations.extend(
            text_violations(
                value.get("not_required_reason"),
                path=f"{path}.not_required_reason",
                max_chars=MAX_TEXT_CHARS,
            )
        )
        if value.get("licence_no") is not None:
            violations.append(f"{path}.licence_no: refused when not_required is true")
    else:
        violations.extend(
            text_violations(
                value.get("licence_no"), path=f"{path}.licence_no", max_chars=MAX_ID_CHARS
            )
        )
    return violations


def _email_violations(value: object, *, path: str) -> list[str]:
    """An email, by the contact channels' own rule (``app.domain.contacts``):
    one ``@``, non-empty local and domain parts. One rule, one home."""
    violations = text_violations(value, path=path, max_chars=MAX_EMAIL_CHARS)
    if violations:
        return violations
    try:
        validate_identifier(ChannelType.EMAIL, str(value))
    except InvalidChannelIdentifierError:
        return [f"{path}: not an email address"]
    return []


def _officer_role(value: object) -> OfficerRole | None:
    """The role as its vocabulary member, or ``None`` when it is not one."""
    if isinstance(value, str) and value in {member.value for member in OfficerRole}:
        return OfficerRole(value)
    return None


def _role_violations(
    officer: Mapping[str, Any], *, path: str, event_type: ChangeEventType
) -> list[str]:
    """The role is in the vocabulary, and it matches the event's type.

    A ``secretary_appointment`` naming a director is a category error: the role
    is what the ND2A block and the master data are written from, so the two
    disagreeing is a request to refuse rather than a nuance to record.
    """
    violations = _enum_violations(officer.get("role"), path=f"{path}.role", vocabulary=OfficerRole)
    role = _officer_role(officer.get("role"))
    if role is not None and role not in TYPE_ROLES[event_type]:
        allowed = ", ".join(sorted(member.value for member in TYPE_ROLES[event_type]))
        violations.append(
            f"{path}.role: {role.value} is not a role of a {event_type.value} ({allowed})"
        )
    return violations


def _officer_violations(
    officer: object, *, path: str, event_type: ChangeEventType, appointment: bool
) -> list[str]:
    """One officer block: the registry's key set for this kind, its required
    substance, the type↔role coherence, and the nested value objects."""
    keys = _APPOINTMENT_OFFICER_KEYS if appointment else _CESSATION_OFFICER_KEYS
    required = {"role"} | (
        {"appointment_date"} if appointment else {"cessation_date", "cessation_reason"}
    )
    violations = _object_violations(officer, path=path, keys=keys, required=frozenset(required))
    if not isinstance(officer, Mapping):
        return violations
    for key in sorted(set(officer) & _OFFICER_IDENTITY_KEYS):
        violations.extend(
            text_violations(officer[key], path=f"{path}.{key}", max_chars=MAX_TEXT_CHARS)
        )
    if not any(
        isinstance(officer.get(key), str) and str(officer[key]).strip()
        for key in _OFFICER_IDENTITY_KEYS
    ):
        violations.append(f"{path}: needs one of {', '.join(sorted(_OFFICER_IDENTITY_KEYS))}")
    if officer.get("other_names_en") is not None and officer.get("surname_en") is None:
        violations.append(f"{path}.other_names_en: requires surname_en (the form's name split)")
    violations.extend(_role_violations(officer, path=path, event_type=event_type))
    violations.extend(
        _capacity_violations(officer, path=path, role=_officer_role(officer.get("role")))
    )
    for key in ("previous_names", "aliases"):
        if key in officer:
            violations.extend(name_history_violations(officer[key], path=f"{path}.{key}"))
    for key in (
        "correspondence_address",
        "registered_office_address",
        "usual_residential_address",
    ):
        if key in officer:
            violations.extend(address_violations(officer[key], path=f"{path}.{key}"))
    if "email" in officer:
        violations.extend(_email_violations(officer["email"], path=f"{path}.email"))
    for key in (
        "br_number",
        "hkid_partial",
        "passport_partial",
        "passport_issuing_country_region",
        "hkid_full",
        "hkid_check_digit",
        "passport_full",
    ):
        if key in officer:
            violations.extend(
                text_violations(officer[key], path=f"{path}.{key}", max_chars=MAX_ID_CHARS)
            )
    if "tcsp_licence" in officer:
        violations.extend(
            _tcsp_licence_violations(officer["tcsp_licence"], path=f"{path}.tcsp_licence")
        )
    if "appointment_date" in officer:
        violations.extend(
            _date_violations(officer["appointment_date"], path=f"{path}.appointment_date")
        )
    if "cessation_date" in officer:
        violations.extend(
            _date_violations(officer["cessation_date"], path=f"{path}.cessation_date")
        )
    if "cessation_reason" in officer:
        violations.extend(
            _enum_violations(
                officer["cessation_reason"],
                path=f"{path}.cessation_reason",
                vocabulary=CessationReason,
            )
        )
    for key in ("existing_director_at_appointment", "continues_office_after_cessation"):
        if key in officer:
            violations.extend(_bool_violations(officer[key], path=f"{path}.{key}"))
    return violations


def payload_violations(*, event_type: ChangeEventType, payload: object) -> list[str]:
    """Every place a payload leaves the grammar for this event type.

    An empty list means accepted. The shapes are the contract's
    ``change_event.*`` vocabulary (see the module docstring); a type whose
    shape is reserved (``company_name_change``) has none, so this returns no
    violation and the reservation is ``event_violations``'s to report. The §1
    protected class is refused **first**: a payload carrying it is refused for
    that reason whether or not the rest of it fits, so the field the caller
    must drop is the first thing the answer names.
    """
    expected = _PAYLOAD_KEY_BY_TYPE.get(event_type)
    if expected is None:
        return []
    if not isinstance(payload, Mapping):
        return ["payload: not an object"]
    protected = _protected_class_violations(payload)
    if protected:
        return protected
    size_violations = _size_violations(payload)
    if size_violations:
        return size_violations
    if event_type is ChangeEventType.OTHER:
        violations = _object_violations(
            payload,
            path="payload",
            keys=frozenset({"summary"}),
            required=frozenset({"summary"}),
        )
        summary = payload.get("summary")
        violations.extend(
            text_violations(summary, path="payload.summary", max_chars=MAX_SUMMARY_CHARS)
        )
        if isinstance(summary, str) and "\n" in summary:
            violations.append("payload.summary: must be a single line")
        return violations
    violations = _object_violations(
        payload, path="payload", keys=frozenset({expected}), required=frozenset({expected})
    )
    body = payload.get(expected)
    if not isinstance(body, Mapping):
        violations.append(f"payload.{expected}: not an object")
        return violations
    if event_type is ChangeEventType.REGISTERED_OFFICE_CHANGE:
        violations.extend(
            _object_violations(
                body,
                path=f"payload.{expected}",
                keys=frozenset({"address"}),
                required=frozenset({"address"}),
            )
        )
        if "address" in body:
            violations.extend(
                address_violations(body["address"], path=f"payload.{expected}.address")
            )
        return violations
    violations.extend(
        _object_violations(
            body,
            path=f"payload.{expected}",
            keys=frozenset({"officer"}),
            required=frozenset({"officer"}),
        )
    )
    if "officer" not in body:
        return violations
    officer = body["officer"]
    if isinstance(officer, Mapping) and "continuation_sheet_count" in officer:
        # Named for what it is: contract §3 removed the count from the payload
        # vocabulary because it is derived at package assembly from the number
        # of additional officers. An unknown-key message would be true and
        # unhelpful.
        violations.append(
            "payload.officer.continuation_sheet_count: derived at package assembly, never payload"
        )
    violations.extend(
        _officer_violations(
            officer,
            path=f"payload.{expected}.officer",
            event_type=event_type,
            appointment=event_type
            in (ChangeEventType.DIRECTOR_APPOINTMENT, ChangeEventType.SECRETARY_APPOINTMENT),
        )
    )
    return violations


def _size_violations(payload: Mapping[str, Any]) -> list[str]:
    """The payload must be storable JSON, and not a document store.

    Serializing it here also catches what a JSONB column would refuse much
    later and much less clearly — a ``date`` object, a set, a stray model.
    """
    try:
        serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        return ["payload: not JSON-serializable"]
    if len(serialized.encode("utf-8")) > MAX_PAYLOAD_BYTES:
        return [f"payload: larger than {MAX_PAYLOAD_BYTES} bytes"]
    return []


def event_violations(
    *,
    event_type: ChangeEventType,
    effective_date: date,
    informed_date: date | None,
    payload: object,
) -> list[str]:
    """Every place an event entry (or an edit) leaves the rules; empty is accepted.

    Three things, one list, because the caller's remedy is one request: the
    type is enterable in v1.0 (``company_name_change`` is not), §2.6's sanity
    rule holds (``informed_date >= effective_date`` — equal is legal, and an
    absent informed date is not a violation), and the payload fits its type's
    grammar.
    """
    violations: list[str] = []
    if is_reserved(event_type):
        violations.append(
            f"type: {event_type.value} is reserved for Wave 2/NNC2 — not enterable in v1.0"
        )
    if informed_date is not None and informed_date < effective_date:
        violations.append(
            "informed_date: before effective_date "
            "(contract §2.6 requires informed_date >= effective_date)"
        )
    violations.extend(payload_violations(event_type=event_type, payload=payload))
    return violations


__all__ = [
    "ACTION_PERMISSIONS",
    "ADDRESS_KEYS",
    "ADDRESS_REQUIRED_KEYS",
    "AWAITING_REVIEW_STATUSES",
    "CHANGE_EVENT_TRANSITIONS",
    "EDIT_FIELDS",
    "MAX_CAPACITIES",
    "MAX_PAYLOAD_BYTES",
    "NAME_HISTORY_KEYS",
    "OFFICE_CAPACITIES",
    "PROTECTED_CLASS_FIELDS",
    "PROTECTED_CLASS_UNAVAILABLE",
    "RESERVED_TYPES",
    "TYPE_ROLES",
    "UNWIRED_REASONS",
    "WIRED_ACTIONS",
    "Address",
    "AllowedAction",
    "CessationReason",
    "ChangeEventAction",
    "ChangeEventRelation",
    "ChangeEventSourceType",
    "ChangeEventStatus",
    "ChangeEventType",
    "OfficerCapacity",
    "OfficerRole",
    "address_violations",
    "admits",
    "allowed_actions",
    "alternate_to_required",
    "event_violations",
    "is_awaiting_review",
    "is_reserved",
    "is_wired",
    "name_history_violations",
    "payload_violations",
    "permission_for",
    "protected_class_fields",
    "rationale_required",
    "text_violations",
    "unwired_reason",
]
