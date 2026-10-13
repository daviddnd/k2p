"""The leak canary: no raw client value reaches the outbound snippet.

One red line, stated as a table: for every case the composed, scrubbed and
tokenized snippet — the text the gateway would actually put on the wire —
reads a typed ``[LABEL_nn]`` token where the client's own value stood, and
never the value itself. This is the measured floor of the existing tokenized
mode, **not** an anonymity guarantee: the plan's ruling D5 stands, free text
the local pass does not recognise still transits, and the accurate claim for
this channel is "minimized, typed-token, locally restorable" — never
"anonymous".

The harness mirrors ``app.application.ai_gateway.infer``'s domain stages for
the reply-parsing profile: compose (scrub, fail closed), then tokenize
against the in-office roster. The roster is handed to the tokenizer directly
because the lexicon *is* the names themselves; deriving that lexicon from the
caller's payload is the application seam's business (Task 3), not this file's.
Hermetic by construction: pure domain calls, no network, no database, no API
key, synthetic data throughout.

Every case asserts. The plan's leak-closing tasks flipped each known gap by
deleting its ``gap`` key, and none remains: the canary reports no xfail and
no xpass. The xfail machinery stays wired to a ``gap`` key for the record,
but the table-integrity control now forbids one outright — a future leak has
to be named and closed, never parked as an expected failure.
"""

import re
from collections.abc import Mapping, Sequence

import pytest

from app.domain.ai_gateway import TaskType
from app.domain.redaction import RedactionRefused, compose_snippet, tokenize

# The one company anchor every case composes against: a BRN-shaped value the
# scrubber deliberately leaves alone (it is indistinguishable from a bare
# 8-digit run), so the canary's subject stays the reply text.
COMPANY_ANCHOR = "12345678"

# Ruling D1's closed label vocabulary, as a shape: every expected label must
# be a *typed* token, which is also ruling D2's "type, never shape" — a marker
# carrying a digit count, a magnitude or a first character would fail here.
TOKEN_LABEL_PATTERN = re.compile(
    r"^\[(?:PERSON|COMPANY|BRN|CR|ADDRESS|HKID|PASSPORT|DOB|PHONE|EMAIL|BANK)_\d{2}\]$"
)

