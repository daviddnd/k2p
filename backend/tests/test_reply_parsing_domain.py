"""Reply-parsing domain tests (scope §2.5, §7; data-model §2.7; the M5b slice).

Pure rules, no I/O: the closed grammar transcribed from
``research/llm/prompt-prototypes/schemas/reply-parse.schema.json`` (acceptance
and refusal — unknown keys and wrong enums included), the deterministic
post-checks (verbatim ``evidence_span`` with the ``INFERRED:`` convention,
checklist ``fact_id`` membership, ISO dates), the field-scoring and routing
rules (the no-guessing rule for grammar-incomplete proposals included), the
payload-proposal mapping onto the ``change_event.*`` vocabulary, and the
withdrawal/correction matching against earlier events. The pipeline that calls
a model and lands proposals lives in ``test_reply_parsing_scenarios_db.py``.
"""

from uuid import UUID, uuid4

from app.domain.change_events import ChangeEventStatus, ChangeEventType
from app.domain.confidence import ConfidenceBand
from app.domain.reply_parsing import (
    ENTERABLE_EVENT_TYPES,
    INVALID_DATE_PENALTY,
    PARSE_LITERALS,
    SPAN_VIOLATION_PENALTY,
    UNCERTAIN_PENALTY,
    UNKNOWN_FACT_ID_PENALTY,
    ConfirmationAnswer,
    EarlierEvent,
    MentionKind,
    ReplyLanguage,
    ReplyStatus,
    field_scores,
    match_earlier_event,
    parse_violations,
    payload_proposal,
    proposal_violations,
    route_parse,
    span_is_verbatim,
    unregistered_person_names,
)

REPLY = (
    "Dear team, the registered office is still Unit 5, 1 Kai Chuen Road, Kowloon. "
    "Please note that Alice Chan resigned as director on 15 March 2026. "
    "We have no other changes. Regards, Bob"
)

CHECKLIST = frozenset({"company.registered_office", "officers.directors"})


# --- The vocabularies are the schema's and the change-event grammar's -------


def test_the_status_vocabulary_is_the_schemas_six() -> None:
    """Pinned as strings, in the schema's order: a rename cannot quietly leave
    the grammar accepting a status the pipeline no longer understands."""
    assert [status.value for status in ReplyStatus] == [
        "confirmed_no_change",
        "changes_reported",
        "correction",
        "withdrawal",
        "unclear",
        "off_topic",
    ]


def test_the_language_answer_and_mention_vocabularies() -> None:
    assert [language.value for language in ReplyLanguage] == ["en", "zh-hk", "mixed"]
    assert [answer.value for answer in ConfirmationAnswer] == [
        "confirmed",
        "denied",
        "not_addressed",
    ]
    assert [kind.value for kind in MentionKind] == ["name_en", "name_zh", "brn", "other"]


def test_the_event_type_vocabulary_is_the_enterable_subset() -> None:
    """The schema's enum is the *enterable* ``ChangeEventType``s only:
    ``company_name_change`` is reserved for Wave 2/NNC2 and must not appear in
    a parse proposal."""
    assert {event_type.value for event_type in ENTERABLE_EVENT_TYPES} == {
        "director_appointment",
        "director_cessation",
        "secretary_appointment",
        "secretary_cessation",
        "registered_office_change",
        "other",
    }
    assert ChangeEventType.COMPANY_NAME_CHANGE not in ENTERABLE_EVENT_TYPES


def test_the_allowed_literals_are_the_grammars_closed_enums() -> None:
    """Task 7's ``allowed_literals`` for this task, pinned: the union of the
    answer grammar's closed vocabularies and nothing wider. The gateway's
    strict restore admits a declared literal; every other value the answer
    returns must be a token from this call's table, and a value literal it
    never saw is refused."""
    assert (
        {member.value for member in ReplyStatus}
        | {member.value for member in ReplyLanguage}
        | {member.value for member in ConfirmationAnswer}
        | {member.value for member in MentionKind}
        | {member.value for member in ENTERABLE_EVENT_TYPES}
    ) == PARSE_LITERALS
    assert "A123456(7)" not in PARSE_LITERALS


# --- The grammar: acceptance and refusal -------------------------------------


def _confirmation(
    *,
    fact_id: str = "company.registered_office",
    answer: str = "confirmed",
    span: str = "registered office",
    uncertain: bool = False,
) -> dict:
    return {
        "fact_id": fact_id,
        "answer": answer,
        "evidence_span": span,
        "uncertain": uncertain,
    }


def _cessation_event(
    *,
    effective_date: str | None = "2026-03-15",
    reason: str | None = "resignation_others",
    span: str = "resigned as director",
    party_span: str = "Alice Chan",
    uncertain: bool = False,
    party_uncertain: bool = False,
) -> dict:
    return {
        "type": "director_cessation",
        "party": {
            "name_as_written": "Alice Chan",
            "name_en": "Alice Chan",
            "name_zh": None,
            "partial_id": None,
            "evidence_span": party_span,
            "uncertain": party_uncertain,
        },
        "effective_date": effective_date,
        "effective_date_text": "15 March 2026",
        "informed_date": None,
        "new_values": {} if reason is None else {"cessation_reason": reason},
        "evidence_span": span,
        "uncertain": uncertain,
        "notes": None,
    }


