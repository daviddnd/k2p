"""Domain tests for the multi-company resolver (scope §2.5, §7; data-model §2.7).

Pure and database-free: the message text comes in as a string, the sender's
companies come in as identifiers, and nothing here loads either. The rule is
the one the Phase 0 spike showed a model cannot be trusted with — it listed
two candidates with equal ``name_exact`` evidence and still answered
``resolved`` (reply R16) — so these tests are about the two properties that
keep the answer deterministic: a tie never resolves, and fuzzy evidence never
resolves.

The two companies are the spike corpus' own: Bright Lantern Design Limited
and Pacific Jade Logistics Limited, with the identifiers a Hong Kong company
actually carries (an eight-digit BRN, a seven-digit CR number, an English and
a Traditional Chinese registered name).
"""

from uuid import UUID

from app.domain.reply_resolver import (
    Candidate,
    CompanyIdentifiers,
    MatchEvidence,
    Resolution,
    resolve,
)

BRIGHT = UUID("11111111-1111-1111-1111-111111111111")
JADE = UUID("22222222-2222-2222-2222-222222222222")
ASIA = UUID("33333333-3333-3333-3333-333333333333")

ASIA_LIMITED = CompanyIdentifiers(
    br_number="11112222", cr_number="3334444", name_en="Asia Limited"
)
BRIGHT_LANTERN = CompanyIdentifiers(
    br_number="12345678",
    cr_number="2345678",
    name_en="Bright Lantern Design Limited",
    name_zh="明燈設計有限公司",
)
PACIFIC_JADE = CompanyIdentifiers(
    br_number="87654321",
    cr_number="1234567",
    name_en="Pacific Jade Logistics Limited",
    name_zh="翠玉太平洋物流有限公司",
)
PORTFOLIO = {BRIGHT: BRIGHT_LANTERN, JADE: PACIFIC_JADE}


def test_the_vocabulary_is_the_spike_schemas_plus_cr_exact() -> None:
    # brn_exact, name_exact, name_fuzzy and context_only are the Phase 0
    # spike schema's own set; cr_exact is this module's addition — a CR
    # number is an identifier in its own right, and reporting it as
    # ``brn_exact`` would lose which number the client actually quoted. The
    # M5b fallback will need that mapping, since the spike schema has no
    # ``cr_exact`` to answer in.
    assert {item.value for item in MatchEvidence} == {
        "cr_exact",
        "brn_exact",
        "name_exact",
        "name_fuzzy",
        "context_only",
    }
    assert {item.value for item in Resolution} == {"resolved", "ambiguous", "no_match"}


def test_an_exact_brn_resolves() -> None:
    outcome = resolve(text="Hi, our BRN is 12345678 — please use it.", candidates=PORTFOLIO)

    assert outcome.resolution is Resolution.RESOLVED
    assert outcome.company_id == BRIGHT
    assert outcome.evidence is MatchEvidence.BRN_EXACT
    assert outcome.score == 1.0
    assert outcome.candidates == (
        Candidate(company_id=BRIGHT, evidence=MatchEvidence.BRN_EXACT, matched_text="12345678"),
    )


def test_an_exact_cr_resolves() -> None:
    outcome = resolve(text="Please file this against CR 1234567.", candidates=PORTFOLIO)

    assert outcome.resolution is Resolution.RESOLVED
    assert outcome.company_id == JADE
    assert outcome.evidence is MatchEvidence.CR_EXACT
    assert outcome.score == 1.0
    assert outcome.candidates == (
        Candidate(company_id=JADE, evidence=MatchEvidence.CR_EXACT, matched_text="1234567"),
    )


def test_a_digit_run_that_only_contains_a_cr_is_not_a_cr_match() -> None:
    # 1234567 sits inside 91234567. A number is a run of digits, not any
    # substring of one — the fragment is not Pacific Jade's CR number.
    outcome = resolve(text="The bank reference was 91234567.", candidates=PORTFOLIO)

    assert outcome.resolution is Resolution.NO_MATCH
    assert outcome.company_id is None
    assert outcome.candidates == ()
    assert outcome.score < 0.5


