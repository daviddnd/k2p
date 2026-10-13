"""Corpus replay of the shipped reply parser — the Gate-B/C measurement harness
(scope §7; DoD item 22; the M5b slice).

The 18-record seed corpus (``research/llm/reply-corpus/replies.jsonl``, frozen
per ``AUTHORING-BRIEF.md`` §8) holds a labeled ground truth per synthetic client
reply. This suite replays it through the **shipped** deterministic half of the
M5b parser — ``app.domain.reply_parsing.parse_violations`` (the closed grammar),
``field_scores`` (the post-check downgrades), ``route_parse`` (the §2.7 bands,
the no-guessing rule and ruling R-D's register cap) — with the model's answer
scripted from the ground truth itself, and measures the result with the corpus
README's own conventions (normalized equality, ``value`` / ``any_of`` /
``contains`` / ``expect_null`` / ``expect_bool`` matchers; the Gate-B
macro-average of per-reply
``correct / total``, overall and per language band). It is deterministic:
always-on, no network, no API key, no database.

What the replay pins, and what it does not:

- **Mechanics.** That a parse built from the labels passes the shipped grammar,
  earns the expected scores and routings, and is measured at 100% by the
  Gate-B formula — a faithful replay must be. The measurement harness failing
  to give a perfect replay a perfect number is the bug this suite catches.
- **The conservative bend.** The derivation is label-faithful: it scripts
  exactly the fields the corpus labels, never more. The shipped payload
  grammar requires values the spike-era corpus does not label (the split
  name pair — ``name_as_written`` is deliberately not mapped — a cessation
  reason, the address parts), so every change-event proposal in the replay
  is grammar-incomplete and the no-guessing rule bends its reply to
  ``client_clarification``. Only event-free replies can route
  ``auto_accept`` — on this corpus, that is the whole conservative posture
  in one assertion: **no reply carrying a change proposal auto-accepts.**
- **Gate C, deterministically.** A deliberately wrong field the post-checks
  can see — a fabricated evidence span, a calendar-impossible date, an
  uncompletable confident proposal, an answer to a question never asked —
  lands ``staff_review`` or ``client_clarification``, never ``auto_accept``.
  What the deterministic layer cannot see (a wrong value quoting the reply
  verbatim with a complete payload) is exactly what the launch gate measures
  on the corpus against a real model — the launch run is the opt-in
  ``research/llm/eval-harness/run_eval.py``, never CI.

Scope notes, stated plainly: the two ``resolver`` fields on
``test_resolver`` records are not scored here — the shipped M5b pipeline has
no resolver model fallback (deferred per the plan's decision log; the
deterministic resolver is M5a's own suite). And the seed corpus's number is
not the launch number: the ≥150 blind-authored ``replies-launch.jsonl`` is a
pre-launch deliverable per ``AUTHORING-BRIEF.md``; the DoD-22 bar (overall
≥ 90%, no band < 85%, zero high-confidence mis-extractions) is measured on
that corpus at launch. This suite pins the harness the launch run rides on.
"""

import hashlib
import json
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

import pytest

from app.domain.confidence import ConfidenceBand
from app.domain.reply_parsing import (
    field_scores,
    parse_violations,
    proposal_violations,
    route_parse,
    span_is_verbatim,
)

CORPUS_PATH = (
    Path(__file__).resolve().parents[2] / "research" / "llm" / "reply-corpus" / "replies.jsonl"
)

# The seed is frozen (AUTHORING-BRIEF.md §8: revisions are new files, never
# edits in place) — the digest is the lineage, so the test pins it. A failure
# here means the bytes under measurement are not the bytes this harness pins.
CORPUS_SHA256 = "c2f47d93a05161e43c6c8d84b8f6a2edb4d0929633bb75d40482ef642441903d"
CORPUS_RECORDS = 18

# The scored-field accounting the corpus README promises, derived by hand from
# the README's "one scored field, one entry" table over the shipped parser's
# surface (status, confirmations, event fields, expect_no_events,
# company_mentions — resolver fields excluded, see the module docstring).
EXPECTED_FIELD_COUNT = 87
EXPECTED_FIELD_COUNT_BY_BAND = {"en": 32, "zh-hk": 34, "mixed": 21}