def _parse(
    *,
    status: str = "confirmed_no_change",
    confirmations: list | None = None,
    change_events: list | None = None,
) -> dict:
    return {
        "status": status,
        "language": "en",
        "confirmations": confirmations or [],
        "change_events": change_events or [],
        "multi_company_signals": {"company_mentions": []},
    }


def test_the_minimal_parse_passes_the_grammar() -> None:
    assert parse_violations(_parse()) == []
    assert parse_violations(
        _parse(
            status="changes_reported",
            confirmations=[_confirmation()],
            change_events=[_cessation_event()],
        )
    ) == []


def test_a_non_object_parse_is_refused() -> None:
    assert parse_violations(["not", "an", "object"]) != []


def test_unknown_keys_are_refused_at_every_level() -> None:
    raw = _parse()
    raw["surprise"] = True
    assert any("surprise" in violation for violation in parse_violations(raw))

    confirmation = _confirmation()
    confirmation["extra"] = 1
    violations = parse_violations(_parse(confirmations=[confirmation]))
    assert any("confirmations[0].extra" in violation for violation in violations)

    event = _cessation_event()
    event["party"]["extra"] = 1
    violations = parse_violations(_parse(change_events=[event]))
    assert any("change_events[0].party.extra" in violation for violation in violations)


def test_missing_required_keys_are_refused() -> None:
    raw = _parse()
    del raw["multi_company_signals"]
    assert any("multi_company_signals" in violation for violation in parse_violations(raw))

    confirmation = _confirmation()
    del confirmation["uncertain"]
    violations = parse_violations(_parse(confirmations=[confirmation]))
    assert any("confirmations[0].uncertain" in violation for violation in violations)


def test_wrong_enums_are_refused() -> None:
    raw = _parse(status="sort_of_confused")
    assert any("status" in violation for violation in parse_violations(raw))

    raw = _parse()
    raw["language"] = "fr"
    assert any("language" in violation for violation in parse_violations(raw))

    confirmation = _confirmation(answer="maybe")
    violations = parse_violations(_parse(confirmations=[confirmation]))
    assert any("answer" in violation for violation in violations)

    event = _cessation_event()
    event["type"] = "company_name_change"
    assert any("type" in violation for violation in parse_violations(_parse(change_events=[event])))

    signals = {"company_mentions": [{"text": "Acme", "kind": "nickname"}]}
    raw = _parse()
    raw["multi_company_signals"] = signals
    assert any("kind" in violation for violation in parse_violations(raw))


def test_uncertain_must_be_a_real_boolean_not_a_truthy_value() -> None:
    confirmation = _confirmation(uncertain="yes")  # type: ignore[dict-item]
    violations = parse_violations(_parse(confirmations=[confirmation]))
    assert any("uncertain" in violation for violation in violations)
    confirmation = _confirmation(uncertain=1)  # type: ignore[dict-item]
    violations = parse_violations(_parse(confirmations=[confirmation]))
    assert any("uncertain" in violation for violation in violations)


def test_the_date_shape_and_the_calendar_check_are_different_layers() -> None:
    """The grammar checks the ``YYYY-MM-DD`` *shape*; whether the date is a
    real calendar day is the ISO post-check's job (scoring)."""
    event = _cessation_event(effective_date="2026-3-5")
    assert any("effective_date" in v for v in parse_violations(_parse(change_events=[event])))

    event = _cessation_event(effective_date="2026-13-45")
    assert parse_violations(_parse(change_events=[event])) == []


def test_new_values_admits_only_strings_and_nulls() -> None:
    event = _cessation_event()
    event["new_values"] = {"cessation_reason": ["a", "b"]}
    violations = parse_violations(_parse(change_events=[event]))
    assert any("new_values.cessation_reason" in violation for violation in violations)


# --- The span post-check and the INFERRED: convention -------------------------


def test_a_verbatim_span_passes() -> None:
    assert span_is_verbatim("registered office", REPLY)


def test_a_fabricated_span_fails() -> None:
    assert not span_is_verbatim("the moon office", REPLY)


def test_the_span_check_is_case_sensitive() -> None:
    """Verbatim means the reply's own characters — a case-shifted span is not
    in the text and has no business passing as one."""
    assert not span_is_verbatim("Registered Office", REPLY)


def test_an_empty_span_proves_nothing() -> None:
    assert not span_is_verbatim("", REPLY)


def test_the_inferred_prefix_accepts_a_verbatim_remainder() -> None:
    """The documented normalization: a span that is not itself verbatim is
    acceptable only as ``INFERRED:`` + a remainder that *is* verbatim."""
    assert span_is_verbatim("INFERRED: registered office", REPLY)
    assert span_is_verbatim("INFERRED:registered office", REPLY)


def test_the_inferred_prefix_cannot_launder_a_fabrication() -> None:
    assert not span_is_verbatim("INFERRED: the moon office", REPLY)
    assert not span_is_verbatim("inferred: registered office", REPLY)  # prefix is exact


# --- Field scoring ------------------------------------------------------------


