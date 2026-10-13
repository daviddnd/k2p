"""Redaction-domain tests (the M6 counsel package: Q1.2 redaction bar).

Pure rules, no I/O: the deterministic scrubber's per-pattern cases, the
scrub-then-verify fail-closed cases, the Q1.2 profiles as data (allow/exclusion
tables, tokenize-by-default with the opt-out reason recorded), the compose
invariants (one anchor + one subject, field-by-field allow-list, the 16 KiB
cap, the ordinance-QA no-client-content rule), the tokenize/restore round trip,
and the Q1.5 digest (≤500-char normalized prefix + SHA-256 of the exact
outbound snippet). Every snippet is synthetic; no real person's data appears.
"""

import hashlib

import pytest

from app.domain.ai_gateway import TaskType
from app.domain.redaction import (
    CJK_VARIANTS,
    CONTACT_WINDOW_CHARS,
    DIGEST_MAX_CHARS,
    EXCLUDED_FIELDS,
    PROFILES,
    SNIPPET_MAX_BYTES,
    TOKEN_LABELS,
    RedactionProfile,
    RedactionRefused,
    ScrubbedText,
    SnippetDigest,
    compose_snippet,
    normalize_for_matching,
    restore_tokens,
    scrub_free_text,
    scrub_text,
    snippet_digest,
    tokenize,
)

# --- Normalization for lexicon matching ---


def test_normalize_folds_width_case_and_whitespace() -> None:
    assert normalize_for_matching("Ａ １２３") == "a 123"  # noqa: RUF001


def test_normalize_folds_simplified_to_traditional_record_form() -> None:
    assert normalize_for_matching("陈大文") == normalize_for_matching("陳大文")


def test_normalize_collapses_whitespace_runs_including_the_ideographic_space() -> None:
    """NFKC folds U+3000 to a space, so all whitespace shapes end up in the
    same normalized form: a reply that separates words with an ideographic
    space still matches a lexicon value written with an ASCII one."""
    assert normalize_for_matching("陳大文 \t\n  signed　now") == "陳大文 signed now"


def test_the_variant_map_is_bounded_and_character_wise() -> None:
    """The table is bounded on purpose (the plan's global constraint 1: no
    CJK conversion dependency is approved), so it stays a small
    simplified→traditional pair list the tests exercise and nothing else. A
    real full-coverage traditional↔simplified engine is a dependency
    decision, not a data edit; single-character pairs are what makes the
    character-wise application in ``normalize_for_matching`` total."""
    assert CJK_VARIANTS["陈"] == "陳"
    assert len(CJK_VARIANTS) <= 32
    for simplified, traditional in CJK_VARIANTS.items():
        assert len(simplified) == 1, simplified
        assert len(traditional) == 1, traditional
        assert simplified != traditional


# --- Scrubber: one deterministic case per pattern family ---

SCRUB_CASES = [
    # HKID, old one-letter format with parenthesised check digit.
    ("My HKID is A123456(7), thanks.", "[HKID_01]"),
    # HKID, two-letter format.
    ("New style AB123456(1) please.", "[HKID_01]"),
    # Passport, English label.
    ("Passport No.: X1234567", "[PASSPORT_01]"),
    # Passport, Chinese label.
    ("護照號碼：X1234567", "[PASSPORT_01]"),  # noqa: RUF001
    # Date of birth, English label.
    ("date of birth: 1985-03-21", "[DOB_01]"),
    # Date of birth, Chinese label.
    ("出生日期：1985年3月21日", "[DOB_01]"),  # noqa: RUF001
    # Bank account, English label.
    ("account number: 87654321012", "[BANK_01]"),
    # Bank account, Chinese label.
    ("戶口：87654321012", "[BANK_01]"),  # noqa: RUF001
    # HK phone, separated local form.
    ("Call me at 9123 4567.", "[PHONE_01]"),
    # HK phone, hyphenated with the 852 prefix.
    ("Call +852-9123-4567 now", "[PHONE_01]"),
    # Email address.
    ("reach me at tester01@example.org today", "[EMAIL_01]"),
    # Residential address, English full shape with district tail.
    (
        "Flat A, 12/F, 88 Test Road, Test District, Kowloon",
        "[ADDRESS_01]",
    ),
    # Residential address, Chinese shape with district head.
    (
        "新界沙田測試街12號3樓302室。",
        "[ADDRESS_01]",
    ),
]


@pytest.mark.parametrize(("text", "marker"), SCRUB_CASES, ids=[text for text, _ in SCRUB_CASES])
def test_scrub_replaces_each_pattern_family(text: str, marker: str) -> None:
    scrubbed = scrub_free_text(text)
    assert marker in scrubbed
    # The protected value itself is gone from the outbound text.
    for fragment in ("A123456(7)", "AB123456(1)", "X1234567", "1985-03-21", "87654321012",
                     "9123", "tester01@example.org", "Test Road", "測試街12號"):
        assert fragment not in scrubbed


def test_scrub_leaves_ordinary_prose_and_identifiers_untouched() -> None:
    """The BRN-shaped company anchor and ordinary prose must survive: a bare
    8-digit run in the low prefixes (1-3) is left alone so date- and
    year-shaped runs are not shredded. A bare run in the higher prefixes
    (4-9) is a phone number (Task 4's bare-mobile case; ruling R-T4-5 makes
    the unverifiable prefix class fail toward masking)."""
    text = "Please confirm the change for BRN 22334455. The director resigned on 2026-09-30."
    assert scrub_free_text(text) == text


def test_scrub_reports_every_redaction_with_a_stable_marker() -> None:
    text = "HKID A123456(7), backup phone 9123 4567, email tester01@example.org"
    scrubbed = scrub_free_text(text)
    assert "[HKID_01]" in scrubbed
    assert "[PHONE_01]" in scrubbed
    assert "[EMAIL_01]" in scrubbed
    # Re-scrubbing the scrubbed text is idempotent: markers never match.
    assert scrub_free_text(scrubbed) == scrubbed


# --- Scrubber: the mask is reversible (ruling D3) ---


def test_scrub_text_returns_the_table_and_scrub_free_text_wraps_it() -> None:
    """D3's shape: the scrubber returns ``ScrubbedText(text, tokens)`` — what
    each marker replaced, so the mask can be undone locally — and the
    pre-existing ``scrub_free_text`` stays as the text-only wrapper its
    callers already use."""
    scrubbed = scrub_text("My HKID is A123456(7), thanks.")
    assert isinstance(scrubbed, ScrubbedText)
    assert scrubbed.text == "My HKID is [HKID_01], thanks."
    assert scrubbed.tokens == {"[HKID_01]": "A123456(7)"}
    assert scrub_free_text("My HKID is A123456(7), thanks.") == scrubbed.text


def test_scrub_text_records_each_marker_once_with_its_own_match() -> None:
    """Every marker in the text has an entry, and the entries are the exact
    matched spans: two identifiers of one class number their own markers."""
    scrubbed = scrub_text("HKID A123456(7) or AB123456(1), and email tester01@example.org")
    assert scrubbed.tokens == {
        "[HKID_01]": "A123456(7)",
        "[HKID_02]": "AB123456(1)",
        "[EMAIL_01]": "tester01@example.org",
    }
    for marker, value in scrubbed.tokens.items():
        assert marker in scrubbed.text
        assert value not in scrubbed.text


def test_scrub_text_leaves_an_unmatched_text_with_an_empty_table() -> None:
    scrubbed = scrub_text("Confirmed, proceed.")
    assert scrubbed.text == "Confirmed, proceed."
    assert scrubbed.tokens == {}


# --- Scrubber: fail closed when a protected-class shape cannot be scrubbed ---


def test_fail_closed_on_labelled_hkid_without_check_digit() -> None:
    """A client who abbreviates their HKID leaves a body the scrubber will
    not guess at: the verify pass flags the residual shape and the call
    fails closed rather than sending a near-complete ID number."""
    with pytest.raises(RedactionRefused, match="hkid"):
        scrub_free_text("My HKID is A123456, I will quote the check digit later.")


def test_fail_closed_on_partial_residential_address() -> None:
    """An address fragment the address pattern cannot consume whole (no
    street/district shape to replace) is refused, not guessed at."""
    with pytest.raises(RedactionRefused, match="address"):
        scrub_free_text("Address: Room 12, Test Tower")


def test_full_address_with_label_is_scrubbed_not_refused() -> None:
    """The label alone in front of a full address is fine: the address
    pattern consumes the shape, the verify pass finds no residual."""
    scrubbed = scrub_free_text("Address: Flat A, 12/F, 88 Test Road, Test District, Kowloon")
    assert "[ADDRESS_01]" in scrubbed


def test_scrub_covers_hk_island_district_tails() -> None:
    """Fix round 1 (review finding: English address pattern needed a
    district tail): number+street with an HK Island tail transited intact
    because only Kowloon/HK/NT closed the pattern. Both demonstrated shapes
    scrub now."""
    for text in (
        "88 Test Road, Mid-Levels",
        "12 Smithfield Road, Central",
    ):
        assert "[ADDRESS_01]" in scrub_free_text(text)