# The case-data keys, all strings so the table keeps one shape the later tasks
# extend:
#   reply_text  the raw client reply the snippet is composed from (required)
#   raw_N       one raw client value that must not reach the wire (1..n)
#   label       the typed token the model must read in its place (required)
#   person      the in-office officer name given to the tokenizer (optional)
#   refusal     "allowed" when a fail-closed compose refusal also satisfies the
#               case — nothing transits either way (optional)
#   gap         retired with the T1-T5 gaps: a case carrying one is xfailed and
#               fails the table-integrity control, so no case carries one
CANARY_CASES: tuple[tuple[str, dict[str, str]], ...] = (
    (
        "keyworded_hkid",
        {
            "reply_text": "My HKID is A123456(7), please correct the record.",
            "raw_1": "A123456(7)",
            "label": "[HKID_01]",
        },
    ),
    (
        "keyworded_passport",
        {
            "reply_text": "Passport No.: K12345678 was renewed last year.",
            "raw_1": "K12345678",
            "label": "[PASSPORT_01]",
        },
    ),
    (
        "email_address",
        {
            "reply_text": "Reach me at tester01@example.org if the form is unclear.",
            "raw_1": "tester01@example.org",
            "label": "[EMAIL_01]",
        },
    ),
    (
        "keyworded_dob",
        {
            "reply_text": "Just to confirm, I was born 1985-03-21, as stated.",
            "raw_1": "1985-03-21",
            "label": "[DOB_01]",
        },
    ),
    (
        "keyworded_bank_account",
        {
            "reply_text": "Please send the refund to bank account 87654321012 when ready.",
            "raw_1": "87654321012",
            "label": "[BANK_01]",
        },
    ),
    (
        "hk_english_address_with_district_tail",
        {
            "reply_text": (
                "The office moves to Flat A, 12/F, 88 Test Road, Test District, Kowloon."
            ),
            "raw_1": "88 Test Road",
            "raw_2": "Test District",
            "label": "[ADDRESS_01]",
        },
    ),
    (
        "in_roster_english_officer_name",
        {
            "reply_text": "Chan Tai Man has confirmed the particulars are unchanged.",
            "person": "Chan Tai Man",
            "raw_1": "Chan Tai Man",
            "label": "[PERSON_01]",
        },
    ),
    # --- Review-focus item 1: an identifier written with spaces or full-width
    # digits was the gap; Task 4's detection on the normalized form covers
    # both spellings, so both cases assert.
    (
        "spaced_hkid",
        {
            "reply_text": "My HKID is A 1 2 3 4 5 6 (7), the card is worn.",
            "raw_1": "A 1 2 3 4 5 6 (7)",
            "label": "[HKID_01]",
        },
    ),
    (
        "full_width_mobile_digits",
        {
            "reply_text": "Call me on ９４０４９１２４ after six.",  # noqa: RUF001
            "raw_1": "９４０４９１２４",  # noqa: RUF001
            "label": "[PHONE_01]",
        },
    ),
    # --- Review-focus item 2: the record holds the traditional form and the
    # reply writes the simplified one; Tasks 2-3's script-folded lexicon
    # matching closes it, so the case asserts.
    (
        "simplified_chinese_name",
        {
            "reply_text": "陈大文 has signed the resolution.",
            "person": "陳大文",
            "raw_1": "陈大文",
            "label": "[PERSON_01]",
        },
    ),
    # --- Review-focus item 3: the bare forms the separator/keyword patterns
    # cannot see — a bare 8-digit run in the higher prefixes (4-9), and a bare
    # passport-shaped token — now assert (Task 4).
    (
        "bare_eight_digit_mobile",
        {
            "reply_text": "You can reach me on 94049124 after six.",
            "raw_1": "94049124",
            "label": "[PHONE_01]",
        },
    ),
    (
        # Ruling R-T4-5: the allocated mobile list could not be verified, so
        # the unverifiable prefix class fails toward masking (D6).
        "bare_four_prefix_mobile",
        {
            "reply_text": "You can reach me on 45678901 after six.",
            "raw_1": "45678901",
            "label": "[PHONE_01]",
        },
    ),
    (
        "bare_passport_number",
        {
            "reply_text": "My passport is K12345678, issued in 2019.",
            "raw_1": "K12345678",
            "label": "[PASSPORT_01]",
            "refusal": "allowed",
        },
    ),
    # --- Review-focus item 1 (ruling R-B): phones are contact numbers. The
    # local fixed line carries no keyword-free rule (it is indistinguishable
    # from a date or a BRN), so it is masked on the strength of the contact
    # word before it; the PRC mobile and the explicit international form each
    # carry their own shape and need none.
    (
        "contact_anchored_fixed_line",
        {
            "reply_text": "We changed office; my phone is 23456789, unchanged.",
            "raw_1": "23456789",
            "label": "[PHONE_01]",
        },
    ),
    (
        "bare_prc_mobile",
        {
            "reply_text": "You can reach me on 13812345678 after six.",
            "raw_1": "13812345678",
            "label": "[PHONE_01]",
        },
    ),
    (
        "international_phone",
        {
            "reply_text": "Call me on +86 138 1234 5678 if the form is unclear.",
            "raw_1": "+86 138 1234 5678",
            "label": "[PHONE_01]",
        },
    ),
    # --- Workstream ④ T4: the spaced-trunk international form — the old
    # --- pattern matched only the subscriber tail and left the country and
    # --- area codes on the wire; the optional trunk group closes it.
    (
        "spaced_trunk_international_phone",
        {
            "reply_text": "Please call +86 (0)10 1234 5678 after the meeting.",
            "raw_1": "+86 (0)10 1234 5678",
            # The residual leaked the country code and area code as fragments
            # while masking the subscriber tail — the fragment is its own red
            # line beside the whole-number one.
            "raw_2": "+86 (0)10",
            "label": "[PHONE_01]",
        },
    ),
    # --- Review-focus item 5 (ruling R-C): a long run the in-pattern BANK
    # keyword cannot reach — the account word separated from the number by
    # other words, a transfer verb, and a licensed bank name — is masked
    # through the context rule, while an unanchored long number is the
    # recorded residual (D5) and transits.
    (
        "account_word_separated_from_number",
        {
            "reply_text": "The account on file is 1234567890, please update it.",
            "raw_1": "1234567890",
            "label": "[BANK_01]",
        },
    ),
    (
        "transfer_verb_bank_number",
        {
            "reply_text": "Please 存入 987654321 before Friday.",
            "raw_1": "987654321",
            "label": "[BANK_01]",
        },
    ),
    # --- Workstream ④ T4: the >17-digit residual, closed on the anchored leg.
    # --- The old width bound clipped nothing of an 18+ run — the digit fence
    # --- failed at digit 18 and the whole run transited; an anchored 19-digit
    # --- run now masks whole, while an unanchored run stays the recorded D5
    # --- residual (pinned in the domain suite).
    (
        "anchored_over_long_run",
        {
            "reply_text": "请将款项存入银行户口 12345678901234567890，谢谢。",  # noqa: RUF001
            "raw_1": "12345678901234567890",
            "label": "[BANK_01]",
        },
    ),
    (
        "bank_name_anchored_account",
        {
            "reply_text": "The refund goes to the 匯豐戶口 1234567890, unchanged.",
            "raw_1": "1234567890",
            "label": "[BANK_01]",
        },
    ),
    # --- Review-focus item 4: a village/estate/building/unit address carries
    # no street name and no 號; Task 4's address-family pattern closes it.
    (
        "village_estate_unit_address",
        {
            "reply_text": "The office moves to 太和村丽和楼17A室 from next month.",
            "raw_1": "太和村丽和楼17A室",
            "label": "[ADDRESS_01]",
        },
    ),
    # --- R-A' (the final review): the designator before a recognised marker
    # is a digit OR a single Latin letter, and 座 joins the markers, so
    # letter-unit and 座 addresses the digit-only rule had dropped mask again.
    (
        "village_estate_letter_unit_address",
        {
            "reply_text": "The office moves to 太和村A樓B室 from next month.",
            "raw_1": "太和村A樓B室",
            "label": "[ADDRESS_01]",
        },
    ),
    (
        "estate_seat_marker_address",
        {
            "reply_text": "We shifted the office to 太和邨5座 last week.",
            "raw_1": "太和邨5座",
            "label": "[ADDRESS_01]",
        },
    ),
    # --- Review-focus item 7: the reply names a person who is not in the
    # tenant's records. The canary pins the closable half — a name one edit
    # from a known one is over-tokenized rather than leaked (ruling D6) — since
    # a name with no near match is the residual D5 documents, and Mode T cannot
    # assert it closed.
    (
        "out_of_roster_name_near_match",
        {
            "reply_text": "Our neighbour Chan Tai Mann also signed the minutes.",
            "person": "Chan Tai Man",
            "raw_1": "Chan Tai Mann",
            "label": "[PERSON_01]",
        },
    ),
    # --- Workstream ④ T2 (rulings D2/D3): the simplified-script keyword and
    # --- marker vocabularies. Each case writes the simplified spelling the
    # --- Traditional-only vocabularies used to miss; the prose controls live
    # --- beside the table in the domain suite.
    (
        "simplified_passport",
        {
            "reply_text": "我的护照号码 E1234567 已更新，请更正记录。",  # noqa: RUF001
            "raw_1": "E1234567",
            "label": "[PASSPORT_01]",
        },
    ),
    (
        "simplified_contact_anchored_fixed_line",
        {
            "reply_text": "请致电 23456789 与我联络，其他不变。",  # noqa: RUF001
            "raw_1": "23456789",
            "label": "[PHONE_01]",
        },
    ),
    (
        "simplified_transfer_verb_bank_number",
        {
            "reply_text": "请转账到银行户口 98765432，谢谢。",  # noqa: RUF001
            "raw_1": "98765432",
            "label": "[BANK_01]",
        },
    ),
    (
        "simplified_keyworded_hkid",
        {
            "reply_text": "身份证 A123456(7) 已换领新证。",
            "raw_1": "A123456(7)",
            "label": "[HKID_01]",
        },
    ),
    (
        "simplified_cjk_street_address",
        {
            "reply_text": "办公室迁至深圳市南山区科苑路1号。",
            "raw_1": "深圳市南山区科苑路1号",
            "label": "[ADDRESS_01]",
        },
    ),
    (
        "simplified_kowloon_street_address",
        {
            "reply_text": "仓库在九龙旺角道1号，下月启用。",  # noqa: RUF001
            "raw_1": "旺角道1号",
            "label": "[ADDRESS_01]",
        },
    ),
    # --- Workstream ④ T3 (rulings D4/D5/D6): the address geo-vocabulary is an
    # --- ANCHOR family, never a class — a district/locality/province/country
    # --- name combines with a nearby shape fragment to extend and confirm the
    # --- mask; the name alone masks nothing (the prose controls live beside
    # --- the table in the domain suite).
    (
        "geo_anchored_street_without_hao",
        {
            "reply_text": "新界沙田大圍道12，下月搬。",  # noqa: RUF001
            "raw_1": "沙田大圍道12",
            "label": "[ADDRESS_01]",
        },
    ),
    (
        "geo_mainland_admin_street",
        {
            "reply_text": "搬到北京市海淀区中关村大街27。",
            "raw_1": "海淀区中关村大街27",
            "label": "[ADDRESS_01]",
        },
    ),
    (
        "geo_country_anchored_street",
        {
            "reply_text": "澳洲悉尼喬治街20 有分店。",
            "raw_1": "澳洲悉尼喬治街20",
            "label": "[ADDRESS_01]",
        },
    ),
    (
        "geo_postal_combination",
        {
            "reply_text": "廣東省深圳市 518057 見。",
            "raw_1": "518057",
            "label": "[ADDRESS_01]",
        },
    ),
    # --- Workstream ④ T4: 小区/小區 — the mainland residential-compound token
    # --- the T3 lists did not settle; it joins the building-family marker set
    # --- (the D6 lane), so the family pattern masks 阳光小区3栋 on its own
    # --- shape rather than only through the geo combination.
    (
        "mainland_compound_unit_address",
        {
            "reply_text": "新办公室在阳光小区3栋，下月启用。",  # noqa: RUF001
            "raw_1": "阳光小区3栋",
            "label": "[ADDRESS_01]",
        },
    ),
)