def test_clean_confirmation_scores_full_marks() -> None:
    scores = field_scores(
        parse=_parse(confirmations=[_confirmation()]),
        reply_text=REPLY,
        checklist_fact_ids=CHECKLIST,
    )
    assert scores == {"confirmations.company.registered_office": 1.0}


def test_the_scoring_table_is_the_named_constants() -> None:
    assert UNCERTAIN_PENALTY == 0.3
    assert SPAN_VIOLATION_PENALTY == 0.4
    assert UNKNOWN_FACT_ID_PENALTY == 0.5
    assert INVALID_DATE_PENALTY == 0.3


def test_the_uncertain_flag_downgrades_by_the_named_constant() -> None:
    scores = field_scores(
        parse=_parse(confirmations=[_confirmation(uncertain=True)]),
        reply_text=REPLY,
        checklist_fact_ids=CHECKLIST,
    )
    assert scores["confirmations.company.registered_office"] == 1.0 - UNCERTAIN_PENALTY


def test_a_fabricated_span_downgrades_the_field() -> None:
    scores = field_scores(
        parse=_parse(confirmations=[_confirmation(span="the moon office")]),
        reply_text=REPLY,
        checklist_fact_ids=CHECKLIST,
    )
    assert scores["confirmations.company.registered_office"] == 1.0 - SPAN_VIOLATION_PENALTY


def test_a_fact_id_outside_the_checklist_downgrades_the_field() -> None:
    scores = field_scores(
        parse=_parse(confirmations=[_confirmation(fact_id="company.moon_base")]),
        reply_text=REPLY,
        checklist_fact_ids=CHECKLIST,
    )
    assert scores["confirmations.company.moon_base"] == 1.0 - UNKNOWN_FACT_ID_PENALTY


def test_penalties_stack_and_floor_at_zero() -> None:
    confirmation = _confirmation(fact_id="company.moon_base", span="the moon", uncertain=True)
    scores = field_scores(
        parse=_parse(confirmations=[confirmation]),
        reply_text=REPLY,
        checklist_fact_ids=CHECKLIST,
    )
    assert scores["confirmations.company.moon_base"] == 0.0


def test_event_scoring_counts_party_flags_spans_and_iso_dates() -> None:
    event = _cessation_event(party_uncertain=True, effective_date="2026-13-45")
    scores = field_scores(
        parse=_parse(change_events=[event]),
        reply_text=REPLY,
        checklist_fact_ids=CHECKLIST,
    )
    # one party uncertain flag and one calendar-invalid date; every span verbatim.
    assert scores["change_events.0"] == 1.0 - UNCERTAIN_PENALTY - INVALID_DATE_PENALTY


def test_a_shape_valid_but_calendar_invalid_date_downgrades() -> None:
    """``2026-13-45`` passed the grammar's pattern — the post-check catches it."""
    event = _cessation_event(effective_date="2026-13-45")
    scores = field_scores(
        parse=_parse(change_events=[event]),
        reply_text=REPLY,
        checklist_fact_ids=CHECKLIST,
    )
    assert scores["change_events.0"] == 1.0 - INVALID_DATE_PENALTY


# --- Routing: the bands and the no-guessing rule ------------------------------

# The register the routing tests hand the band rule: the person the fixture
# proposals name, as the company's appointments would carry them. A parse's
# band decision always reads one (ruling R-D), so every call site states it —
# a test that forgot would be a caller bug, not a default.
REGISTER = ("Alice Chan",)


def test_a_clean_parse_routes_auto_accept() -> None:
    parse = _parse(confirmations=[_confirmation()], change_events=[_cessation_event()])
    scores = field_scores(parse=parse, reply_text=REPLY, checklist_fact_ids=CHECKLIST)
    assert route_parse(
        scores=scores, change_events=parse["change_events"], registered_names=REGISTER
    ) is (ConfidenceBand.AUTO_ACCEPT)


def test_the_overall_score_is_the_weakest_field() -> None:
    """One uncertain confirmation beside a clean event: the min rules and the
    whole parse routes to staff review."""
    parse = _parse(
        confirmations=[_confirmation(uncertain=True)],
        change_events=[_cessation_event()],
    )
    scores = field_scores(parse=parse, reply_text=REPLY, checklist_fact_ids=CHECKLIST)
    assert route_parse(
        scores=scores, change_events=parse["change_events"], registered_names=REGISTER
    ) is (ConfidenceBand.STAFF_REVIEW)


def test_a_weak_field_routes_client_clarification() -> None:
    parse = _parse(confirmations=[_confirmation(fact_id="company.moon_base", span="the moon")])
    scores = field_scores(parse=parse, reply_text=REPLY, checklist_fact_ids=CHECKLIST)
    assert (
        route_parse(scores=scores, change_events=[], registered_names=REGISTER)
        is ConfidenceBand.CLIENT_CLARIFICATION
    )


def test_an_empty_parse_has_no_routing() -> None:
    assert route_parse(scores={}, change_events=[], registered_names=REGISTER) is None