def test_an_exact_english_name_resolves() -> None:
    outcome = resolve(text="Bright Lantern Design Limited has moved office.", candidates=PORTFOLIO)

    assert outcome.resolution is Resolution.RESOLVED
    assert outcome.company_id == BRIGHT
    assert outcome.evidence is MatchEvidence.NAME_EXACT
    assert outcome.score == 0.9
    assert outcome.candidates == (
        Candidate(
            company_id=BRIGHT,
            evidence=MatchEvidence.NAME_EXACT,
            matched_text="Bright Lantern Design Limited",
        ),
    )


def test_an_exact_traditional_chinese_name_resolves() -> None:
    outcome = resolve(text="明燈設計有限公司的地址沒有改變。", candidates=PORTFOLIO)

    assert outcome.resolution is Resolution.RESOLVED
    assert outcome.company_id == BRIGHT
    assert outcome.evidence is MatchEvidence.NAME_EXACT
    assert outcome.candidates[0].matched_text == "明燈設計有限公司"


def test_a_latin_name_inside_a_longer_word_is_not_a_match() -> None:
    # "Eurasia Limited" is not "Asia Limited". A Latin name has to be a word
    # of its own: a decisive tier matching a fragment would attribute the
    # message to a company it never named, which is the breach this rule
    # exists to prevent.
    outcome = resolve(
        text="Eurasia Limited is the holding company.",
        candidates={ASIA: ASIA_LIMITED, JADE: PACIFIC_JADE},
    )

    assert outcome.resolution is Resolution.NO_MATCH
    assert outcome.company_id is None
    # The one candidate that resembles the text at all is left at the fuzzy
    # tier, where it decides nothing; the other company is not in the list.
    assert [(item.company_id, item.evidence) for item in outcome.candidates] == [
        (ASIA, MatchEvidence.NAME_FUZZY)
    ]


def test_a_latin_name_is_still_a_match_at_word_boundaries() -> None:
    for text in ("Asia Limited confirmed.", "the Asia Limited", "(Asia Limited)"):
        outcome = resolve(text=text, candidates={ASIA: ASIA_LIMITED})

        assert outcome.resolution is Resolution.RESOLVED, text
        assert outcome.company_id == ASIA, text
        assert outcome.evidence is MatchEvidence.NAME_EXACT, text


def test_a_latin_name_glued_to_chinese_still_matches() -> None:
    # Hong Kong mail mixes scripts with no space between them, so 的 or a verb
    # runs straight into the Latin name. The boundary test is ASCII-only for
    # exactly this reason: 的 and 知 are word characters to Python's Unicode
    # ``\w``, which would refuse both of these ordinary mentions and quarantine
    # them.
    for text in ("Asia Limited的地址沒有改變。", "通知Asia Limited"):
        outcome = resolve(text=text, candidates={ASIA: ASIA_LIMITED})

        assert outcome.resolution is Resolution.RESOLVED, text
        assert outcome.company_id == ASIA, text
        assert outcome.evidence is MatchEvidence.NAME_EXACT, text


def test_a_cjk_name_inside_a_longer_chinese_name_still_matches() -> None:
    # The accepted residual (see the module docstring): Chinese has no word
    # delimiters, so 明燈設計有限公司 inside a longer Chinese name still
    # matches. Chinese replies are the product's primary market, and a
    # boundary guard would reject the ordinary 請通知太平有限公司的地址; the
    # M5b fallback is the designed answer for the hard cases, and what
    # contains a wrong match here is that the candidates are always the
    # sender's own registered companies.
    outcome = resolve(text="宏昌明燈設計有限公司的地址沒有改變。", candidates=PORTFOLIO)

    assert outcome.resolution is Resolution.RESOLVED
    assert outcome.company_id == BRIGHT
    assert outcome.evidence is MatchEvidence.NAME_EXACT


def test_case_and_whitespace_do_not_hide_a_name() -> None:
    outcome = resolve(text="BRIGHT   LANTERN\n\tDesign LIMITED confirmed.", candidates=PORTFOLIO)

    assert outcome.company_id == BRIGHT
    assert outcome.evidence is MatchEvidence.NAME_EXACT
    # The evidence is the text the client typed, not the registered name.
    assert outcome.candidates[0].matched_text == "BRIGHT   LANTERN\n\tDesign LIMITED"