def _wire_snippet(reply_text: str, person: str = "") -> str:
    """The exact outbound text for one reply-parsing case, or a refusal.

    Composes against the fixed BRN anchor and the case's reply text, then
    tokenizes against the in-office roster exactly as the gateway's name
    tokenization does. Raises ``RedactionRefused`` when the compose fails
    closed — nothing transits at all, which is the other acceptable outcome
    (a bare identifier the local pass will not certify) and which the case
    declares with ``refusal``.
    """
    composed = compose_snippet(
        task_type=TaskType.REPLY_PARSING,
        company_anchor=COMPANY_ANCHOR,
        fields={"reply_text": reply_text},
    )
    if not person:
        return composed.text
    return tokenize(composed.text, values={"PERSON": [person]}).text


def assert_no_raw_values(snippet_text: str, raw_values: Sequence[str]) -> None:
    """The red line, in one place: none of these values may reach the wire.

    Later tasks assert the same claim about the same text, so the check lives
    here rather than being re-spelled per caller. The message names the value
    that escaped — synthetic data in every case, and a failure has to say what
    leaked.
    """
    for raw in raw_values:
        assert raw not in snippet_text, (
            f"raw client value {raw!r} reached the outbound snippet: {snippet_text!r}"
        )


def _raw_values(case: Mapping[str, str]) -> tuple[str, ...]:
    """Every ``raw_N`` value the case declares, in declaration order."""
    return tuple(value for key, value in sorted(case.items()) if key.startswith("raw_"))