def test_a_grammar_incomplete_event_is_never_auto_accepted() -> None:
    """The no-guessing rule: the model is fully certain, every span is
    verbatim — but the client never gave a cessation reason or a parseable
    date, so the proposal cannot satisfy the payload grammar without guessing.
    The parse routes to clarification regardless of the perfect score."""
    event = _cessation_event(effective_date=None, reason=None)
    parse = _parse(status="changes_reported", change_events=[event])
    scores = field_scores(parse=parse, reply_text=REPLY, checklist_fact_ids=CHECKLIST)
    assert scores["change_events.0"] == 1.0
    assert route_parse(
        scores=scores, change_events=parse["change_events"], registered_names=REGISTER
    ) is (ConfidenceBand.CLIENT_CLARIFICATION)


def test_the_no_guessing_rule_overrides_even_a_staff_review_score() -> None:
    event = _cessation_event(effective_date=None, reason=None, uncertain=True)
    parse = _parse(status="changes_reported", change_events=[event])
    scores = field_scores(parse=parse, reply_text=REPLY, checklist_fact_ids=CHECKLIST)
    assert scores["change_events.0"] == 1.0 - UNCERTAIN_PENALTY  # staff_review band alone
    assert route_parse(
        scores=scores, change_events=parse["change_events"], registered_names=REGISTER
    ) is (ConfidenceBand.CLIENT_CLARIFICATION)


# --- R-D: a person off the register is never auto-accepted ---------------------


def _person_event(
    *,
    name_en: str | None = None,
    name_as_written: str | None = None,
    name_zh: str | None = None,
    partial_id: str | None = None,
    event_type: str = "director_cessation",
) -> dict:
    """One payload-complete proposal naming a person — the R-D fixture.

    ``director_cessation`` with a reason, so the no-guessing rule is silent and
    the only thing that can move the band is the register rule under test.
    """
    event = _cessation_event()
    event["type"] = event_type
    event["party"] = {
        "name_as_written": name_as_written or name_en,
        "name_en": name_en,
        "name_zh": name_zh,
        "partial_id": partial_id,
        "evidence_span": "Alice Chan",
        "uncertain": False,
    }
    return event


def test_a_person_absent_from_the_register_cannot_auto_accept() -> None:
    """R-D's rule, at the band: every field scores 1.0 — verbatim spans, a
    nameable cessation reason, a real calendar day — and the proposal's own
    completeness is not in question. The parse would auto-accept on score; it
    names a person the company's appointments do not carry, so it routes
    ``staff_review`` instead. A name nobody in the company's own records has
    ever held is a fact only a person may accept."""
    event = _person_event(name_en="Wong New Director")
    parse = _parse(status="changes_reported", change_events=[event])
    scores = field_scores(parse=parse, reply_text=REPLY, checklist_fact_ids=CHECKLIST)
    assert all(score >= 0.85 for score in scores.values())
    assert proposal_violations(event=event) == []
    assert (
        route_parse(scores=scores, change_events=parse["change_events"], registered_names=())
        is ConfidenceBand.STAFF_REVIEW
    )


def test_a_person_on_the_register_still_auto_accepts() -> None:
    """The control that keeps R-D honest: the same scores, the same complete
    proposal, the same person — and the register carries them, so nothing
    bends. A rule that demoted this would make every ordinary cessation wait
    for a human."""
    event = _person_event(name_en="Alice Chan")
    parse = _parse(status="changes_reported", change_events=[event])
    scores = field_scores(parse=parse, reply_text=REPLY, checklist_fact_ids=CHECKLIST)
    assert route_parse(
        scores=scores, change_events=parse["change_events"], registered_names=("Alice Chan",)
    ) is (ConfidenceBand.AUTO_ACCEPT)


def test_the_register_rule_is_a_cap_and_never_an_upgrade() -> None:
    """Both other bends keep their place: an unregistered person beside a
    field the score already put at ``staff_review`` stays there (the cap
    changes only an ``auto_accept`` band), and one beside a proposal the
    no-guessing rule refuses stays at ``client_clarification`` — the strictest
    of the three wins, so the rule can only ever take auto-accept away."""
    weak = _parse(
        confirmations=[_confirmation(uncertain=True)],
        change_events=[_person_event(name_en="Wong New Director")],
    )
    scores = field_scores(parse=weak, reply_text=REPLY, checklist_fact_ids=CHECKLIST)
    assert route_parse(
        scores=scores, change_events=weak["change_events"], registered_names=()
    ) is ConfidenceBand.STAFF_REVIEW

    uncompletable = _parse(
        status="changes_reported",
        change_events=[_person_event(name_en="Wong New Director") | {"effective_date": None}],
    )
    uncompletable["change_events"][0]["new_values"] = {}
    scores = field_scores(parse=uncompletable, reply_text=REPLY, checklist_fact_ids=CHECKLIST)
    assert route_parse(
        scores=scores, change_events=uncompletable["change_events"], registered_names=()
    ) is ConfidenceBand.CLIENT_CLARIFICATION