def _load_records() -> list[dict[str, Any]]:
    raw = CORPUS_PATH.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == CORPUS_SHA256, (
        f"{CORPUS_PATH} changed: the pinned harness no longer measures the "
        "frozen seed (AUTHORING-BRIEF.md §8 — revisions are new files)"
    )
    records = [json.loads(line) for line in raw.decode("utf-8").splitlines() if line.strip()]
    assert len(records) == CORPUS_RECORDS
    return records


RECORDS = _load_records()
RECORDS_BY_ID = {record["id"]: record for record in RECORDS}

# The portfolio the mention-kind classifier reads (five synthetic companies).
_PORTFOLIO = {
    record["company_id"]: record["company_context"] for record in RECORDS
}


# --------------------------------------------------------------------------
# Ground-truth matchers (the corpus README's conventions, as data)
# --------------------------------------------------------------------------

MISSING = object()  # sentinel for unresolved dotted paths


def _norm(value: Any) -> str:
    """The README's normalization: casefold, whitespace collapsed, stripped."""
    if value is None:
        return ""
    return " ".join(str(value).casefold().split())


def _resolve_path(obj: Any, dotted: str) -> Any:
    cur = obj
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return MISSING
        cur = cur[part]
    return cur


def _match_expected(actual: Any, spec: dict[str, Any]) -> bool:
    """One ground-truth matcher from the corpus README; MISSING never matches."""
    if actual is MISSING:
        return False
    if spec.get("expect_null"):
        return actual is None
    if "expect_bool" in spec:
        return actual is spec["expect_bool"]
    if "value" in spec:
        return _norm(actual) == _norm(spec["value"])
    if "any_of" in spec:
        return any(_norm(actual) == _norm(alt) for alt in spec["any_of"])
    if "contains" in spec:
        return actual is not None and _norm(spec["contains"]) in _norm(actual)
    raise ValueError(f"unknown matcher spec: {spec}")


def _match_events(expected_events: list[dict], parsed_events: list[dict]) -> list[tuple]:
    """Pair each expected event with a parsed event of the same ``type`` —
    best ``party.name_as_written`` match on ties, each parsed event consumed
    once (the README's own rule, as the harness implements it)."""
    used: set[int] = set()
    pairs = []
    for expected in expected_events:
        candidates = [
            (i, parsed)
            for i, parsed in enumerate(parsed_events)
            if i not in used and parsed.get("type") == expected["type"]
        ]
        if not candidates:
            pairs.append((expected, None))
            continue
        name_spec = expected.get("fields", {}).get("party.name_as_written")
        if len(candidates) > 1 and name_spec:
            alternatives = [
                _norm(alt) for alt in (name_spec.get("any_of") or [name_spec.get("value")])
            ]

            def score(item: tuple[int, dict], alternatives: list = alternatives) -> float:
                name = _norm(_resolve_path(item[1], "party.name_as_written"))
                if not name or not alternatives:
                    return 0.0
                return max(SequenceMatcher(None, name, alt).ratio() for alt in alternatives)

            chosen = max(candidates, key=score)
        else:
            chosen = candidates[0]
        used.add(chosen[0])
        pairs.append((expected, chosen[1]))
    return pairs


# --------------------------------------------------------------------------
# The replay: ground truth → scripted model JSON (the deterministic derivation)
# --------------------------------------------------------------------------


def _verbatim_span(record: dict) -> str:
    """The replay's documented span convention: the whole reply.

    The replay measures scoring and matching mechanics, not span precision —
    the full reply is a verbatim substring of itself by construction, so every
    scripted span passes the verbatim post-check exactly as a faithfully
    quoted span would. (Span precision on real model output is the live run's
    subject, never CI's.)
    """
    return record["reply_text"]


def _name_choice(record: dict, spec: dict[str, Any]) -> str:
    """The name form the scripted party carries: the first alternative that
    appears verbatim in the reply, else the first alternative (the corpus
    brief requires the reply's own string among the alternatives, so the
    first is the honest default)."""
    alternatives = spec.get("any_of") or [spec.get("value")]
    for alternative in alternatives:
        if isinstance(alternative, str) and alternative in record["reply_text"]:
            return alternative
    return alternatives[0]


def _date_choice(spec: dict[str, Any]) -> str | None:
    """The scripted ISO date: the labeled value, the first alternative, or
    ``None`` for ``expect_null``."""
    if spec.get("expect_null"):
        return None
    if "value" in spec:
        return spec["value"]
    return spec["any_of"][0]