def test_registered_office_style_business_address_still_scrubs() -> None:
    """The expanded tails must consume the registered-office shape whole —
    floor, tower name, number+street, Island district — so the verify pass
    has no residue: scrubbed, not refused."""
    scrubbed = scrub_free_text("Suite 1201, 12/F, One IFC, 1 Harbour View Street, Central")
    assert "[ADDRESS_01]" in scrubbed


def test_untailled_number_and_street_shape_fails_closed() -> None:
    """Fix round 1 (review finding): a number+street with no recognizable
    HK district tail was neither scrubbed nor refused. Design call: refuse —
    the shape is address-class and the scrubber cannot certify it, and the
    documented cost asymmetry favours refusal (false refusals route to
    staff; false sends route to the PCPD). A foreign-looking address is
    therefore refused, not passed."""
    with pytest.raises(RedactionRefused, match="street_address"):
        scrub_free_text("Level 5, 88 Market Street, Melbourne")


def test_number_and_street_words_in_ordinary_prose_do_not_trip_the_detector() -> None:
    """Guard against the verify detector eating ordinary prose: with no
    digit+street shape present the text passes through unchanged, and the
    street keyword inside a hyphenated verb does not count."""
    text = "12 colleagues confirmed; venue is the 5th floor lounge. 88 road-tested units shipped."
    assert scrub_free_text(text) == text


# --- Scrubber: the bare and obfuscated identifier forms (review-focus 1-4) ---


def test_bare_eight_digit_mobile_is_masked() -> None:
    """Review-focus item 3: a bare HK mobile number carries no separator and
    no keyword, so the phone pattern misses it. A bare run in the higher
    prefixes (4-9) is a phone number — ruling R-T4-5 masks the unverifiable
    prefix class under D6 — while the low prefixes stay untouched (the prose
    test above)."""
    scrubbed = scrub_free_text("You can reach me on 94049124 after six.")
    assert "[PHONE_01]" in scrubbed
    assert "94049124" not in scrubbed


def test_date_shaped_eight_digit_run_is_spared() -> None:
    """R-T4-5's other half: the bare-8-digit phone detector starts at prefix 4,
    so a contiguous date- or year-shaped run (prefixes 1-3) passes through
    unmasked while the same shape in a mobile prefix is masked."""
    assert scrub_free_text("20261006") == "20261006"
    assert scrub_free_text("19991231") == "19991231"
    assert scrub_free_text("45678901") == "[PHONE_01]"


def test_full_width_mobile_digits_are_masked() -> None:
    """Review-focus item 1: NFKC width folding runs for detection, so a reply
    whose digits are full-width is recognised as the same number."""
    scrubbed = scrub_free_text("Call me on ９４０４９１２４ after six.")  # noqa: RUF001
    assert "[PHONE_01]" in scrubbed
    assert "９４０４９１２４" not in scrubbed  # noqa: RUF001


# --- Scrubber: phones are contact numbers, not mobiles (ruling R-B) ---


def test_bare_fixed_line_transits_without_a_keyword() -> None:
    """A bare eight-digit run in the low prefixes (2-3) is indistinguishable
    from a date or a BRN, so on its own it is not a phone — the contact
    keyword is what makes it one. The run keeps its shape, which is also what
    keeps a BRN-shaped company anchor intact."""
    assert scrub_free_text("23456789") == "23456789"
    assert scrub_free_text("29881234 and 34567890") == "29881234 and 34567890"


def test_fixed_line_after_a_contact_word_is_masked() -> None:
    """The same run *is* a phone when a contact word precedes it within
    ``CONTACT_WINDOW_CHARS``. Python's ``re`` cannot express a variable-width
    keyword lookbehind, so this shape is the context rule rather than a
    ``_SCRUB_PATTERNS`` entry — and its mask is reversible like every other
    (ruling D3)."""
    scrubbed = scrub_text("我的電話 23456789，請更新。")  # noqa: RUF001
    assert scrubbed.text == "我的電話 [PHONE_01]，請更新。"  # noqa: RUF001
    assert scrubbed.tokens == {"[PHONE_01]": "23456789"}
    assert scrub_free_text("Please call me on 23456789 today.") == (
        "Please call me on [PHONE_01] today."
    )


def test_a_contact_word_further_back_than_the_window_does_not_mask() -> None:
    """The window is bounded: a keyword far from the run does not turn every
    later number into a phone. ``CONTACT_WINDOW_CHARS`` is that bound, counted
    in the normalized comparison form."""
    text = "call " + "-" * CONTACT_WINDOW_CHARS + "23456789"
    assert scrub_free_text(text) == text


def test_ascii_contact_words_are_word_bounded() -> None:
    """The ASCII anchor alternatives carry word boundaries so a substring is
    not a contact word: ``tel`` inside ``hotel``, ``mobile`` inside
    ``automobile`` and ``office`` inside ``officer`` no longer turn a nearby
    bare run into a phone. The CJK alternatives keep no boundary — CJK words
    are not space-delimited — so ``電話`` still anchors."""
    assert scrub_free_text("hotel 23456789") == "hotel 23456789"
    assert scrub_free_text("automobile 23456789") == "automobile 23456789"
    assert scrub_free_text("officer 23456789") == "officer 23456789"
    assert scrub_free_text("電話 23456789") == "電話 [PHONE_01]"


def test_bare_prc_mobile_is_masked() -> None:
    """R-B's second shape: a bare PRC mobile is eleven digits — ``1[3-9]``
    plus nine more — a shape no date or BRN shares, so it needs no keyword."""
    scrubbed = scrub_text("You can reach me on 13812345678")
    assert scrubbed.text == "You can reach me on [PHONE_01]"
    assert scrubbed.tokens == {"[PHONE_01]": "13812345678"}


def test_prc_mobile_in_international_form_is_masked() -> None:
    """The same number written with its country code: the explicit
    international form carries its own evidence, so it needs no keyword."""
    scrubbed = scrub_text("Call +86 138 1234 5678 now")
    assert scrubbed.text == "Call [PHONE_01] now"
    assert scrubbed.tokens == {"[PHONE_01]": "+86 138 1234 5678"}


def test_international_form_is_masked() -> None:
    """R-B's first shape in order of confidence, and the one that has to claim
    its whole span: the local 4-4 split is a *sub-shape* of a grouped
    international tail, so the international rule is ordered ahead of it and
    the country code does not survive as a fragment. The ``00`` trunk prefix
    sits against its country code (the ruling's shape); a spaced ``00 44`` is
    the same number written in a form the pattern does not claim."""
    assert scrub_free_text("Reach the office on +1 415 555 0132 today.") == (
        "Reach the office on [PHONE_01] today."
    )
    assert scrub_free_text("Dial 0044 20 7946 0958 from Hong Kong.") == (
        "Dial [PHONE_01] from Hong Kong."
    )

def test_spaced_trunk_international_form_masks_whole() -> None:
    """Workstream ④ T4 closes the spaced-trunk residual: the international
    pattern did not admit the parenthesized-zero trunk break common in
    written mainland forms ("+86 (0)10 …"), so it matched only the
    subscriber tail and left the country code and area code on the wire. The
    optional trunk group admits the break and the whole number masks as one
    span. The admission is guarded: a parenthesized zero without the ``+``/
    ``00`` lead is not a phone shape and passes untouched, and the digit
    fences on both ends are unchanged."""
    scrubbed = scrub_text("Call me on +86 (0)10 1234 5678 tomorrow.")
    assert scrubbed.text == "Call me on [PHONE_01] tomorrow."
    assert scrubbed.tokens == {"[PHONE_01]": "+86 (0)10 1234 5678"}
    assert scrub_free_text("The (0) hypothesis is set out below.") == (
        "The (0) hypothesis is set out below."
    )
    assert scrub_free_text("請致電 (0) 再試一次。") == "請致電 (0) 再試一次。"


def test_date_and_brn_shaped_runs_are_not_phones() -> None:
    """R-B's guard rails: the low prefixes (1-3) stay out of the keyword-free
    phone rules, so a date- or year-shaped run and a BRN-shaped run are not
    shredded — while the PRC rule needs eleven digits and the international
    rule its own ``+``/``00`` syntax."""
    assert scrub_free_text("20261006") == "20261006"
    assert scrub_free_text("12345678") == "12345678"


def test_hkid_with_spaces_is_masked() -> None:
    """Review-focus item 1: an HKID written with spaces between its digits
    defeats the contiguous pattern; detection on the normalized form admits
    the spacing while the original span is what gets replaced."""
    scrubbed = scrub_free_text("My HKID is A 1 2 3 4 5 6 (7), the card is worn.")
    assert "[HKID_01]" in scrubbed
    assert "A 1 2 3 4 5 6 (7)" not in scrubbed


def test_full_width_hkid_is_masked() -> None:
    """Review-focus item 1, the other obfuscation: full-width letters, digits
    and parentheses (NFKC folds them all) are recognised and replaced."""
    scrubbed = scrub_free_text("My HKID is Ａ１２３４５６（７）.")  # noqa: RUF001
    assert "[HKID_01]" in scrubbed
    assert "Ａ１２３４５６（７）" not in scrubbed  # noqa: RUF001