def test_the_register_comparison_is_exact_after_normalization() -> None:
    """Ruling R-T4a: the comparison form folds width, case, whitespace and the
    bounded script variants — ``陈大文`` against a register's ``陳大文`` is the
    registered person, and so is a re-spaced, case-shifted ``CHAN  tai man`` —
    and nothing else counts. The tokenizer's one-edit radius is deliberately
    **not** shared with this gate: R-D denies by default, so a one-edit variant
    (``張嘉新`` for ``張嘉欣``, ``Chan Tay Man`` for ``Chan Tai Man``, a short
    name's near miss) is a different person and routes ``staff_review`` — which
    is the fail-safe direction here, and the reason a register name is never
    guessed at."""
    cases = (
        ("Chan Tai Man", ("Chan Tai Man",), ConfidenceBand.AUTO_ACCEPT),
        ("CHAN  tai man", ("Chan Tai Man",), ConfidenceBand.AUTO_ACCEPT),
        ("陈大文", ("陳大文",), ConfidenceBand.AUTO_ACCEPT),
        ("張嘉新", ("張嘉欣",), ConfidenceBand.STAFF_REVIEW),
        ("Chan Tay Man", ("Chan Tai Man",), ConfidenceBand.STAFF_REVIEW),
        ("Chen", ("Chan",), ConfidenceBand.STAFF_REVIEW),
    )
    for proposed, register, expected in cases:
        assert route_parse(
            scores={"change_events.0": 1.0},
            change_events=[_person_event(name_en=proposed)],
            registered_names=register,
        ) is expected, (proposed, register)


def test_unregistered_person_names_reads_every_name_form_and_never_the_identifier() -> None:
    """The rule's own home, read by the band rule and by the landing that
    records the reason: every name form the party carries is compared on its
    own, so one known form beside one the register lacks still names a
    stranger — and ``partial_id`` is not a name, so an identifier the register
    has never seen cannot masquerade as a person's absence. Empty means every
    named person is on the register."""
    on_register = _person_event(name_en="Alice Chan", name_zh="陳大文", partial_id="Z9999")
    assert (
        unregistered_person_names(
            change_events=[on_register], registered_names=("Alice Chan", "陳大文")
        )
        == ()
    )

    # The register knows her English name and not the Chinese one: the
    # comparison is per form, so the run is held for a person to read — and
    # the identifier beside them is not consulted either way.
    assert unregistered_person_names(
        change_events=[on_register], registered_names=("Alice Chan",)
    ) == ("陳大文",)

    stranger = _person_event(name_as_written="Ms Wong")
    assert unregistered_person_names(change_events=[stranger], registered_names=()) == ("Ms Wong",)


def _appointment_event(
    *,
    name_en: str | None = None,
    name_zh: str | None = None,
    name_as_written: str | None = None,
    new_values: dict | None = None,
) -> dict:
    """One payload-complete appointment, with ``new_values`` as the officer
    block's free-form additions — the review finding's channel.

    The appointment shape is the one that accepts *every* name-bearing officer
    key (``_APPOINTMENT_OFFICER_KEYS`` carries the name-history lists a
    cessation's block does not), so the channel tests ride it.
    """
    return {
        "type": "director_appointment",
        "party": {
            "name_as_written": name_as_written or name_en or "the appointing resolution",
            "name_en": name_en,
            "name_zh": name_zh,
            "partial_id": None,
            "evidence_span": "Alice Chan",
            "uncertain": False,
        },
        "effective_date": "2026-05-01",
        "effective_date_text": "1 May 2026",
        "informed_date": None,
        "new_values": {} if new_values is None else new_values,
        "evidence_span": "appointed",
        "uncertain": False,
        "notes": None,
    }


def test_a_stranger_named_only_in_new_values_name_en_is_not_auto_accepted() -> None:
    """The review finding's channel: ``payload_proposal`` copies ``new_values``
    into the officer block verbatim, and fills the name from the party only
    when the officer key is still empty — so a stranger written in
    ``new_values.name_en`` *is* the name the event lands, and a party naming a
    registered person must not launder it. The party-only read auto-accepted
    exactly this parse."""
    event = _appointment_event(name_en="Alice Chan", new_values={"name_en": "Wong New Director"})
    assert proposal_violations(event=event) == []
    # The payload really carries the stranger: this is the bypass, not a
    # hypothetical.
    landed = payload_proposal(event=event)["appointment"]["officer"]
    assert landed["name_en"] == "Wong New Director"
    assert route_parse(
        scores={"change_events.0": 1.0},
        change_events=[event],
        registered_names=("Alice Chan",),
    ) is ConfidenceBand.STAFF_REVIEW


def test_a_stranger_named_only_in_new_values_name_zh_is_not_auto_accepted() -> None:
    """The same channel in the other script, and the shape the recorded model
    run emits: ``research/llm/eval-harness/results/run-20261001T234452Z.json``
    ``replies[16]`` (R17) writes ``new_values: {"name_zh": ...}``."""
    event = _appointment_event(name_en="Alice Chan", new_values={"name_zh": "吳美玲"})
    assert proposal_violations(event=event) == []
    assert payload_proposal(event=event)["appointment"]["officer"]["name_zh"] == "吳美玲"
    assert route_parse(
        scores={"change_events.0": 1.0},
        change_events=[event],
        registered_names=("Alice Chan",),
    ) is ConfidenceBand.STAFF_REVIEW