def test_the_strongest_evidence_a_candidate_has_is_the_one_reported() -> None:
    outcome = resolve(text="Bright Lantern Design Limited (BRN 12345678)", candidates=PORTFOLIO)

    assert outcome.company_id == BRIGHT
    assert outcome.evidence is MatchEvidence.BRN_EXACT
    assert len(outcome.candidates) == 1


def test_a_tie_between_two_decisive_candidates_is_ambiguous() -> None:
    text = "Bright Lantern Design Limited and Pacific Jade Logistics Limited both filed late."

    outcome = resolve(text=text, candidates=PORTFOLIO)

    assert outcome.resolution is Resolution.AMBIGUOUS
    assert outcome.company_id is None
    assert outcome.evidence is None
    assert outcome.score < 0.5
    assert [item.evidence for item in outcome.candidates] == [
        MatchEvidence.NAME_EXACT,
        MatchEvidence.NAME_EXACT,
    ]


def test_a_tie_across_evidence_tiers_is_still_ambiguous() -> None:
    # This rule is stricter than the spike's post-check: that one resolved a
    # BRN-versus-name case to the BRN company (its evidence hierarchy puts an
    # identifier above a name). Here a name is decisive evidence too, so two
    # decisive candidates of any tiers are a tie, and a tie never resolves —
    # the cost of asking staff is paid rather than the cost of filing against
    # the wrong company.
    outcome = resolve(
        text="BRN 12345678 — and also Pacific Jade Logistics Limited.", candidates=PORTFOLIO
    )

    assert outcome.resolution is Resolution.AMBIGUOUS
    assert outcome.company_id is None
    assert outcome.evidence is None
    assert [item.evidence for item in outcome.candidates] == [
        MatchEvidence.BRN_EXACT,
        MatchEvidence.NAME_EXACT,
    ]


def test_two_numbers_of_two_companies_are_ambiguous() -> None:
    outcome = resolve(
        text="Our BRN is 12345678 and theirs is 87654321.", candidates=PORTFOLIO
    )

    assert outcome.resolution is Resolution.AMBIGUOUS
    assert outcome.company_id is None
    assert outcome.score < 0.5


def test_fuzzy_evidence_never_resolves() -> None:
    outcome = resolve(text="Bright Lantem Deslgn Limited confirmed.", candidates=PORTFOLIO)

    assert outcome.resolution is Resolution.NO_MATCH
    assert outcome.company_id is None
    assert outcome.evidence is None
    assert outcome.score < 0.5
    assert outcome.candidates == (
        Candidate(
            company_id=BRIGHT,
            evidence=MatchEvidence.NAME_FUZZY,
            matched_text="Bright Lantem Deslgn Limited",
        ),
    )


def test_fuzzy_evidence_is_the_messages_own_characters() -> None:
    text = "the client wrote: Bright\n\nLantem   Deslgn Limited, thanks"

    outcome = resolve(text=text, candidates=PORTFOLIO)

    assert outcome.resolution is Resolution.NO_MATCH
    assert outcome.company_id is None
    assert [(item.evidence, item.matched_text) for item in outcome.candidates] == [
        (MatchEvidence.NAME_FUZZY, "Bright\n\nLantem   Deslgn Limited,")
    ]


def test_a_loose_reference_to_a_company_is_not_a_match() -> None:
    # The spike's prompt: a partial name ("the design company") is not the
    # registered name, and close is not a resolution either.
    outcome = resolve(text="Please ask the design company to confirm.", candidates=PORTFOLIO)

    assert outcome.resolution is Resolution.NO_MATCH
    assert outcome.company_id is None
    assert outcome.candidates == ()


def test_a_company_outside_the_candidates_is_no_match() -> None:
    # The candidates are the sender's own companies: a third company named in
    # the message matches nothing here, which is what keeps a sender from
    # reaching one.
    outcome = resolve(
        text="Pacific Jade Logistics Limited confirmed.", candidates={BRIGHT: BRIGHT_LANTERN}
    )

    assert outcome.resolution is Resolution.NO_MATCH
    assert outcome.company_id is None
    assert outcome.candidates == ()


def test_no_candidates_at_all_is_no_match() -> None:
    outcome = resolve(text="Bright Lantern Design Limited", candidates={})

    assert outcome.resolution is Resolution.NO_MATCH
    assert outcome.company_id is None
    assert outcome.evidence is None
    assert outcome.candidates == ()
    assert outcome.score < 0.5