def _mention_kind(text: str) -> str:
    """The closed ``kind`` vocabulary, classified against the portfolio."""
    for context in _PORTFOLIO.values():
        if text == context["brn"]:
            return "brn"
    for context in _PORTFOLIO.values():
        if _norm(text) == _norm(context["name_en"]):
            return "name_en"
    for context in _PORTFOLIO.values():
        if text == context["name_zh"]:
            return "name_zh"
    return "other"


def scripted_parse(record: dict) -> dict[str, Any]:
    """The model's answer, scripted deterministically from the record's labels.

    Label-faithful means: exactly the fields the ground truth labels, never
    more. The corpus labels ``party.name_as_written`` but not the split name
    pair, not a cessation reason, and only a ``contains`` substring for an
    address — so those scripted events carry no ``new_values`` beyond the
    labeled address substring, and (per ``payload_proposal``'s deliberate
    ``name_as_written`` exclusion) every proposal is grammar-incomplete. That
    is a property of the boundary between spike-era labels and the shipped
    payload grammar, stated here because the routing assertions read it.
    """
    ground_truth = record["ground_truth"]
    span = _verbatim_span(record)

    confirmations = [
        {
            "fact_id": item["fact_id"],
            "answer": item["answer"],
            "evidence_span": span,
            "uncertain": False,
        }
        for item in ground_truth.get("confirmations", [])
    ]

    change_events = []
    for expected in ground_truth.get("events", []):
        fields = expected.get("fields", {})
        name_spec = fields.get("party.name_as_written")
        name = _name_choice(record, name_spec) if name_spec else "the company"
        effective = _date_choice(fields["effective_date"]) if "effective_date" in fields else None
        informed = _date_choice(fields["informed_date"]) if "informed_date" in fields else None
        uncertain = (
            fields["uncertain"]["expect_bool"] if "uncertain" in fields else False
        )
        # The least-invented new value: for a labeled new_values.* `contains`
        # spec, the substring itself under the labeled key — the smallest
        # value that satisfies the label and nothing beyond it.
        new_values = {
            path.removeprefix("new_values."): spec["contains"]
            for path, spec in fields.items()
            if path.startswith("new_values.") and "contains" in spec
        }
        change_events.append(
            {
                "type": expected["type"],
                "party": {
                    "name_as_written": name,
                    "name_en": None,
                    "name_zh": None,
                    "partial_id": None,
                    "evidence_span": name if name in record["reply_text"] else span,
                    "uncertain": False,
                },
                "effective_date": effective,
                "effective_date_text": "" if effective is None else effective,
                "informed_date": informed,
                "new_values": new_values,
                "evidence_span": span,
                "uncertain": uncertain,
                "notes": None,
            }
        )

    mentions = [
        {"text": text, "kind": _mention_kind(text)}
        for text in ground_truth.get("company_mentions", [])
    ]
    return {
        "status": ground_truth["status"],
        "language": record["language"],
        "confirmations": confirmations,
        "change_events": change_events,
        "multi_company_signals": {"company_mentions": mentions},
    }


# --------------------------------------------------------------------------
# The Gate-B measurement (the corpus README's unit accounting), over the
# shipped parser's surface
# --------------------------------------------------------------------------