def test_a_stranger_named_only_by_the_split_new_values_pair_is_not_auto_accepted() -> None:
    """R-T4b's guarantee, kept: a stranger written as a split pair is joined
    (``surname_en`` + ``other_names_en`` → ``Wong New Director``, the register's
    own join) and that joined form is off-register, so it still routes
    ``staff_review``. The party's own forms name a registered person and must
    not launder the second name the payload carries."""
    event = _appointment_event(
        name_as_written="Alice Chan",
        new_values={"surname_en": "Wong", "other_names_en": "New Director"},
    )
    assert proposal_violations(event=event) == []
    landed = payload_proposal(event=event)["appointment"]["officer"]
    assert (landed["surname_en"], landed["other_names_en"]) == ("Wong", "New Director")
    assert unregistered_person_names(
        change_events=[event], registered_names=("Alice Chan", "Chan Tai Man")
    ) == ("Wong New Director",)
    assert route_parse(
        scores={"change_events.0": 1.0},
        change_events=[event],
        registered_names=("Alice Chan",),
    ) is ConfidenceBand.STAFF_REVIEW


def test_the_split_pair_of_a_registered_person_auto_accepts() -> None:
    """R-T4b's control, and the over-demotion it fixes: the ND2A officer grammar
    *expects* an English name split into ``surname_en`` + ``other_names_en``,
    while the register holds the joined form ``_person_names`` builds. The pair
    joins to ``Chan Tai Man`` — the register entry — so the appointment
    auto-accepts; reading the fragments as two people demoted every correctly
    split appointment of a known officer."""
    event = _appointment_event(
        name_as_written="Chan Tai Man",
        new_values={"surname_en": "Chan", "other_names_en": "Tai Man"},
    )
    assert proposal_violations(event=event) == []
    assert unregistered_person_names(
        change_events=[event], registered_names=("Chan Tai Man",)
    ) == ()
    assert route_parse(
        scores={"change_events.0": 1.0},
        change_events=[event],
        registered_names=("Chan Tai Man",),
    ) is ConfidenceBand.AUTO_ACCEPT


def test_half_a_split_pair_is_still_a_name() -> None:
    """Half a pair is not a pair: with only one part stated there is nothing to
    join, so the part that *is* there is the only name this channel carries and
    it is compared as one — a lone fragment the register does not hold cannot
    slip through by being unjoinable. (A *blank* second part never reaches this
    rule: the officer grammar refuses ``other_names_en: must not be blank``, so
    the no-guessing bend takes that parse to ``client_clarification`` first.)"""
    event = _appointment_event(
        name_as_written="Alice Chan",
        new_values={"surname_en": "Wong"},
    )
    assert proposal_violations(event=event) == []
    assert unregistered_person_names(
        change_events=[event], registered_names=("Alice Chan",)
    ) == ("Wong",)
    assert route_parse(
        scores={"change_events.0": 1.0},
        change_events=[event],
        registered_names=("Alice Chan",),
    ) is ConfidenceBand.STAFF_REVIEW


def test_new_values_naming_the_registered_person_still_auto_accepts() -> None:
    """The channel walk's control: the same shape, the same register, a name
    the register knows — nothing bends. The fix scans a channel; it does not
    demote every proposal that fills one."""
    event = _appointment_event(name_en="Alice Chan", new_values={"name_en": "Alice Chan"})
    assert proposal_violations(event=event) == []
    assert route_parse(
        scores={"change_events.0": 1.0},
        change_events=[event],
        registered_names=("Alice Chan",),
    ) is ConfidenceBand.AUTO_ACCEPT


def test_every_name_bearing_new_values_key_is_a_channel() -> None:
    """The rest of the officer grammar's name-bearing keys — the
    alternate-director text box and the ``{name_zh, name_en}`` name-history
    entries — are channels too: ``new_values`` lands whatever the officer
    grammar accepts, so a stranger must not enter through a box the reader
    never opened."""
    for new_values in (
        {"alternate_to": "Wong New Director", "capacity": ["alternate_director"]},
        {"aliases": [{"name_en": "Wong New Director"}]},
        {"previous_names": [{"name_zh": "吳美玲"}]},
    ):
        event = _appointment_event(name_en="Alice Chan", new_values=new_values)
        assert proposal_violations(event=event) == [], new_values
        assert route_parse(
            scores={"change_events.0": 1.0},
            change_events=[event],
            registered_names=("Alice Chan",),
        ) is ConfidenceBand.STAFF_REVIEW, new_values