def _assert_case_holds(case_id: str, case: Mapping[str, str]) -> None:
    """The case's whole claim: raw values stay home, a typed token travels.

    A refusal satisfies a case only where the case allows one — a compose that
    fails closed sends nothing, so it can never be the outcome of a case that
    expects the model to read something.
    """
    try:
        snippet_text = _wire_snippet(case["reply_text"], case.get("person", ""))
    except RedactionRefused as exc:
        assert case.get("refusal") == "allowed", (
            f"{case_id}: the compose refused ({exc}) and the case accepts no refusal"
        )
        return
    assert_no_raw_values(snippet_text, _raw_values(case))
    label = case["label"]
    assert label in snippet_text, f"{case_id}: no {label} in {snippet_text!r}"


@pytest.mark.parametrize(
    ("case_id", "case"),
    [
        pytest.param(
            case_id,
            case,
            id=case_id,
            # The gap wiring, kept for the record: a case carrying a `gap` key
            # would be marked xfail with its reason — deleting the key was how
            # T1-T5 flipped a case to an asserted pass, and the table-integrity
            # control now forbids a new one. `strict=False` keeps an unexpected
            # pass visible as XPASS rather than a failure, so the leak table and
            # the code can be reconciled at a flip.
            marks=(
                ()
                if "gap" not in case
                else (pytest.mark.xfail(reason=case["gap"], strict=False),)
            ),
        )
        for case_id, case in CANARY_CASES
    ],
)
def test_no_raw_value_reaches_the_wire(case_id: str, case: Mapping[str, str]) -> None:
    """Every case in the table, with its gap cases reported as xfail."""
    _assert_case_holds(case_id, case)