def score_replay(record: dict, parse: dict) -> dict[str, Any]:
    """Score one replayed reply: every labeled field correct/incorrect, per
    the README's accounting — status (1), each confirmation (1), each expected
    event field (1), ``expect_no_events`` (1), ``company_mentions`` (1 for the
    set). Resolver fields are out of scope for the shipped parser (module
    docstring)."""
    ground_truth = record["ground_truth"]
    fields: list[dict[str, Any]] = []

    def add(field_id: str, correct: bool) -> None:
        fields.append({"field_id": field_id, "correct": correct})

    parsed_events = parse["change_events"]
    pairs = _match_events(ground_truth.get("events", []), parsed_events)

    add("status", _norm(parse["status"]) == _norm(ground_truth["status"]))

    confirmations_by_fact = {c["fact_id"]: c for c in parse["confirmations"]}
    for expected in ground_truth.get("confirmations", []):
        parsed = confirmations_by_fact.get(expected["fact_id"])
        add(
            f"confirm:{expected['fact_id']}",
            parsed is not None and _norm(parsed["answer"]) == _norm(expected["answer"]),
        )

    for index, (expected, event) in enumerate(pairs):
        for path, spec in expected.get("fields", {}).items():
            actual = _resolve_path(event, path) if event is not None else MISSING
            # The documented scorer fallback (the schema allows arbitrary
            # new_values keys): a `contains` spec also runs against every
            # new_values value joined when the exact key is absent.
            if (
                actual is MISSING
                and event is not None
                and path.startswith("new_values.")
                and "contains" in spec
            ):
                values = event.get("new_values")
                if isinstance(values, dict):
                    joined = " ".join(v for v in values.values() if isinstance(v, str))
                    actual = joined if joined else MISSING
            add(f"event[{index}:{expected['type']}].{path}", _match_expected(actual, spec))

    if "expect_no_events" in ground_truth:
        add(
            "expect_no_events",
            (len(parsed_events) == 0) is ground_truth["expect_no_events"],
        )

    if "company_mentions" in ground_truth:
        mentions = [m.get("text", "") for m in parse["multi_company_signals"]["company_mentions"]]
        joined = _norm(" || ".join(mentions))
        all_found = all(_norm(want) in joined for want in ground_truth["company_mentions"])
        add("company_mentions", all_found)

    correct = sum(1 for field in fields if field["correct"])
    return {
        "fields": fields,
        "correct": correct,
        "total": len(fields),
        "accuracy": correct / len(fields) if fields else None,
    }


def macro_average(accuracies: list[float]) -> float | None:
    """Gate B's unit: the macro-average — every reply weighs the same."""
    if not accuracies:
        return None
    return sum(accuracies) / len(accuracies)


def replay_parse(record: dict, parse: dict) -> dict[str, Any]:
    """The shipped pipeline's deterministic half, as ``_land`` drives it:
    grammar first (fail-closed), then scoring, then routing.

    ``route_parse`` reads the company's register too (ruling R-D), so the
    replay hands it the only register the corpus carries — the record's own
    ``known_officers``, in exactly the forms ``_person_names`` yields: the
    English name and the Chinese name, the two script forms the master record
    is written in. That is what keeps the Gate-C pins below honest: R-D is
    live in this harness, and R01's injected cessation auto-accepts because
    ``CHEUNG Ka Yan`` *is* one of the company's directors, not because the
    rule was out of reach.
    """
    scores = None if parse_violations(parse) else field_scores(
        parse=parse,
        reply_text=record["reply_text"],
        checklist_fact_ids={item["fact_id"] for item in record["checklist"]},
    )
    routing = (
        None
        if scores is None
        else route_parse(
            scores=scores,
            change_events=parse["change_events"],
            registered_names=_register_names(record),
        )
    )
    return {"scores": scores, "routing": routing}


def _register_names(record: dict) -> tuple[str, ...]:
    """The record's known officers as the parser's register read would return
    them: ``list_appointments`` projects each person's name forms, and every
    officer in the corpus context is a person record with the two forms
    (``name_zh`` is absent for the English-only contexts)."""
    return tuple(
        name
        for officer in record["company_context"]["known_officers"]
        for name in (officer.get("name_en"), officer.get("name_zh"))
        if name
    )


# --------------------------------------------------------------------------
# The corpus is the frozen seed, and the derivation passes the shipped grammar
# --------------------------------------------------------------------------


def test_the_corpus_is_the_frozen_seed() -> None:
    """Lineage, not courtesy (AUTHORING-BRIEF.md §8): the digest is the pin."""
    assert {record["id"] for record in RECORDS} == {
        f"R{index:02d}" for index in range(1, CORPUS_RECORDS + 1)
    }
    assert len({record["language"] for record in RECORDS}) == 3


def test_every_scripted_parse_passes_the_shipped_grammar() -> None:
    """A parse built from the labels must pass the closed grammar — this is
    the tripwire for drift between the corpus's conventions and the shipped
    ``reply-parse`` grammar (a rename on either side fails here)."""
    for record in RECORDS:
        assert parse_violations(scripted_parse(record)) == [], record["id"]


# --------------------------------------------------------------------------
# Mechanics: the Gate-B formula over the faithful replay
# --------------------------------------------------------------------------