def test_a_proposal_that_names_no_person_is_untouched() -> None:
    """The guardrail's third leg: R-D judges the officer types only. A
    registered-office move names an address and an ``other`` event a summary
    line, so an empty register — nobody on file at all — leaves their bands
    exactly where the scores put them."""
    office_move = {
        "type": "registered_office_change",
        "party": {
            "name_as_written": "Unit 5",
            "name_en": None,
            "name_zh": None,
            "partial_id": None,
            "evidence_span": "Unit 5",
            "uncertain": False,
        },
        "effective_date": "2026-05-01",
        "effective_date_text": "1 May 2026",
        "informed_date": None,
        "new_values": {
            "flat": "Unit 5",
            "building": "Kai Chuen Building",
            "street": "1 Kai Chuen Road",
            "district": "Kowloon",
        },
        "evidence_span": "moved to",
        "uncertain": False,
        "notes": None,
    }
    other = {
        "type": "other",
        "party": {
            "name_as_written": "the company",
            "name_en": None,
            "name_zh": None,
            "partial_id": None,
            "evidence_span": "company seal",
            "uncertain": False,
        },
        "effective_date": "2026-06-01",
        "effective_date_text": "1 June 2026",
        "informed_date": None,
        "new_values": {"summary": "Changed the company seal keeper."},
        "evidence_span": "company seal",
        "uncertain": False,
        "notes": None,
    }
    for event in (office_move, other):
        assert proposal_violations(event=event) == []
        assert route_parse(
            scores={"change_events.0": 1.0}, change_events=[event], registered_names=()
        ) is (ConfidenceBand.AUTO_ACCEPT)
        assert unregistered_person_names(change_events=[event], registered_names=()) == ()


# --- The payload-proposal mapping ---------------------------------------------


def test_the_appointment_proposal_maps_onto_the_appointment_payload() -> None:
    event = {
        "type": "secretary_appointment",
        "party": {
            "name_as_written": "Mary Wong",
            "name_en": "Mary Wong",
            "name_zh": "王瑪麗",
            "partial_id": None,
            "evidence_span": "Mary Wong",
            "uncertain": False,
        },
        "effective_date": "2026-04-01",
        "effective_date_text": "1 April 2026",
        "informed_date": None,
        "new_values": {"email": "mary@example.com"},
        "evidence_span": "appointed as company secretary",
        "uncertain": False,
        "notes": None,
    }
    assert payload_proposal(event=event) == {
        "appointment": {
            "officer": {
                "role": "company_secretary",
                "name_en": "Mary Wong",
                "name_zh": "王瑪麗",
                "email": "mary@example.com",
                "appointment_date": "2026-04-01",
            }
        }
    }
    assert proposal_violations(event=event) == []


def test_the_cessation_proposal_requires_the_reason_and_date() -> None:
    event = _cessation_event(reason=None)
    violations = proposal_violations(event=event)
    assert any("cessation_reason" in violation for violation in violations)

    event = _cessation_event(effective_date=None)
    assert any("effective_date" in violation for violation in proposal_violations(event=event))


def test_a_calendar_impossible_date_refuses_the_proposal_no_payload_check_catches() -> None:
    """The grammar checks the ISO *shape* only, and the scoring post-check
    downgrades without refusing — so the refusal lives here, the one home the
    router, the landing, and the letter all read. An office-change payload
    carries the address and no date the payload grammar could catch, and no
    payload check exists for ``informed_date`` at all: both shapes must bend
    to clarification instead of reaching ``date.fromisoformat`` and crashing
    the cycle."""
    office = {
        "type": "registered_office_change",
        "party": {
            "name_as_written": "Unit 5",
            "name_en": None,
            "name_zh": None,
            "partial_id": None,
            "evidence_span": "moved to",
            "uncertain": False,
        },
        "effective_date": "2026-02-30",
        "effective_date_text": "30 February 2026",
        "informed_date": None,
        "new_values": {
            "flat": "Unit 5",
            "building": "Kai Chuen Building",
            "street": "1 Kai Chuen Road",
            "district": "Kowloon",
        },
        "evidence_span": "moved to",
        "uncertain": False,
        "notes": None,
    }
    violations = proposal_violations(event=office)
    assert any(
        "effective_date" in violation and "calendar day" in violation
        for violation in violations
    )

    informed = _cessation_event()
    informed["informed_date"] = "2026-13-45"
    violations = proposal_violations(event=informed)
    assert any(
        "informed_date" in violation and "calendar day" in violation
        for violation in violations
    )

    # Real calendar dates still pass — the refusal names the impossible day,
    # never the shape.
    assert proposal_violations(event=_cessation_event()) == []


def test_a_withdrawal_reference_waives_the_filing_legs_but_not_the_calendar() -> None:
    """A withdrawal files nothing — the event it points at already exists —
    so the completeness legs (a null effective date, the payload grammar) are
    waived for the reference judgment; but a date the withdrawal *states*
    must still be a real calendar day, one home either way: the letter
    restates the proposal, and an impossible date must bend the reply to
    clarification instead of crashing the restatement."""
    reference = _cessation_event(effective_date=None)
    reference["new_values"] = {}
    assert proposal_violations(event=reference) != []
    assert proposal_violations(event=reference, reference_only=True) == []

    impossible = _cessation_event(effective_date="2026-02-30")
    violations = proposal_violations(event=impossible, reference_only=True)
    assert any(
        "effective_date" in violation and "calendar day" in violation
        for violation in violations
    )

    impossible_informed = _cessation_event()
    impossible_informed["informed_date"] = "2026-13-45"
    violations = proposal_violations(event=impossible_informed, reference_only=True)
    assert any(
        "informed_date" in violation and "calendar day" in violation
        for violation in violations
    )