def test_the_canary_fails_when_a_raw_value_would_transit() -> None:
    """The harness's own control: a canary that cannot fail measures nothing."""
    with pytest.raises(AssertionError, match="reached the outbound snippet"):
        assert_no_raw_values(
            "company: 12345678\nreply_text: A123456(7)",
            ["A123456(7)"],
        )


def test_the_canary_demands_the_typed_token_not_merely_the_value_s_absence() -> None:
    """Absence is not enough: a snippet that simply dropped the value would
    satisfy the red line while leaving the model unable to say what it read.
    The case claim is both halves, so a missing label fails."""
    with pytest.raises(AssertionError, match="no \\[PASSPORT_01\\]"):
        _assert_case_holds(
            "synthetic_label_absence",
            {
                "reply_text": "My HKID is A123456(7).",
                "raw_1": "A123456(7)",
                "label": "[PASSPORT_01]",
            },
        )


def test_every_expected_label_is_a_typed_token() -> None:
    """Ruling D1's closed vocabulary and D2's "type, never shape", checked over
    the table: a marker that carried a digit count or a first character would
    not match. Task 3 turns this vocabulary into the domain's ``TOKEN_LABELS``,
    at which point the pattern is redundant rather than wrong."""
    for case_id, case in CANARY_CASES:
        assert TOKEN_LABEL_PATTERN.match(case["label"]), case_id


def test_every_case_declares_a_raw_value_and_no_case_carries_a_gap() -> None:
    """Table integrity: a case without a raw value asserts nothing, a
    duplicated case id would make one of the two cases unreportable, and the
    ``gap`` key - the T1-T5 migration aid that marked a known leak xfail with
    the task that closes it - is now forbidden outright. The table holds no
    gap, so this is the stronger invariant: a future stray key fails here
    rather than quietly parking a leak as an expected failure."""
    case_ids = [case_id for case_id, _ in CANARY_CASES]
    assert len(set(case_ids)) == len(case_ids)
    for case_id, case in CANARY_CASES:
        assert case["reply_text"], case_id
        assert _raw_values(case), case_id
        assert "gap" not in case, f"{case_id} carries a gap key: {case['gap']}"