def test_the_scored_field_accounting_is_the_readmes() -> None:
    """The harness counts exactly the README's fields — no padding, no drops.
    The totals are hand-derived from the README's table over this corpus's
    labels (resolver fields excluded, module docstring)."""
    totals = {band: 0 for band in EXPECTED_FIELD_COUNT_BY_BAND}
    for record in RECORDS:
        scored = score_replay(record, scripted_parse(record))
        assert scored["correct"] == scored["total"] > 0, record["id"]
        totals[record["language"]] += scored["total"]
    assert sum(totals.values()) == EXPECTED_FIELD_COUNT
    assert totals == EXPECTED_FIELD_COUNT_BY_BAND


def test_the_macro_average_is_the_gate_b_unit_not_the_micro() -> None:
    """Macro weighs every reply equally; micro weighs every field. The formula
    pinned here must be the macro — a one-field reply counts exactly as much
    as a fifteen-field reply."""
    macro = macro_average([1.0, 1 / 3])
    micro = (1 + 1) / (1 + 3)
    assert macro == pytest.approx(2 / 3)
    assert macro != micro


def test_the_faithful_replay_measures_perfect_end_to_end() -> None:
    """The mechanics pin: ground truth → scripted parse → shipped grammar,
    scoring, routing → Gate-B measurement gives a faithful replay 100%,
    overall and in every language band. A harness that cannot give a perfect
    replay a perfect number is broken — that is what this asserts."""
    results = {
        record["id"]: score_replay(record, scripted_parse(record)) for record in RECORDS
    }
    overall = macro_average([r["accuracy"] for r in results.values()])
    assert overall == 1.0
    for band in EXPECTED_FIELD_COUNT_BY_BAND:
        band_results = [
            result
            for record, result in results.items()
            if RECORDS_BY_ID[record]["language"] == band
        ]
        assert macro_average([r["accuracy"] for r in band_results]) == 1.0, band


# --------------------------------------------------------------------------
# Routing pins: the conservative posture, end to end
# --------------------------------------------------------------------------


def test_event_free_replies_route_auto_accept() -> None:
    """The positive control: a clean replay with nothing to propose earns the
    auto-accept band — full-mark field scores, no proposal, no bend. If this
    ever flips, the Gate-C assertions below lose their meaning."""
    for record in RECORDS:
        if record["ground_truth"].get("events"):
            continue
        replay = replay_parse(record, scripted_parse(record))
        assert replay["routing"] is ConfidenceBand.AUTO_ACCEPT, record["id"]


def test_no_reply_carrying_a_change_proposal_auto_accepts() -> None:
    """The no-guessing rule over the label-faithful replay: every scripted
    event lacks a grammar-required value the corpus does not label (the split
    name pair, a cessation reason, the address parts), so every event-bearing
    reply bends to ``client_clarification`` — and the bend, not a weak score,
    does it (the scores themselves are full or near-full marks)."""
    for record in RECORDS:
        expected_events = record["ground_truth"].get("events", [])
        if not expected_events:
            continue
        parse = scripted_parse(record)
        replay = replay_parse(record, parse)
        assert replay["routing"] is ConfidenceBand.CLIENT_CLARIFICATION, record["id"]
        assert replay["scores"] is not None
        # The bend, not the score: on every reply but the uncertain R12, all
        # fields scored inside the auto-accept band on score alone — the
        # clarification routing is the no-guessing rule's work, not a weak
        # parse's (R12's bend is doubled: 0.7 by its own uncertain flag, then
        # the rule regardless).
        if record["id"] != "R12":
            assert all(score >= 0.85 for score in replay["scores"].values()), record["id"]
        for event in parse["change_events"]:
            assert proposal_violations(event=event), record["id"]


def test_the_uncertain_reply_scores_down_but_still_bends() -> None:
    """R12 carries ``uncertain: true`` on its event: the UNCERTAIN_PENALTY
    downgrades the field to staff_review on score alone, and the null
    effective date bends the whole reply to clarification regardless."""
    record = RECORDS_BY_ID["R12"]
    parse = scripted_parse(record)
    replay = replay_parse(record, parse)
    assert replay["scores"]["change_events.0"] == 0.7
    assert replay["routing"] is ConfidenceBand.CLIENT_CLARIFICATION


# --------------------------------------------------------------------------
# Gate C, deterministically: a deliberately wrong high-confidence field never
# auto-accepts
# --------------------------------------------------------------------------