def test_bare_passport_number_is_refused_or_masked() -> None:
    """Review-focus item 3: the passport pattern needs its keyword adjacent to
    the number, so 'passport is K12345678' slips past it. A bare
    passport-shaped token (letters plus the long digit run a six-digit HKID
    body cannot have) is masked rather than sent."""
    scrubbed = scrub_free_text("My passport is K12345678, issued in 2019.")
    assert "[PASSPORT_01]" in scrubbed
    assert "K12345678" not in scrubbed


def test_village_estate_building_unit_address_is_masked() -> None:
    """Review-focus item 4: a village/estate/building address carries no
    street name and no 號, so neither address pattern consumes it. The whole
    village+building+unit shape is replaced, not its tail."""
    scrubbed = scrub_free_text("The office moves to 太和村丽和楼17A室 from next month.")
    assert "[ADDRESS_01]" in scrubbed
    assert "太和村丽和楼17A室" not in scrubbed
    assert "17A室" not in scrubbed


def test_cjk_family_residual_without_a_designator_transits() -> None:
    """R-A' (the final review) shrinks the residual R-A left. A family-token
    shape with **no** designator before a recognised marker is not an address
    and passes untouched: a bare building word (``太和村``), an unrecognised
    marker (``大廈A臺`` — the marker set is 室|單位|樓|號|座), or a family token
    whose recognised marker (``室``) carries no designator before it
    (``大廈室``). The designator-bearing fragment the
    old pin held (``大廈3室``) is now certified and masked — see
    ``test_cjk_letter_unit_and_seat_addresses_are_masked``."""
    for text in ("太和村", "大廈A臺", "大廈室"):
        assert scrub_free_text(text) == text


# --- Scrubber: a CJK address needs a number (ruling R-A) ---


def test_cjk_building_address_with_a_unit_number_is_masked() -> None:
    """R-A's positive half: the estate/building family is an address once the
    span carries the unit number (17A here), so it is masked and the mask is
    reversible like every other (ruling D3)."""
    scrubbed = scrub_text("The office moves to 太和村丽和楼17A室 from next month.")
    assert scrubbed.text == "The office moves to [ADDRESS_01] from next month."
    assert scrubbed.tokens == {"[ADDRESS_01]": "太和村丽和楼17A室"}


def test_cjk_letter_unit_and_seat_addresses_are_masked() -> None:
    """R-A' (the final review): R-A's digit-only requirement also dropped
    plausible HK addresses whose unit is a letter or whose marker is 座 — the
    pre-slice code caught several of them. The family rule's designator is a
    digit OR a single Latin letter, and 座 is a recognised marker, so each of
    these masks as the exact span and stays reversible (ruling D3)."""
    for text in (
        "太和村丽和楼A室",
        "太和村A樓B室",
        "村屋A室",
        "大廈B單位",
        "太和邨5座",
    ):
        scrubbed = scrub_text(text)
        assert scrubbed.text == "[ADDRESS_01]", text
        assert scrubbed.tokens == {"[ADDRESS_01]": text}, text


def test_bare_floor_and_unit_fragment_is_masked() -> None:
    """R-A: a bare floor-plus-unit fragment carries its own numbers (the floor
    and the unit), so it is address-class and masked rather than transiting —
    the previous slice left ``3樓302室`` in the clear. The Latin ``F`` floor
    and the simplified unit markers are the same shape."""
    assert scrub_free_text("3樓302室") == "[ADDRESS_01]"
    assert scrub_free_text("3/F 302室") == "[ADDRESS_01]"
    assert scrub_free_text("3楼302号") == "[ADDRESS_01]"


def test_cjk_prose_without_digits_is_untouched() -> None:
    """R-A overturns the previous slice's R-T4-6 over-scrub: a CJK address
    needs a number, so ordinary prose carrying the family characters with no
    digits is neither masked nor refused (the founder's directive —
    ``本村樓上開會`` must pass)."""
    assert scrub_free_text("本村樓上開會") == "本村樓上開會"


def test_cjk_prose_with_a_village_word_is_not_refused() -> None:
    """The other former over-scrub pin: with no digit in the span the
    fail-closed detector no longer fires, so staff keep the automation on
    ordinary prose rather than being routed every time."""
    assert scrub_free_text("村口樓下有巴士") == "村口樓下有巴士"


def test_mainland_compound_token_masks_with_designator_and_unit() -> None:
    """Workstream ④ T4: 小区/小區 joins the building-family marker set (the
    token T3's settled lists did not carry), in both scripts and every lane
    that names the family — the R-A' replacer, the verify detector's mirror,
    and the geo combination's family fragment. With a designator and unit the
    compound address masks on its own shape; without them it is ordinary
    prose and passes untouched."""
    scrubbed = scrub_text("我们阳光小区3栋见。")
    # The named lead absorbs up to its run cap of preceding CJK prose — the
    # documented family behaviour — so the pins bind the token's exact span
    # and assert the raw value's absence rather than a hand-partitioned one.
    assert "阳光小区3栋" not in scrubbed.text
    assert scrubbed.tokens == {"[ADDRESS_01]": "我们阳光小区3栋"}
    traditional = scrub_text("我們住在陽光小區3棟。")
    assert "陽光小區3棟" not in traditional.text
    assert traditional.tokens == {"[ADDRESS_01]": "我們住在陽光小區3棟"}
    # The prose controls: no designator+unit, no mask and no refusal.
    assert scrub_free_text("这个小区环境优美，适合居住。") == "这个小区环境优美，适合居住。"  # noqa: RUF001
    assert scrub_free_text("我們小區在沙田，很近。") == "我們小區在沙田，很近。"  # noqa: RUF001


def test_building_word_prose_is_untouched() -> None:
    """The building word alone is not an address: no number in the span means
    no mask and no refusal."""
    assert scrub_free_text("大廈管理費已繳") == "大廈管理費已繳"


# --- Scrubber: context-anchored long numbers (ruling R-C) ---


def test_anchored_account_number_is_masked() -> None:
    """R-C's account-word anchor. ``匯豐戶口 1234567890`` keeps the mask the
    keyword-in-pattern BANK rule already gives it (no regression); a run that
    keyword cannot reach — the account word and the number separated by other
    words — is masked by the context rule instead, and the mask is reversible
    like every other (ruling D3)."""
    adjacent = scrub_text("匯豐戶口 1234567890")
    assert adjacent.text == "匯豐[BANK_01]"
    assert adjacent.tokens == {"[BANK_01]": "戶口 1234567890"}
    separated = scrub_text("戶口 是 1234567890")
    assert separated.text == "戶口 是 [BANK_01]"
    assert separated.tokens == {"[BANK_01]": "1234567890"}


def test_transfer_verb_anchors_a_long_number() -> None:
    """R-C's transfer-verb anchor: ``存入`` carries no account word, but a verb
    that moves money to an account marks the long run after it as a bank
    number, and the mask is reversible (ruling D3)."""
    scrubbed = scrub_text("請存入 987654321")
    assert scrubbed.text == "請存入 [BANK_01]"
    assert scrubbed.tokens == {"[BANK_01]": "987654321"}


def test_bank_name_alone_does_not_mask_far_numbers() -> None:
    """R-C's window bound, over the bank-name anchor. A licensed bank name is
    an anchor, so a number beside it is masked (``匯豐 1234567890``); a number
    further back than ``CONTACT_WINDOW_CHARS`` — the window T1's contact rule
    established, reused here — is beyond its reach and keeps its shape.
    Anchoring is a rule, not a licence to mask every long number."""
    assert scrub_free_text("匯豐 1234567890") == "匯豐 [BANK_01]"
    far = "匯豐 " + "-" * CONTACT_WINDOW_CHARS + "987654321"
    assert scrub_free_text(far) == far


def test_ascii_number_anchors_are_word_bounded() -> None:
    """The T1 word-boundary lesson carries to R-C's ASCII anchors: ``account``
    is the word, not the substring of ``accountant``, and ``BEA`` is not the
    start of ``beauty`` — neither turns a nearby run into a bank number. The
    CJK alternatives keep no boundary (CJK prose is not space-delimited), so
    ``匯豐`` still anchors."""
    assert scrub_free_text("accountant 987654321") == "accountant 987654321"
    assert scrub_free_text("beauty 987654321") == "beauty 987654321"
    assert scrub_free_text("匯豐 987654321") == "匯豐 [BANK_01]"


def test_phone_bank_label_precedence_is_pinned() -> None:
    """Workstream ④ T4, the PHONE/BANK label asymmetry — pinned, and recorded
    as a residual by construction. When both anchor families qualify within
    their windows ("call HSBC on 23456789" — a contact word and a licensed
    bank name before one fixed-line-shaped run), the PHONE rule claims the
    run first: ``_CONTEXT_RULES`` runs the more specific eight-digit shape
    ahead of the wider long-number rule, and a context rule never re-reads a
    span an earlier rule claimed. The label is the only asymmetry — and it
    is cosmetic: ruling D2's label is "type, never shape", model-facing
    metadata, while the restore table binds the exact span, so ``[PHONE_01]``
    and the ``[BANK_01]`` the second rule would have written restore to the
    same text. No rule change settles which kind an ambiguous number is —
    only the client knows — so the precedence is pinned here and the
    imperfect label stands as the recorded residual."""
    scrubbed = scrub_text("call HSBC on 23456789 please")
    assert scrubbed.text == "call HSBC on [PHONE_01] please"
    assert scrubbed.tokens == {"[PHONE_01]": "23456789"}
    # The single-anchor cases label perfectly — the asymmetry needs BOTH
    # anchors, and only then is the label (never the value) in question.
    bank_only = scrub_text("匯豐 23456789")
    assert bank_only.text == "匯豐 [BANK_01]"
    phone_only = scrub_text("call 23456789")
    assert phone_only.text == "call [PHONE_01]"