def test_the_registered_office_proposal_maps_the_address_parts() -> None:
    event = {
        "type": "registered_office_change",
        "party": {
            "name_as_written": "Unit 5",
            "name_en": None,
            "name_zh": None,
            "partial_id": None,
            "evidence_span": "Unit 5",
            "uncertain": False,
        },
        "effective_date": "2026-05-01",
        "effective_date_text": "1 May 2026",
        "informed_date": None,
        "new_values": {
            "flat": "Unit 5",
            "building": "Kai Chuen Building",
            "street": "1 Kai Chuen Road",
            "district": "Kowloon",
        },
        "evidence_span": "moved to",
        "uncertain": False,
        "notes": None,
    }
    proposal = payload_proposal(event=event)
    assert proposal_violations(event=event) == []
    address = proposal["registered_office"]["address"]
    assert address["district"] == "Kowloon"


def test_an_incomplete_address_is_grammar_incomplete() -> None:
    event = {
        "type": "registered_office_change",
        "party": {
            "name_as_written": "Unit 5",
            "name_en": None,
            "name_zh": None,
            "partial_id": None,
            "evidence_span": "Unit 5",
            "uncertain": False,
        },
        "effective_date": "2026-05-01",
        "effective_date_text": "1 May 2026",
        "informed_date": None,
        "new_values": {"flat": "Unit 5", "street": "1 Kai Chuen Road"},
        "evidence_span": "moved to",
        "uncertain": False,
        "notes": None,
    }
    violations = proposal_violations(event=event)
    assert any("building" in violation for violation in violations)


def test_the_other_proposal_maps_the_summary_line() -> None:
    event = {
        "type": "other",
        "party": {
            "name_as_written": "the company",
            "name_en": None,
            "name_zh": None,
            "partial_id": None,
            "evidence_span": "the company",
            "uncertain": False,
        },
        "effective_date": "2026-06-01",
        "effective_date_text": "1 June 2026",
        "informed_date": None,
        "new_values": {"summary": "Changed the company seal keeper to the finance manager."},
        "evidence_span": "company seal",
        "uncertain": False,
        "notes": None,
    }
    assert payload_proposal(event=event) == {
        "summary": "Changed the company seal keeper to the finance manager."
    }
    assert proposal_violations(event=event) == []


def test_a_key_outside_the_payload_grammar_blocks_auto_acceptance() -> None:
    """A ``new_values`` key the grammar does not recognize is refused by the
    payload grammar itself — the proposal cannot be auto-accepted with an
    invention travelling in it; the named key tells staff what to drop."""
    event = _cessation_event()
    event["new_values"] = {"cessation_reason": "resignation_others", "moon_base": "yes"}
    assert any("moon_base" in violation for violation in proposal_violations(event=event))


def test_the_protected_class_is_refused_by_name() -> None:
    event = _cessation_event()
    event["new_values"] = {"cessation_reason": "resignation_others", "hkid_full": "A123456"}
    violations = proposal_violations(event=event)
    assert any("protected_class_unavailable" in violation for violation in violations)


# --- Withdrawal / correction matching ------------------------------------------


def _earlier(
    *,
    status: ChangeEventStatus = ChangeEventStatus.PENDING,
    party_names: tuple[str, ...] = ("Alice Chan",),
    event_type: ChangeEventType = ChangeEventType.DIRECTOR_CESSATION,
    event_id: UUID | None = None,
) -> EarlierEvent:
    return EarlierEvent(
        id=event_id or uuid4(),
        event_type=event_type,
        status=status,
        party_names=party_names,
    )


def test_a_withdrawal_matches_the_earlier_event_by_type_and_party() -> None:
    earlier = _earlier()
    assert match_earlier_event(event=_cessation_event(), earlier_events=[earlier]) == earlier


def test_a_submitted_event_cannot_be_withdrawn_or_corrected() -> None:
    assert (
        match_earlier_event(
            event=_cessation_event(),
            earlier_events=[_earlier(status=ChangeEventStatus.SUBMITTED)],
        )
        is None
    )


def test_the_terminal_statuses_are_not_matched() -> None:
    for status in (ChangeEventStatus.WITHDRAWN, ChangeEventStatus.DELETED):
        assert (
            match_earlier_event(
                event=_cessation_event(), earlier_events=[_earlier(status=status)]
            )
            is None
        )


def test_a_different_party_or_type_does_not_match() -> None:
    assert (
        match_earlier_event(
            event=_cessation_event(),
            earlier_events=[_earlier(party_names=("Bob Lee",))],
        )
        is None
    )
    assert (
        match_earlier_event(
            event=_cessation_event(),
            earlier_events=[_earlier(event_type=ChangeEventType.DIRECTOR_APPOINTMENT)],
        )
        is None
    )


def test_two_matching_events_are_ambiguity_and_refused() -> None:
    first = _earlier(event_id=uuid4())
    second = _earlier(event_id=uuid4())
    assert match_earlier_event(event=_cessation_event(), earlier_events=[first, second]) is None


def test_party_name_matching_normalizes_case_and_edge_spaces() -> None:
    earlier = _earlier(party_names=("  ALICE CHAN ",))
    assert match_earlier_event(event=_cessation_event(), earlier_events=[earlier]) == earlier