_FABRICATED = "the moon office, which appears nowhere in the reply"


def _r01_with_injected_event(event: dict) -> tuple[dict, dict]:
    """R01's clean replay plus one injected change event — the host every
    Gate-C injection rides on (its un-injected parse routes auto_accept, so
    the injection is what moves the routing)."""
    record = RECORDS_BY_ID["R01"]
    parse = scripted_parse(record)
    assert replay_parse(record, parse)["routing"] is ConfidenceBand.AUTO_ACCEPT
    parse["status"] = "changes_reported"
    parse["change_events"] = [event]
    return record, parse


def _confident_cessation(record: dict, **overrides: Any) -> dict:
    """A fully confident, payload-complete cessation proposal — every span
    verbatim in this reply (the whole-reply convention), a real calendar
    date, a split English name, a named reason — unless an override says
    otherwise. Completeness matters: the Gate-C tests inject exactly one
    wrong thing each, so everything else must be fileable."""
    event = {
        "type": "director_cessation",
        "party": {
            "name_as_written": "CHEUNG Ka Yan",
            "name_en": "CHEUNG Ka Yan",
            "name_zh": None,
            "partial_id": None,
            "evidence_span": "CHEUNG Ka Yan",
            "uncertain": False,
        },
        "effective_date": "2026-08-01",
        "effective_date_text": "1 August 2026",
        "informed_date": None,
        "new_values": {"cessation_reason": "resignation_others"},
        "evidence_span": record["reply_text"],
        "uncertain": False,
        "notes": None,
    }
    event.update(overrides)
    return event


def _confident_office_move(record: dict, **overrides: Any) -> dict:
    """A fully confident, payload-complete registered-office move — complete
    address parts, every span verbatim in this reply (the whole-reply
    convention) — unless an override says otherwise. The office payload
    carries the address and no date, so the Gate-C date injection rides a
    payload the date grammar cannot see."""
    event = {
        "type": "registered_office_change",
        "party": {
            "name_as_written": "the registered office",
            "name_en": None,
            "name_zh": None,
            "partial_id": None,
            "evidence_span": record["reply_text"],
            "uncertain": False,
        },
        "effective_date": "2026-08-01",
        "effective_date_text": "1 August 2026",
        "informed_date": None,
        "new_values": {
            "flat": "Unit 5",
            "building": "Kai Chuen Building",
            "street": "1 Kai Chuen Road",
            "district": "Kowloon",
        },
        "evidence_span": record["reply_text"],
        "uncertain": False,
        "notes": None,
    }
    event.update(overrides)
    return event


def test_a_wrong_field_with_a_fabricated_span_lands_staff_review() -> None:
    """Gate C, the fabrication shape: the model asserts a cessation R01's
    reply never reports, confidently — but its evidence span quotes nothing
    in the reply. The verbatim post-check downgrades the field to 0.6, the
    weakest-field rule drags the whole parse to staff_review, and
    ``auto_accept`` is unreachable. The injected value is genuinely wrong:
    R01's ground truth asserts no events at all."""
    record, parse = _r01_with_injected_event(
        _confident_cessation(RECORDS_BY_ID["R01"], evidence_span=_FABRICATED)
    )
    replay = replay_parse(record, parse)
    assert replay["scores"] is not None
    assert replay["scores"]["change_events.0"] == 0.6
    assert replay["routing"] is ConfidenceBand.STAFF_REVIEW


def test_a_wrong_calendar_impossible_date_never_auto_accepts() -> None:
    """Gate C, the impossible-date shape: ``2026-13-45`` matches the grammar's
    ISO *shape*, so the grammar passes it — and two deterministic mechanisms
    each independently keep it from auto-accept: the calendar post-check
    drops the field to 0.7 (staff_review on score alone), and the payload
    grammar refuses the date, so the no-guessing rule bends the parse to
    ``client_clarification`` regardless of the score. The assertion pins the
    bend, the score pins the post-check."""
    record, parse = _r01_with_injected_event(
        _confident_cessation(RECORDS_BY_ID["R01"], effective_date="2026-13-45")
    )
    replay = replay_parse(record, parse)
    assert replay["scores"] is not None
    assert replay["scores"]["change_events.0"] == 0.7
    assert replay["routing"] is ConfidenceBand.CLIENT_CLARIFICATION