def test_an_empty_message_is_no_match() -> None:
    outcome = resolve(text="", candidates=PORTFOLIO)

    assert outcome.resolution is Resolution.NO_MATCH
    assert outcome.company_id is None
    assert outcome.candidates == ()


def test_a_candidate_without_identifiers_matches_nothing() -> None:
    empty = CompanyIdentifiers()
    outcome = resolve(text="Our BRN is 12345678.", candidates={BRIGHT: empty})

    assert outcome.resolution is Resolution.NO_MATCH
    assert outcome.company_id is None
    assert outcome.candidates == ()


def test_a_formatted_stored_identifier_matches_nothing() -> None:
    # The comparison is verbatim against the master data's stored form, so a
    # CR number the TCSP recorded as "CR 1234567" does not match the number
    # the client typed. Failing closed is the point: guessing would attribute
    # the message on a value the record does not actually state.
    formatted = CompanyIdentifiers(cr_number="CR 1234567", name_en="Pacific Jade Logistics Limited")
    outcome = resolve(text="Please file this against CR 1234567.", candidates={JADE: formatted})

    assert outcome.resolution is Resolution.NO_MATCH
    assert outcome.company_id is None
    assert outcome.candidates == ()


def test_full_width_digits_are_not_an_identifier_match() -> None:
    # The digits a Chinese IME produces are full-width (U+FF10-U+FF19): digits
    # to a regex, and characters the stored number does not have, so the run
    # is not the identifier. Deliberately not folded to ASCII — folding would
    # widen matching, and the failure direction here is quarantine and staff
    # review.
    outcome = resolve(text="請跟進ＣＲ１２３４５６７的申請", candidates=PORTFOLIO)

    assert outcome.resolution is Resolution.NO_MATCH
    assert outcome.company_id is None
    assert outcome.candidates == ()


def test_context_only_is_never_produced() -> None:
    # ``context_only`` is the M5b model fallback's tier. This rule reports a
    # candidate only when the message itself carries the identifier or the
    # name, so nothing it returns is ever context-only — not even for a
    # message it cannot place at all.
    texts = (
        "We spoke about the filing on the phone.",
        "Bright Lantem Deslgn Limited confirmed.",
        "Bright Lantern Design Limited",
    )

    for text in texts:
        outcome = resolve(text=text, candidates=PORTFOLIO)
        assert all(item.evidence is not MatchEvidence.CONTEXT_ONLY for item in outcome.candidates)


def test_the_resolution_names_the_case_the_decision_is_in() -> None:
    # The invariant a caller reads the field for: only ``resolved`` carries a
    # company, and the two refusals are told apart by the name rather than by
    # the shape of ``candidates`` (a message naming two companies and one
    # naming none are different staff remedies).
    cases = (
        (Resolution.RESOLVED, "Our BRN is 12345678."),
        (Resolution.RESOLVED, "Bright Lantern Design Limited"),
        (Resolution.AMBIGUOUS, "Bright Lantern Design Limited and Pacific Jade Logistics Limited"),
        (Resolution.NO_MATCH, "Please ask the design company to confirm."),
        (Resolution.NO_MATCH, "Bright Lantem Deslgn Limited"),
    )

    for resolution, text in cases:
        outcome = resolve(text=text, candidates=PORTFOLIO)
        assert outcome.resolution is resolution, text
        assert (outcome.company_id is not None) is (resolution is Resolution.RESOLVED), text
        assert (outcome.evidence is not None) is (resolution is Resolution.RESOLVED), text


def test_the_decision_does_not_depend_on_the_mapping_order() -> None:
    text = "Bright Lantern Design Limited and Pacific Jade Logistics Limited both filed late."

    forward = resolve(text=text, candidates={BRIGHT: BRIGHT_LANTERN, JADE: PACIFIC_JADE})
    reverse = resolve(text=text, candidates={JADE: PACIFIC_JADE, BRIGHT: BRIGHT_LANTERN})

    assert forward == reverse
    # Ordered by company id, so repeated runs and the assessment agree.
    assert [item.company_id for item in forward.candidates] == [BRIGHT, JADE]