def test_unanchored_long_number_still_transits() -> None:
    """R-C's recorded residual: with no anchor word before it, a bare long run
    is not a bank number and keeps its shape — the rule masks what context
    certifies, never every long number. This is recorded in the contract's
    limits list, not fixed here. Workstream ④ T4 extends the shape to 18+
    digits: the run's *length* is not the evidence, the anchor is, so an
    unanchored 19-digit run transits exactly like a 9-digit one."""
    assert scrub_free_text("987654321") == "987654321"
    assert scrub_free_text("reference 987654321") == "reference 987654321"
    assert scrub_free_text("参考编号 12345678901234567890 见附件") == (
        "参考编号 12345678901234567890 见附件"
    )


def test_an_anchored_over_long_run_masks_whole() -> None:
    """Workstream ④ T4 closes the >17-digit residual on the anchored leg: an
    anchored run of 18+ digits was clipped by the old ``\d{8,17}`` bound and
    transited whole — the fence failed at digit 18, so no sub-run matched.
    The bound is a floor, not a ceiling: once an account word certifies the
    run, its length is the bank's own choice and the whole run masks."""
    scrubbed = scrub_free_text("请转账到户口 12345678901234567890，谢谢。")  # noqa: RUF001
    assert "[BANK_01]" in scrubbed
    assert "12345678901234567890" not in scrubbed
    english = scrub_free_text("Settle to the account 98765432109876543210 today.")
    assert "[BANK_01]" in english
    assert "98765432109876543210" not in english


# --- The Q1.2 profiles as data ---


def test_every_task_type_has_a_profile() -> None:
    assert set(PROFILES) == set(TaskType)


def test_the_profile_table_is_q12s() -> None:
    """Pinned field by field against Q1.2's table: the allow-list is the
    table's minimum-content column plus the conditional column, and the
    hard exclusions appear in no profile at all."""
    assert PROFILES[TaskType.REPLY_PARSING].allowed_fields == frozenset(
        {"reply_text", "role_reference", "officer_name"}
    )
    assert PROFILES[TaskType.CONFIRMATION_REQUEST].allowed_fields == frozenset(
        {"field_label", "current_value", "ambiguous_field", "named_person"}
    )
    assert PROFILES[TaskType.DOCUMENT_EXTRACTION].allowed_fields == frozenset(
        {"document_text", "field_region", "officer_name_as_filed"}
    )
    assert PROFILES[TaskType.ORDINANCE_QA].allowed_fields == frozenset()


def test_hard_exclusions_are_in_no_profile() -> None:
    """Q1.2's hard exclusions — full identifiers, addresses, DoB, contact
    and bank identifiers — never appear in any allow-list; the
    name-plus-partial-identifier combination is excluded with them."""
    assert frozenset(
        {
            "hkid_full",
            "passport_full",
            "hkid_partial",
            "passport_partial",
            "residential_address",
            "date_of_birth",
            "phone_number",
            "email_address",
            "bank_account",
        }
    ) == EXCLUDED_FIELDS
    for profile in PROFILES.values():
        assert profile.allowed_fields.isdisjoint(EXCLUDED_FIELDS)


def test_tokenize_by_default_opt_out_reason_is_recorded_in_the_profile() -> None:
    """Cross-cutting rule 3 as data: tokenization is the default, and the
    two profiles that opt out carry their recorded reason in the profile
    itself — an informal allowance has nowhere to live."""
    assert PROFILES[TaskType.REPLY_PARSING].tokenize_names is True
    assert PROFILES[TaskType.REPLY_PARSING].tokenize_opt_out_reason is None
    assert PROFILES[TaskType.CONFIRMATION_REQUEST].tokenize_names is False
    assert PROFILES[TaskType.CONFIRMATION_REQUEST].tokenize_opt_out_reason
    assert PROFILES[TaskType.DOCUMENT_EXTRACTION].tokenize_names is False
    assert PROFILES[TaskType.DOCUMENT_EXTRACTION].tokenize_opt_out_reason
    for profile in PROFILES.values():
        if profile.tokenize_names:
            assert profile.tokenize_opt_out_reason is None
        else:
            assert profile.tokenize_opt_out_reason


def test_only_ordinance_qa_forbids_client_content() -> None:
    assert PROFILES[TaskType.ORDINANCE_QA].allows_client_content is False
    for task_type in (
        TaskType.REPLY_PARSING,
        TaskType.CONFIRMATION_REQUEST,
        TaskType.DOCUMENT_EXTRACTION,
    ):
        assert PROFILES[task_type].allows_client_content is True


# --- Composer invariants ---


def test_compose_builds_the_unit_of_analysis() -> None:
    """One company anchor, one subject, fields sorted: the composed snippet
    is the unit the scrubber and digest operate on."""
    composed = compose_snippet(
        task_type=TaskType.REPLY_PARSING,
        company_anchor="12345678",
        fields={"reply_text": "Confirmed, proceed.", "role_reference": "director"},
    )
    assert composed.task_type is TaskType.REPLY_PARSING
    assert composed.company_anchor == "12345678"
    assert composed.text == (
        "company: 12345678\nreply_text: Confirmed, proceed.\nrole_reference: director"
    )


def test_compose_refuses_a_field_outside_the_profile_allow_list() -> None:
    with pytest.raises(RedactionRefused, match="hkid_full"):
        compose_snippet(
            task_type=TaskType.REPLY_PARSING,
            company_anchor="12345678",
            fields={"hkid_full": "A123456(7)"},
        )


def test_compose_refuses_client_content_for_ordinance_qa() -> None:
    """Q1.2's last row: this task type needs no client content and the
    gateway sends none — not even the company anchor."""
    with pytest.raises(RedactionRefused, match="client_content"):
        compose_snippet(
            task_type=TaskType.ORDINANCE_QA,
            company_anchor="12345678",
            fields={"reply_text": "anything"},
        )


def test_compose_refuses_an_anchor_for_ordinance_qa() -> None:
    with pytest.raises(RedactionRefused, match="client_content"):
        compose_snippet(
            task_type=TaskType.ORDINANCE_QA,
            company_anchor="12345678",
            fields={},
        )


def test_compose_requires_the_company_anchor_and_a_subject() -> None:
    with pytest.raises(RedactionRefused, match="company_anchor"):
        compose_snippet(
            task_type=TaskType.REPLY_PARSING,
            company_anchor="",
            fields={"reply_text": "Confirmed."},
        )
    with pytest.raises(RedactionRefused, match="fields"):
        compose_snippet(
            task_type=TaskType.REPLY_PARSING,
            company_anchor="12345678",
            fields={},
        )


def test_compose_scrubs_free_text_before_it_is_snippet_text() -> None:
    """Cross-cutting rule 2 inside the composer: a volunteered HKID in the
    reply field is scrubbed before the snippet exists; an unscrubbable one
    fails the compose closed."""
    composed = compose_snippet(
        task_type=TaskType.REPLY_PARSING,
        company_anchor="12345678",
        fields={"reply_text": "My HKID is A123456(7), please update."},
    )
    assert "[HKID_01]" in composed.text
    with pytest.raises(RedactionRefused):
        compose_snippet(
            task_type=TaskType.REPLY_PARSING,
            company_anchor="12345678",
            fields={"reply_text": "My HKID is A123456, check digit to follow."},
        )


def test_compose_scrubs_the_company_anchor() -> None:
    """Fix round 1 (review finding: unscrubbed anchor was a leak path): the
    anchor is caller-controlled free text that lands verbatim in the wire
    payload and the digest input. A protected-class address passed as the
    anchor is scrubbed exactly like a field value, and the composed snippet
    carries the marker, not the address."""
    composed = compose_snippet(
        task_type=TaskType.REPLY_PARSING,
        company_anchor="Flat A, 12/F, 88 Test Road, Test District, Kowloon",
        fields={"reply_text": "Confirmed, proceed."},
    )
    assert composed.company_anchor == "[ADDRESS_01]"
    assert composed.text.startswith("company: [ADDRESS_01]")
    assert "Test Road" not in composed.text


# --- Composer: the table rides with the snippet (rulings D3/D4) ---


def test_a_masked_identifier_is_recoverable() -> None:
    """Ruling D3: what the scrubber masks is not discarded. The composed
    snippet carries the request-scoped map from each marker back to the exact
    value it replaced, so the model can reference the marker and the answer is
    re-identified locally."""
    composed = compose_snippet(
        task_type=TaskType.REPLY_PARSING,
        company_anchor="12345678",
        fields={"reply_text": "my HKID is A123456(7)"},
    )
    token = next(t for t in composed.tokens if t.startswith("[HKID"))
    assert composed.tokens[token] == "A123456(7)"