def test_a_confident_but_uncompletable_wrong_event_bends_to_clarification() -> None:
    """Gate C, the no-guessing shape: a wrong cessation stated with total
    confidence — verbatim spans, a named reason — but no effective date the
    payload grammar can file. The field scores a flawless 1.0; the routing is
    ``client_clarification`` **regardless of score**. This bend is the
    shipped pipeline's own answer to the high-confidence mis-extraction."""
    record, parse = _r01_with_injected_event(
        _confident_cessation(RECORDS_BY_ID["R01"], effective_date=None, effective_date_text="")
    )
    replay = replay_parse(record, parse)
    assert replay["scores"] is not None
    assert replay["scores"]["change_events.0"] == 1.0
    assert replay["routing"] is ConfidenceBand.CLIENT_CLARIFICATION


def test_an_office_change_with_an_impossible_effective_date_bends_to_clarification() -> None:
    """Gate C, the payload-blind shape: the office-change payload carries the
    address and no date, so the payload grammar cannot catch a calendar-
    impossible effective_date — the calendar refusal in
    ``proposal_violations`` is the guard, and the no-guessing bend routes the
    parse to ``client_clarification``. The application landing reads the same
    refusal before it ever parses the date: this shape routes, never
    crashes, never lands."""
    record, parse = _r01_with_injected_event(
        _confident_office_move(RECORDS_BY_ID["R01"], effective_date="2026-02-30")
    )
    replay = replay_parse(record, parse)
    assert replay["routing"] is ConfidenceBand.CLIENT_CLARIFICATION


def test_an_officer_event_with_an_impossible_informed_date_bends_to_clarification() -> None:
    """Gate C, the informed-date shape: no payload check exists for
    ``informed_date`` at all — the same one calendar refusal covers it, and
    the parse bends to ``client_clarification`` instead of landing an
    impossible fact or crashing on it."""
    record, parse = _r01_with_injected_event(
        _confident_cessation(RECORDS_BY_ID["R01"], informed_date="2026-13-45")
    )
    replay = replay_parse(record, parse)
    assert replay["routing"] is ConfidenceBand.CLIENT_CLARIFICATION


def test_an_answer_to_a_question_never_asked_lands_staff_review() -> None:
    """Gate C, the unknown-fact shape: a confident confirmation whose fact_id
    is not on the request's checklist cannot join it — the UNKNOWN_FACT_ID
    downgrade takes the field to 0.5, the staff_review band, and the weakest-
    field rule takes the parse with it. Never ``auto_accept``."""
    record = RECORDS_BY_ID["R01"]
    parse = scripted_parse(record)
    assert replay_parse(record, parse)["routing"] is ConfidenceBand.AUTO_ACCEPT
    parse["confirmations"].append(
        {
            "fact_id": "company.moon_base",
            "answer": "confirmed",
            "evidence_span": record["reply_text"],
            "uncertain": False,
        }
    )
    replay = replay_parse(record, parse)
    assert replay["scores"] is not None
    assert replay["scores"]["confirmations.company.moon_base"] == 0.5
    assert replay["routing"] is ConfidenceBand.STAFF_REVIEW


def test_a_verbatim_wrong_value_is_why_gate_c_needs_the_live_run() -> None:
    """The boundary, stated as a test so no one over-reads the suite: a wrong
    value that quotes the reply verbatim, with a complete payload, scores 1.0
    and routes ``auto_accept`` — the deterministic layer cannot see it. Gate
    C's bar (zero such fields on the corpus) is measured against a real model
    by the opt-in live run; this suite pins only what code can guarantee."""
    record = RECORDS_BY_ID["R01"]
    # "CHEUNG Ka Yan" appears verbatim in R01's reply, as does its checklist
    # sentence — a cessation of the company's own director is plausible, wrong
    # (the reply confirms everything), and fully evidenced: the undetectable
    # shape. Only the full-reply span convention keeps every span verbatim.
    parse = scripted_parse(record)
    parse["status"] = "changes_reported"
    parse["change_events"] = [_confident_cessation(record)]
    replay = replay_parse(record, parse)
    assert replay["scores"] is not None
    assert replay["scores"]["change_events.0"] == 1.0
    assert replay["routing"] is ConfidenceBand.AUTO_ACCEPT
    assert not span_is_verbatim(_FABRICATED, record["reply_text"])