# --- The D7 measurement harness (workstream ④ T5) -----------------------------

# Prose controls: sentences that must pass the compose byte-for-byte — no mask
# and no refusal. Over-tokenization is the slice's named risk (every widened
# vocabulary ships with controls, the plan's surprises say so), so the controls
# live beside the case table the harness measures. Each entry names the ruling
# or task it guards.
PROSE_CONTROLS: tuple[tuple[str, str], ...] = (
    ("geo_country_prose", "我上月去日本出差，下週回港。"),  # noqa: RUF001
    ("geo_province_prose", "廣東省的經濟發展很快。"),
    ("geo_locality_prose", "旺角的街市很熱鬧。"),
    ("measure_word_prose", "上市公司有23座寫字樓。"),
    ("admin_division_prose", "行政區劃調整後生效。"),
    ("compound_prose", "这个小区环境优美，适合居住。"),  # noqa: RUF001
    ("trunk_prose", "The (0) hypothesis is set out below."),
    ("unanchored_overlong_run", "参考编号 12345678901234567890 见附件。"),
    ("village_prose", "本村樓上開會，請勿打擾。"),  # noqa: RUF001
)


def measure_rates() -> tuple[int, int, int, int]:
    """The D7 pair's numerators and denominators over the canary's own tables.

    Returns ``(leaks, case_count, over_masks, control_count)``: a leak is a
    declared raw value present in the case's wire text (a refusal satisfies a
    case — nothing transits — and counts toward neither numerator), and an
    over-mask is a prose control whose wire text differs from the unmasked
    compose or which refuses. Both rates are ``0`` exactly when the numerators
    are ``0``; the test below asserts that, and the printed pair feeds the
    plan's decision log.
    """
    leaks = 0
    for _case_id, case in CANARY_CASES:
        try:
            wire = _wire_snippet(case["reply_text"], case.get("person", ""))
        except RedactionRefused:
            continue  # nothing transits: no leak by construction
        leaks += sum(1 for raw in _raw_values(case) if raw in wire)
    over_masks = 0
    for _control_id, control in PROSE_CONTROLS:
        try:
            wire = _wire_snippet(control)
        except RedactionRefused:
            over_masks += 1
            continue
        expected = f"company: {COMPANY_ANCHOR}\nreply_text: {control}"
        if wire != expected:
            over_masks += 1
    return (leaks, len(CANARY_CASES), over_masks, len(PROSE_CONTROLS))


def test_the_d7_pair_is_zero_on_both_axes() -> None:
    """The measurement harness the D7 ruling asks for, over the planted case
    table plus the prose controls: zero leaks across every case, zero
    over-masks across every control. The printed pair is the number the
    local-detector research (T6) reads its options against, and it goes into
    the plan's decision log."""
    leaks, cases, over_masks, controls = measure_rates()
    print(
        f"D7 pair: leak-rate {leaks}/{cases}, "
        f"over-tokenization-rate {over_masks}/{controls}"
    )
    assert leaks == 0
    assert over_masks == 0


def test_the_harness_counts_a_leak_and_an_over_mask() -> None:
    """The harness's own control, the canary fail-control's twin: a walker that
    cannot count measures nothing. Both failure shapes are injected on copies
    of the tables so the real ones stay untouched."""
    leaked = (("synthetic_leak", {"reply_text": "x", "raw_1": "x", "label": "[HKID_01]"}),)
    original_cases = globals()["CANARY_CASES"]
    try:
        globals()["CANARY_CASES"] = leaked
        assert measure_rates()[0] == 1
        globals()["CANARY_CASES"] = ()
        assert measure_rates()[1] == 0
    finally:
        globals()["CANARY_CASES"] = original_cases
    original_controls = globals()["PROSE_CONTROLS"]
    try:
        globals()["PROSE_CONTROLS"] = (("synthetic_over_mask", "Call 94049124 today."),)
        assert measure_rates()[2] == 1
    finally:
        globals()["PROSE_CONTROLS"] = original_controls