def test_a_masked_anchor_value_is_recoverable_too() -> None:
    """The anchor is scrubbed with the reduced pattern set, and a value masked
    there lands in the same table. The bare-mobile carve-out is unchanged — an
    eight-digit anchor keeps its BRN shape — but a volunteered address is
    restorable, not merely gone."""
    anchor = "Flat A, 12/F, 88 Test Road, Test District, Kowloon"
    composed = compose_snippet(
        task_type=TaskType.REPLY_PARSING,
        company_anchor=anchor,
        fields={"reply_text": "Confirmed, proceed."},
    )
    assert composed.company_anchor == "[ADDRESS_01]"
    assert composed.tokens == {"[ADDRESS_01]": anchor}


def test_a_marker_names_exactly_one_value_across_the_whole_snippet() -> None:
    """One table for the composed snippet, not one per field. The counters run
    across the anchor and every field in the order the wire text reads, so two
    fields that each volunteer an email cannot both claim ``[EMAIL_01]`` for
    different originals — which would make the restore a coin flip rather than
    a function."""
    composed = compose_snippet(
        task_type=TaskType.REPLY_PARSING,
        company_anchor="12345678",
        fields={
            "reply_text": "Write to first01@example.org.",
            "role_reference": "second02@example.org",
        },
    )
    assert composed.text == (
        "company: 12345678\n"
        "reply_text: Write to [EMAIL_01].\n"
        "role_reference: [EMAIL_02]"
    )
    assert composed.tokens == {
        "[EMAIL_01]": "first01@example.org",
        "[EMAIL_02]": "second02@example.org",
    }


def test_the_scrub_table_stays_off_the_wire() -> None:
    """Ruling D4's wire half: the composed text is the markers, and no value
    in the table appears in it."""
    composed = compose_snippet(
        task_type=TaskType.REPLY_PARSING,
        company_anchor="12345678",
        fields={"reply_text": "My HKID is A123456(7); write to tester01@example.org."},
    )
    assert "[HKID_01]" in composed.text
    assert "[EMAIL_01]" in composed.text
    assert composed.tokens
    for value in composed.tokens.values():
        assert value not in composed.text


def test_the_ordinance_qa_question_carries_its_own_scrub_table() -> None:
    """The R11 question is scrubbed like any other text, so its masked values
    are restorable too — with no client content in the snippet, the scrub
    table is the whole key."""
    composed = compose_snippet(
        task_type=TaskType.ORDINANCE_QA,
        non_client_text="My HKID is A123456(7) — does s.584 still apply?",
    )
    assert composed.tokens == {"[HKID_01]": "A123456(7)"}
    assert "A123456(7)" not in composed.text


def test_compose_leaves_a_bare_brn_anchor_unchanged() -> None:
    """A bare 8-digit run in the low prefixes (1-3) stays untouched — it is
    indistinguishable from a business registration number — so the ordinary
    BRN anchor passes through scrubbing unchanged."""
    composed = compose_snippet(
        task_type=TaskType.REPLY_PARSING,
        company_anchor="22334455",
        fields={"reply_text": "Confirmed."},
    )
    assert composed.company_anchor == "22334455"
    assert composed.text.startswith("company: 22334455")


def test_a_bare_anchor_in_a_mobile_prefix_keeps_its_brn_shape() -> None:
    """An eight-digit BRN can share a higher prefix (4-9). The anchor is the
    company row's *declared* BRN, not client free text, so the bare-mobile
    rule does not re-type it (D2: a token names the kind, and the tokenizer
    must still see a BRN-shaped anchor). The same run in free text is a phone
    — the bare-mobile case above."""
    composed = compose_snippet(
        task_type=TaskType.REPLY_PARSING,
        company_anchor="99887766",
        fields={"reply_text": "Confirmed."},
    )
    assert composed.company_anchor == "99887766"
    assert composed.text.startswith("company: 99887766")
    assert "[PHONE_01]" in scrub_free_text("Reach me on 99887766.")


def test_compose_enforces_the_16_kib_cap() -> None:
    assert SNIPPET_MAX_BYTES == 16 * 1024
    with pytest.raises(RedactionRefused, match="16 KiB"):
        compose_snippet(
            task_type=TaskType.REPLY_PARSING,
            company_anchor="12345678",
            fields={"reply_text": "word " * 4000},
        )


def test_compose_cap_counts_utf8_bytes_not_characters() -> None:
    """4,000 CJK characters are 12,000 bytes; 5,500 are over the cap even
    though both look similar in character counts."""
    compose_snippet(
        task_type=TaskType.REPLY_PARSING,
        company_anchor="12345678",
        fields={"reply_text": "文" * 4000},
    )
    with pytest.raises(RedactionRefused, match="16 KiB"):
        compose_snippet(
            task_type=TaskType.REPLY_PARSING,
            company_anchor="12345678",
            fields={"reply_text": "文" * 5500},
        )


# --- Tokenizer: tokenize locally, restore locally ---


def test_token_labels_is_the_closed_d1_vocabulary() -> None:
    """Ruling D1's label vocabulary as data: exactly these eleven types, no
    catch-all and no shape-bearing label. The canary's pattern is the same
    set spelled out; this is the one home a later task (T4's detectors)
    numbers against."""
    assert frozenset(
        {
            "PERSON",
            "COMPANY",
            "BRN",
            "CR",
            "ADDRESS",
            "HKID",
            "PASSPORT",
            "DOB",
            "PHONE",
            "EMAIL",
            "BANK",
        }
    ) == TOKEN_LABELS


def test_tokenize_is_typed_and_per_request() -> None:
    """The typed path: each label numbers its own values, an occurrence seen
    twice keeps one token, and the map is exactly the values that matched."""
    result = tokenize(
        "陳大文 met 陳大文 of Example Limited",
        values={"PERSON": ["陳大文"], "COMPANY": ["Example Limited"]},
    )
    assert result.text == "[PERSON_01] met [PERSON_01] of [COMPANY_01]"
    assert result.tokens == {"[PERSON_01]": "陳大文", "[COMPANY_01]": "Example Limited"}


def test_tokenize_matches_through_normalization() -> None:
    """The lexicon value is the record's traditional form and the reply
    writes the simplified one: matching is on the normalized comparison form,
    while the replacement span is the text as written — the surrounding text
    is never rewritten."""
    tokenized = tokenize("陈大文 signed today", values={"PERSON": ["陳大文"]})
    assert tokenized.text == "[PERSON_01] signed today"
    assert tokenized.tokens == {"[PERSON_01]": "陳大文"}


def test_tokenize_gives_a_fresh_map_each_call() -> None:
    """Placeholders are never reused across calls: each call takes a fresh
    map, so two snippets' keys never mix (the request-scoped-memory rule)."""
    first = tokenize("Chan signed.", values={"PERSON": ["Chan"]})
    second = tokenize("Wong signed.", values={"PERSON": ["Wong"]})
    assert first.tokens == {"[PERSON_01]": "Chan"}
    assert second.tokens == {"[PERSON_01]": "Wong"}


def test_tokenize_replaces_each_value_with_a_stable_token() -> None:
    tokenized = tokenize(
        "Director Ada Tester and shareholder Bob Sample of Example Limited signed.",
        values={"PERSON": ["Ada Tester", "Bob Sample"], "COMPANY": ["Example Limited"]},
    )
    assert tokenized.text == (
        "Director [PERSON_01] and shareholder [PERSON_02] of [COMPANY_01] signed."
    )
    assert tokenized.tokens == {
        "[PERSON_01]": "Ada Tester",
        "[PERSON_02]": "Bob Sample",
        "[COMPANY_01]": "Example Limited",
    }


def test_tokenize_registers_a_token_only_for_a_value_that_matched() -> None:
    """The table carries an entry only for a lexicon value that actually
    matched a span. The caller supplies two officer names and only one appears
    in the text: the table holds that value's token and nothing else, so a
    model that emitted a token for the roster's other (never-sent) name has
    nothing to restore — the deployment cannot re-identify a master-data value
    it never put on the wire."""
    tokenized = tokenize(
        "Chan Tai Man signed.",
        values={"PERSON": ["Chan Tai Man", "Wong Siu"]},
    )
    assert tokenized.text == "[PERSON_01] signed."
    assert tokenized.tokens == {"[PERSON_01]": "Chan Tai Man"}
    assert "[PERSON_02]" not in tokenized.tokens


def test_tokenize_refuses_a_label_outside_the_closed_vocabulary() -> None:
    """Ruling D1 enforced mechanically: the label vocabulary is closed, so a
    caller may not invent a shape-bearing one. An unknown key refuses the
    call (nothing is composed) instead of silently emitting a marker that
    carries the value's shape."""
    with pytest.raises(ValueError, match="HKID_8DIGITS"):
        tokenize("A123456(7)", values={"HKID_8DIGITS": ["A123456(7)"]})


def test_a_lexicon_label_the_scrubber_reserves_is_refused() -> None:
    """Ruling R-T7-2: the tokenizer's markers share one namespace with the
    scrubber's, so a caller lexicon under a scrub label (``HKID``, ``PHONE``,
    …) could collide with a scrub marker and re-identify it to a value that
    never left the snippet. It is a caller bug — ``ValueError``, the same
    programming-error signal as an unknown label (R-T4-2) — not a client-data
    refusal."""
    with pytest.raises(ValueError, match="HKID"):
        tokenize("My HKID is A123456(7).", values={"HKID": ["B987654(3)"]})


def test_the_legacy_untagged_tokenize_sugar_is_retired() -> None:
    """The pre-typed ``persons=``/``identifiers=`` shapes were the only
    source of a label outside D1's vocabulary (``IDENTIFIER``); the typed
    ``values`` mapping is now the one tokenizer surface."""
    with pytest.raises(TypeError):
        tokenize("Chan signed.", persons=["Chan"])  # type: ignore[call-arg]


def test_tokenize_replaces_longer_names_first() -> None:
    """A name that is a prefix of another must not split the longer one."""
    tokenized = tokenize(
        "Ada Tester and Ada both signed.",
        values={"PERSON": ["Ada Tester", "Ada"]},
    )
    assert tokenized.text == "[PERSON_01] and [PERSON_02] both signed."
    assert tokenized.tokens["[PERSON_01]"] == "Ada Tester"


def test_tokenize_respects_word_boundaries() -> None:
    """Fix round 1 (review finding): short HK surnames are substrings of
    ordinary English words. 'Chan' must not rewrite 'Channel' — only a
    standalone occurrence is a name."""
    tokenized = tokenize(
        "Chan confirmed; the Channel Tunnel update is unrelated.",
        values={"PERSON": ["Chan"]},
    )
    assert tokenized.text == (
        "[PERSON_01] confirmed; the Channel Tunnel update is unrelated."
    )
    assert tokenized.tokens == {"[PERSON_01]": "Chan"}


def test_tokenize_matches_names_inside_cjk_runs() -> None:
    """The boundary check only fences off ASCII letters and digits: CJK text
    has no spaces to bound words, so a Chinese name embedded mid-sentence
    must still tokenize."""
    tokenized = tokenize(
        "董事陳大文已辭任。確認人Chan亦同意。",
        values={"PERSON": ["陳大文", "Chan"]},
    )
    assert tokenized.text == "董事[PERSON_02]已辭任。確認人[PERSON_01]亦同意。"
    assert tokenized.tokens == {"[PERSON_02]": "陳大文", "[PERSON_01]": "Chan"}


# --- Tokenizer: bounded local fuzzy matching (ruling D6) ---


def test_one_typo_in_a_lexicon_name_is_still_tokenized() -> None:
    """D6's positive half, and the case R-T5-1 exists for: a Chinese name
    written one edit away from the roster's is over-tokenized rather than left
    in the clear — CJK values take a three-character fuzzy floor, so the
    brief's ``陳大民``/``陳大文`` case matches. A Latin name (five-character
    floor) matches too."""
    chinese = tokenize("陳大民 signed.", values={"PERSON": ["陳大文"]})
    assert chinese.text == "[PERSON_01] signed."
    assert chinese.tokens == {"[PERSON_01]": "陳大文"}

    latin = tokenize(
        "Lam Siu Fong signed the resolution.",
        values={"PERSON": ["Lam Siu Fung"]},
    )
    assert latin.text == "[PERSON_01] signed the resolution."
    assert latin.tokens == {"[PERSON_01]": "Lam Siu Fung"}


def test_ambiguous_near_match_is_over_tokenized_and_restores_the_clients_words() -> None:
    """D6's ambiguity branch, under ruling R-T7-1: a token equidistant from two
    roster values is replaced by a fresh typed token that records the literal
    span it replaced — never the lexicon value it could not choose between.
    Restoring the placeholder therefore returns the client's own words
    unchanged, and no placeholder the model can echo back is left
    unrestorable. The two roster values that never appeared are not in the
    table (a token is minted only for a matched value), so no never-sent value
    is restorable either."""
    tokenized = tokenize(
        "Chan Tai Min signed the minutes.",
        values={"PERSON": ["Chan Tai Man", "Chan Tai Men"]},
    )
    assert "Chan Tai Min" not in tokenized.text
    assert tokenized.text == "[PERSON_03] signed the minutes."
    assert tokenized.tokens == {"[PERSON_03]": "Chan Tai Min"}
    assert restore_tokens("[PERSON_03]", tokenized.tokens) == "Chan Tai Min"


def test_unrelated_words_are_not_tokenized() -> None:
    """D6's guard in the other direction: the fuzzy pass is length-floored and
    boundary-fenced, so an ordinary word that merely contains or resembles a
    short lexicon value is left alone — ``Channel`` is not ``Chan`` and
    ``them`` is not ``the``."""
    tokenized = tokenize(
        "Channel and them are unrelated to Chan.",
        values={"PERSON": ["Chan"], "COMPANY": ["The"]},
    )
    assert tokenized.text == "Channel and them are unrelated to [PERSON_01]."


def test_fuzzy_matching_applies_to_company_names() -> None:
    """R-T5-2's allow-list is name-like labels, not persons alone: a
    ``COMPANY`` value one edit from the roster's spelling is over-tokenized
    too."""
    tokenized = tokenize(
        "Example Limitd signed.",
        values={"COMPANY": ["Example Limited"]},
    )
    assert tokenized.text == "[COMPANY_01] signed."
    assert tokenized.tokens == {"[COMPANY_01]": "Example Limited"}


def test_identifier_labels_are_exact_only_under_fuzzy_matching() -> None:
    """R-T5-2: only ``PERSON``/``COMPANY`` are fuzzy-matched. A one-digit
    difference is a *different* identifier — BRNs are issued incrementally, so
    a fuzzy BRN rule would mislabel a neighbouring company's real number — so a
    ``BRN`` lexicon value matches exactly and a one-digit-off number transits
    untouched."""
    tokenized = tokenize(
        "BRN 12345679 differs from 12345678.",
        values={"BRN": ["12345678"]},
    )
    assert tokenized.text == "BRN 12345679 differs from [BRN_01]."
    assert tokenized.tokens == {"[BRN_01]": "12345678"}


def test_cr_is_exact_only_under_fuzzy_matching() -> None:
    """R-T5-2's rule is a property of the label, not of the BRN example: a
    ``CR`` number one digit from the roster's is a different company's number,
    so it matches exactly and a one-digit-off number transits untouched."""
    tokenized = tokenize(
        "CR 12345679 differs from 12345678.",
        values={"CR": ["12345678"]},
    )
    assert tokenized.text == "CR 12345679 differs from [CR_01]."
    assert tokenized.tokens == {"[CR_01]": "12345678"}


def test_the_fuzzy_floor_keeps_short_values_exact_only() -> None:
    """The floor that keeps short words off the fuzzy path: a CJK value below
    three normalized characters is exact-only, so a two-character name one edit
    from the roster's is not over-tokenized. (The three-character
    ``陳大民``/``陳大文`` case above sits *at* the CJK floor and does match.)"""
    tokenized = tokenize("陳大 signed.", values={"PERSON": ["陳小"]})
    assert tokenized.text == "陳大 signed."


def test_the_fuzzy_floor_is_script_aware_beyond_the_base_cjk_block() -> None:
    """R-T5-1's floor has to hold outside the base Unified Ideographs block: a
    name whose characters sit in CJK Extension A (U+3400-U+4DBF) is no less a
    Chinese name, so it takes the three-character floor too and a one-edit
    spelling still matches. A script test that scanned only U+4E00-U+9FFF
    would apply the Latin five-character floor here and the name would not
    match at all."""
    tokenized = tokenize("\u3400\u3401\u3402 signed.", values={"PERSON": ["\u3400\u3401\u3403"]})
    assert tokenized.text == "[PERSON_01] signed."
    assert tokenized.tokens == {"[PERSON_01]": "\u3400\u3401\u3403"}


def test_restore_recurses_over_response_json() -> None:
    tokenized = tokenize(
        "Ada Tester resigned.",
        values={"PERSON": ["Ada Tester"]},
    )
    response: dict[str, object] = {
        "events": [{"officer": "[PERSON_01]", "note": "Director [PERSON_01] ceased."}],
        "count": 1,
    }
    assert restore_tokens(response, tokenized.tokens) == {
        "events": [{"officer": "Ada Tester", "note": "Director Ada Tester ceased."}],
        "count": 1,
    }


def test_restore_leaves_structure_and_non_strings_untouched() -> None:
    """What the walk does not touch: numbers, flags, nulls and free prose pass
    through, and a mapping is rebuilt with its keys intact."""
    value = {"count": 1, "ok": True, "missing": None, "note": "unchanged"}
    assert restore_tokens(value, {"[PERSON_01]": "Ada Tester"}) == value


# --- Restore: the answer may not invent a value (Task 7, review focus 6) ---


def test_an_invented_literal_is_refused() -> None:
    """A value literal the model returns that is neither a known token nor an
    allowed literal is an invention: it cannot have seen the value (the
    snippet carried a token), so it made it up — refused, never stored."""
    with pytest.raises(RedactionRefused) as refused:
        restore_tokens({"answer": "A987654(3)"}, {"[HKID_01]": "A123456(7)"})
    assert refused.value.reason == "invented_value"


def test_known_tokens_and_allowed_enums_restore() -> None:
    assert restore_tokens(
        {"answer": "[HKID_01]", "state": "confirmed"},
        {"[HKID_01]": "A123456(7)"},
        allowed_literals=frozenset({"confirmed"}),
    ) == {"answer": "A123456(7)", "state": "confirmed"}


def test_an_invented_placeholder_is_refused() -> None:
    """A marker this call's table never produced is an invention too: the model
    may not hand back a placeholder nothing can restore."""
    with pytest.raises(RedactionRefused) as refused:
        restore_tokens({"note": "[PERSON_99] stayed"}, {"[PERSON_01]": "Ada Tester"})
    assert refused.value.reason == "invented_value"


def test_free_evidence_text_and_derived_values_are_not_inventions() -> None:
    """The rule's scope, pinned: a value *literal* is refused, free prose is
    not. A reply parse quotes the client (its evidence spans) and computes ISO
    dates from the reply by instruction — neither is a literal the model
    invented. The span post-check owns the prose, this rule owns the literals;
    refusing the prose would refuse the parser's own grammar."""
    value = {
        "span": "the director is unchanged",
        "effective_date": "2026-02-01",
        "notes": None,
    }
    assert restore_tokens(value, {"[PERSON_01]": "Ada Tester"}) == value


def test_a_value_literal_beside_a_known_token_is_still_refused() -> None:
    """Substituting a token does not launder the rest of the string: what the
    model wrote itself is still a literal it can only have invented."""
    with pytest.raises(RedactionRefused) as refused:
        restore_tokens(
            {"note": "[PERSON_01] also gave HKID A987654(3)"},
            {"[PERSON_01]": "Ada Tester"},
        )
    assert refused.value.reason == "invented_value"


# --- Marker fencing (workstream ④, T1; residuals N2/N3 of the 2026-10-06
# --- slice's ledger): pre-existing [LABEL_nn] spans are opaque to both passes ---


def test_a_one_digit_marker_is_refused_as_an_invention() -> None:
    """N3: a ``[PERSON_1]``-shaped marker is marker-intent text the table never
    minted (the passes mint two-digit ordinals). Recognition covers the
    one-digit ordinal, so the strict restore refuses it as an invented
    placeholder rather than passing malformed marker text through to a
    client-facing artifact."""
    with pytest.raises(RedactionRefused) as refused:
        restore_tokens({"note": "[PERSON_1]"}, {"[PERSON_01]": "Ada Tester"})
    assert refused.value.reason == "invented_value"


def test_a_marker_shaped_lexicon_value_cannot_rewrite_an_existing_marker() -> None:
    """N2: a caller lexicon value that is itself marker-shaped text may not
    rewrite a marker already inside the snippet. The pre-existing span is
    opaque to the tokenize pass: the text passes through unchanged and no
    token is minted for a value the snippet only carried as a marker."""
    result = tokenize("See [COMPANY_01] now.", values={"PERSON": ["[COMPANY_01]"]})
    assert result.text == "See [COMPANY_01] now."
    assert result.tokens == {}


def test_the_one_digit_marker_shape_is_opaque_to_the_tokenize_pass() -> None:
    """The same fence covers the malformed one-digit shape: fencing recognises
    the marker family, not only the two-digit minted shape, so a lexicon value
    matching ``[PERSON_1]`` cannot rewrite it either."""
    result = tokenize("See [PERSON_1] now.", values={"PERSON": ["[PERSON_1]"]})
    assert result.text == "See [PERSON_1] now."
    assert result.tokens == {}


def test_the_scrub_pass_leaves_a_pre_existing_marker_opaque() -> None:
    """The scrub side of the fence: a pattern may not rewrite a span that was
    already a marker before the pass ran. The address pattern's reach stops at
    the marker — the pre-existing ``[PHONE_01]`` survives verbatim and the
    address beside it still masks."""
    result = scrub_text("致電 [PHONE_01] 後遷銅鑼灣道1號")
    assert "[PHONE_01]" in result.text
    assert "[ADDRESS_01]" in result.text
    assert "銅鑼灣" not in result.text


# --- The simplified-script layer (workstream ④, T2; rulings D2/D3): the CJK
# --- keyword and marker vocabularies carry both scripts side by side, and the
# --- prose surface of the new entries must pass untouched ---


def test_the_simplified_keyword_vocabularies_mask_their_shapes() -> None:
    """D2, the keyword half: 护照/电话/转账/银行户口/身份证 anchor their shapes
    exactly like their Traditional siblings — detection runs on the normalized
    form, and the simplified keywords are listed beside the Traditional ones."""
    assert scrub_text("我的护照号码 E1234567 已更新").text == "我的护照号码 [PASSPORT_01] 已更新"
    assert scrub_text("请致电 23456789 联络").text == "请致电 [PHONE_01] 联络"
    assert scrub_text("请转账到银行户口 98765432").text == "请转账到[BANK_01]"


def test_the_simplified_hkid_keyword_fails_the_verify_pass_closed() -> None:
    """D2, the detector half: a simplified-keyworded HKID body without its check
    digit is the labelled shape the verify pass owns. Without 身份证 in the
    detector the body transits in clear; the simplified keyword joins the
    Traditional one and the compose refuses."""
    with pytest.raises(RedactionRefused) as caught:
        scrub_text("身份证号码 A123456")
    assert caught.value.reason == "unscrubbable_protected_data"


def test_the_simplified_markers_extend_the_address_patterns() -> None:
    """D2, the marker half: 号/大厦/大楼/单位 join 號/大廈/大樓/單位 in the street,
    family, and bare-floor patterns — a simplified unit marker completes the
    address shape exactly as the Traditional one does."""
    # The leftmost match absorbs up to the run cap of the CJK prose before
    # the shape (20 chars for the street pattern's reach, 6 for the family's
    # named lead) — the same reach the Traditional patterns have always had.
    assert scrub_text("仓库在九龙旺角道1号。").text == "[ADDRESS_01]。"
    assert scrub_text("搬到阳光大厦3座。").text == "[ADDRESS_01]。"
    assert scrub_text("新办公室在永安大楼12楼。").text == "新[ADDRESS_01]。"


@pytest.mark.parametrize(
    "prose",
    [
        "村口楼上有巴士，大家方便。",  # noqa: RUF001  # no designator: not an address (R-A')
        "今年上市的公司很多。",  # 市-suffix prose surface, no shape fragment
        "这件事很省事。",  # 省-suffix prose surface
        "请存入梦想，转账希望。",  # noqa: RUF001  # anchors with no digit run
    ],
)
def test_the_simplified_layer_leaves_prose_untouched(prose: str) -> None:
    """The over-tokenization bar's pins: every widened entry ships with the
    prose it must not claim. An anchor or marker alone masks nothing."""
    assert scrub_text(prose).text == prose

# --- The address geo-vocabulary (workstream ④, T3; rulings D4/D5/D6):
# --- geographic names are an anchor family, never a class ---


def test_a_geo_name_combines_with_a_shape_fragment_to_extend_the_mask() -> None:
    """D4/D5, the combination design: a locality, district, province, or country
    name within the window before a shape fragment (street marker, name+unit,
    building family, postal code) extends and confirms the mask — fragments the
    replacer's street pattern misses (no 號) or shapes only an anchor can
    certify. The mask runs from the nearest anchor to the fragment's end."""
    assert scrub_text("新界沙田大圍道12，下月搬。").text == "新界[ADDRESS_01]，下月搬。"  # noqa: RUF001
    assert scrub_text("搬到北京市海淀区中关村大街27。").text == "搬到北京市[ADDRESS_01]。"
    assert scrub_text("澳洲悉尼喬治街20 有分店。").text == "[ADDRESS_01] 有分店。"
    assert scrub_text("廣東省深圳市 518057 見。").text == "廣東省[ADDRESS_01] 見。"


def test_the_mainland_unit_and_lane_markers_join_the_designator_sets() -> None:
    """D6: 栋/幢/單元/单元 join the family pattern's unit markers and 弄 owns
    the lane shape — mainland addresses route through the same designator+unit
    rule (R-A') as HK ones."""
    # The family's named lead absorbs up to the 6-char run cap of preceding
    # CJK prose — the same reach as the Traditional patterns.
    assert scrub_text("住在阳光村3栋。").text == "[ADDRESS_01]。"
    assert scrub_text("住在阳光苑A幢。").text == "[ADDRESS_01]。"
    assert scrub_text("上海市南京西路12弄5号。").text == "[ADDRESS_01]。"


@pytest.mark.parametrize(
    "prose",
    [
        "我上月去日本出差。",  # a country name alone is prose (D4)
        "廣東省的經濟增長很快。",  # a province name alone is prose
        "旺角的街市很熱鬧。",  # a locality plus 街市, no number
        "公司在中環舉行周年晚會。",  # a locality, no fragment
        "這家上市公司業績良好。",  # 市-suffix anchor, no fragment
        "上市公司有23座寫字樓。",  # bare 座 is a classifier, not a unit
        "行政區劃調整是政府工作。",  # 區-suffix prose surface
    ],
)
def test_geo_names_alone_mask_nothing(prose: str) -> None:
    """D4's red line, pinned: the geo vocabulary never acts as a standalone
    class. A geographic name with no shape fragment in the window leaves the
    text byte-for-byte untouched — over-refusal kills automation exactly as
    a leak would."""
    assert scrub_text(prose).text == prose


# --- Digest: Q1.5's minimized summary ---


def test_digest_is_a_normalized_500_char_prefix_of_the_outbound_snippet() -> None:
    text = "word " * 200
    digest = snippet_digest(text)
    assert isinstance(digest, SnippetDigest)
    assert len(digest.summary) == DIGEST_MAX_CHARS
    assert "\n" not in digest.summary
    assert "  " not in digest.summary


def test_digest_summary_counts_characters_not_bytes() -> None:
    digest = snippet_digest("文" * 800)
    assert len(digest.summary) == DIGEST_MAX_CHARS
    assert digest.summary == "文" * DIGEST_MAX_CHARS


def test_digest_hash_covers_the_exact_outbound_snippet() -> None:
    """Q1.5: the digest pair is (normalized ≤500-char prefix, SHA-256 of the
    exact scrubbed+tokenized snippet) — the hash binds the bytes that left,
    not a normalized copy of them."""
    text = "company: 12345678\nreply_text: [PERSON_01] confirmed."
    digest = snippet_digest(text)
    assert digest.sha256 == hashlib.sha256(text.encode("utf-8")).hexdigest()
    # Normalization for the summary never leaks into the hashed content.
    spaced = "company:   12345678\n\nreply_text: [PERSON_01]   confirmed."
    assert snippet_digest(spaced).sha256 != digest.sha256
    assert snippet_digest(spaced).summary == digest.summary


def test_digest_is_stable_and_sensitive_to_change() -> None:
    text = "company: 12345678\nreply_text: [PERSON_01] confirmed."
    assert snippet_digest(text) == snippet_digest(text)
    changed = text.replace("confirmed", "declined")
    assert snippet_digest(changed).sha256 != snippet_digest(text).sha256


# --- R11: ordinance Q&A's non-client question (M6.4 ruling) ---


def test_compose_admits_a_non_client_question_for_ordinance_qa() -> None:
    """R11: the one payload ordinance Q&A may carry is the staff-authored,
    non-client statute question — no company anchor, no fields, the text is
    the composed snippet verbatim."""
    composed = compose_snippet(
        task_type=TaskType.ORDINANCE_QA,
        non_client_text="Under Cap. 622 s.584, what filing window applies?",
    )
    assert composed.task_type is TaskType.ORDINANCE_QA
    assert composed.company_anchor == ""
    assert composed.fields == ()
    assert composed.text == "Under Cap. 622 s.584, what filing window applies?"


def test_compose_still_refuses_client_content_beside_a_non_client_question() -> None:
    """The R11 exception admits only the question: a company anchor or any
    field alongside it is client content and the compose still refuses."""
    with pytest.raises(RedactionRefused, match="client_content"):
        compose_snippet(
            task_type=TaskType.ORDINANCE_QA,
            company_anchor="12345678",
            non_client_text="Under Cap. 622 s.584, what filing window applies?",
        )
    with pytest.raises(RedactionRefused, match="client_content"):
        compose_snippet(
            task_type=TaskType.ORDINANCE_QA,
            fields={"reply_text": "anything"},
            non_client_text="Under Cap. 622 s.584, what filing window applies?",
        )


def test_compose_refuses_a_non_client_question_for_client_tasks() -> None:
    """The exception is ordinance-Q&A-only; the other three profiles keep the
    anchor-required shape and a stray non-client text is refused, not merged."""
    with pytest.raises(RedactionRefused, match="non_client_text"):
        compose_snippet(
            task_type=TaskType.REPLY_PARSING,
            company_anchor="12345678",
            fields={"reply_text": "Confirmed."},
            non_client_text="Under Cap. 622 s.584, what filing window applies?",
        )


def test_compose_requires_a_question_for_ordinance_qa() -> None:
    """An ordinance-Q&A call with nothing to ask carries nothing defensible."""
    with pytest.raises(RedactionRefused, match="question"):
        compose_snippet(task_type=TaskType.ORDINANCE_QA)


def test_compose_scrubs_the_non_client_question() -> None:
    """Defense in depth (R11): the staff-authored text still passes through
    the scrubber, so a volunteered protected-class identifier is replaced
    before the snippet exists."""
    composed = compose_snippet(
        task_type=TaskType.ORDINANCE_QA,
        non_client_text="My HKID is A123456(7) — does s.584 still apply?",
    )
    assert "[HKID_01]" in composed.text
    assert "A123456(7)" not in composed.text


def test_compose_refuses_an_unscrubbable_question() -> None:
    """Fail-closed stays: a protected-class shape the scrubber cannot cleanly
    consume refuses the whole compose, question or not."""
    with pytest.raises(RedactionRefused, match="unscrubbable"):
        compose_snippet(
            task_type=TaskType.ORDINANCE_QA,
            non_client_text="HKID A123456, check digit to follow — does s.584 apply?",
        )


def test_compose_bounds_the_non_client_question() -> None:
    """The 16 KiB cap is a property of any outbound snippet, not of the
    client-content profiles alone."""
    with pytest.raises(RedactionRefused, match="16 KiB"):
        compose_snippet(task_type=TaskType.ORDINANCE_QA, non_client_text="word " * 4000)


def test_profiles_are_frozen_data() -> None:
    for profile in PROFILES.values():
        assert isinstance(profile, RedactionProfile)


# --- The split profile's compose leg (workstream ③, ruling D2) -----------------

# The caller's token map: two involved companies' identifying values,
# tokenized before composition — the binding stays local, as ruling D2's
# "the caller tokenizes" has it.
_SPLIT_LEXICON = {
    "COMPANY": ["Synthetic Harbour Limited", "Other Harbour Limited"],
}
_TOKENIZED_SPLIT_CONTENT = (
    "Please update the register for [COMPANY_01] and file the [COMPANY_02] return."
)


def test_the_split_profile_is_the_q12_amendment_row() -> None:
    """Ruling D2's row, pinned field by field: one free-text client-content
    field, names tokenized, client content admitted — and no company anchor,
    the deliberate second shape beside the one-anchor rule."""
    profile = PROFILES[TaskType.SPLIT]
    assert profile.allowed_fields == frozenset({"content"})
    assert profile.tokenize_names is True
    assert profile.tokenize_opt_out_reason is None
    assert profile.allows_client_content is True
    assert profile.anchor_required is False


def test_the_split_composes_an_anchor_free_tokenized_snippet() -> None:
    """The split leg: the caller's tokenized content composes into a snippet
    with no anchor line — placeholders and prose alone cross the wire, and
    the token→company binding never appears (the caller keeps it locally)."""
    snippet = compose_snippet(
        task_type=TaskType.SPLIT,
        fields={"content": _TOKENIZED_SPLIT_CONTENT},
    )
    assert snippet.task_type is TaskType.SPLIT
    assert snippet.company_anchor == ""
    assert snippet.fields == (("content", _TOKENIZED_SPLIT_CONTENT),)
    assert snippet.text == f"content: {_TOKENIZED_SPLIT_CONTENT}"
    # Placeholders only: the companies' names appear nowhere in the wire text,
    # and the binding table the snippet carries is the scrub table (empty —
    # the caller's tokenizer table never enters the compose).
    assert "Synthetic Harbour" not in snippet.text
    assert "Other Harbour" not in snippet.text
    assert snippet.tokens == {}


def test_compose_refuses_an_anchor_on_the_split_profile() -> None:
    with pytest.raises(RedactionRefused) as caught:
        compose_snippet(
            task_type=TaskType.SPLIT,
            company_anchor="12345678",
            fields={"content": _TOKENIZED_SPLIT_CONTENT},
        )
    assert caught.value.reason == "anchor_not_permitted"


def test_compose_refuses_a_field_outside_the_split_allow_list() -> None:
    with pytest.raises(RedactionRefused) as caught:
        compose_snippet(
            task_type=TaskType.SPLIT,
            fields={"content": _TOKENIZED_SPLIT_CONTENT, "officer_name": "Chan Tai Man"},
        )
    assert caught.value.reason == "field_not_allowed"
    assert caught.value.detail == "officer_name"


def test_compose_requires_the_content_field_on_the_split_profile() -> None:
    with pytest.raises(RedactionRefused) as caught:
        compose_snippet(task_type=TaskType.SPLIT, fields={})
    assert caught.value.reason == "fields_required"


def test_compose_refuses_non_client_text_on_the_split_profile() -> None:
    with pytest.raises(RedactionRefused) as caught:
        compose_snippet(
            task_type=TaskType.SPLIT,
            fields={"content": _TOKENIZED_SPLIT_CONTENT},
            non_client_text="a staff note",
        )
    assert caught.value.reason == "non_client_text_forbidden"
    assert caught.value.detail == "split"


def test_the_split_leg_keeps_the_hard_exclusions_fail_closed() -> None:
    """A protected-class shape in the content refuses exactly like any other
    profile — the no-anchor shape is not a relaxation of the scrub bar."""
    with pytest.raises(RedactionRefused) as caught:
        compose_snippet(
            task_type=TaskType.SPLIT,
            fields={"content": "HKID A123456, check digit to follow — please file it."},
        )
    assert caught.value.reason == "unscrubbable_protected_data"


def test_the_split_leg_keeps_the_size_cap() -> None:
    with pytest.raises(RedactionRefused) as caught:
        compose_snippet(
            task_type=TaskType.SPLIT,
            fields={"content": "word " * (SNIPPET_MAX_BYTES // 4)},
        )
    assert caught.value.reason == "snippet_too_large"
