"""Q1.2's redaction bar as data and code (the M6 counsel package:
``research/legal/internal-counsel-drafts/flow-1-inference-channel-personal-data-opinion.md``
§Q1.2 and the "M6 must enforce" list, controls 1, 3, 4, 5).

Single owner of the inference channel's minimization. Nothing crosses the
gateway except a snippet this module composed, and the three cross-cutting
rules from Q1.2 are enforced here as code, not runbooks:

1. **The composed snippet is the unit of analysis.** ``compose_snippet`` is
   the only composition point: one company anchor and one subject per call,
   the profile's allow-list enforced field by field, a 16 KiB cap on the
   composed bytes. Callers cannot assemble a payload around it. The single
   exception is controller ruling R11: ordinance Q&A, whose profile admits
   no client content, may carry one staff-authored, non-client statute
   question — no anchor, no fields, still scrubbed, still capped, never
   tokenized.
2. **Free-text scrubbing is mandatory, failing closed, and reversible.**
   ``scrub_text`` pattern-scans client-authored prose deterministically
   (stdlib ``re``) and replaces what it recognises, recording each marker
   against the exact span it replaced. The scrub pass is followed by a verify
   pass: any *protected-class* shape still present afterwards — a match the
   scrubber would have to guess at, like an HKID quoted without its check
   digit, an address fragment without a street shape, or a number-plus-
   street shape with no recognizable HK district tail — raises
   ``RedactionRefused``; the call does not go out half-redacted. What the
   scrubber masks is not discarded (ruling D3): the table it returns restores
   a marker the model echoes back, locally, and the composed snippet carries
   that table. ``scrub_free_text`` stays as the thin text-only wrapper for
   callers that need nothing but the scrubbed string.
3. **Tokenize by default, justify exceptions.** The profile table carries
   ``tokenize_names`` and, where a task opts out, the recorded reason in the
   profile data itself. ``tokenize`` takes a typed lexicon — a label from the
   closed ``TOKEN_LABELS`` vocabulary to the exact values that label applies
   to — whose members are replacement sources only, matched through
   ``normalize_for_matching`` and never sent. Matching is exact first, then
   bounded and privacy-first under ruling D6 and its R-T5 refinements: only
   name-like labels (``PERSON``, ``COMPANY``) are fuzzy-matched — a one-digit
   difference is a different identifier, so ``BRN``/``CR`` stay exact (R-T5-2)
   — and a snippet token within one edit of exactly one such value is replaced
   by that value's token. The fuzzy floor is script-aware (R-T5-1): three
   normalized characters for a value containing CJK, five for a Latin one, so
   a Chinese name (two to four characters) matches while ``Chan`` does not fuzz
   ``Channel``. A token equidistant from two or more values is replaced by a
   fresh typed token that records the literal span it replaced: an ambiguous
   near miss is over-tokenized rather than left in the clear, the local
   restore cannot invent which value it was, and — ruling R-T7-1 — every
   token the snippet carries stays restorable, so restoring it returns the
   client's own words rather than a placeholder nothing can resolve.
   ``tokenize``/``restore_tokens``
   keep the re-identification key in request-scoped memory: placeholders
   cross the wire, originals never do, and the response is restored locally
   from the merge of the two request-scoped tables — the scrub table the
   composed snippet carries (D3) and the tokenizer's, the tokenizer's entries
   winning on a collision as the more specific claim about the text. The two
   tables cannot collide on a label the scrubber reserves: ``tokenize``
   refuses such a caller lexicon (ruling R-T7-2), because a marker both tables
   claimed could be re-identified to a lexicon value that never left the
   snippet. Restore is strict (ruling T7/review focus 6): a value the model
   returns that is neither a known token nor a declared literal — a value
   literal it never saw, or a placeholder this call never minted — refuses the
   answer (``invented_value``) instead of passing it through; free prose is
   not a value, and the task's own grammar judges it. Ruling
   D4: neither table reaches the audit chain, a log, the database, or the
   wire.

The digest pair (Q1.5) closes the module: ``snippet_digest`` produces the
bounded prompt summary the audit trail may carry — a normalized
≤500-character prefix of, and the SHA-256 of the exact bytes of, the
*scrubbed and tokenized* outbound snippet. It is never fed the raw payload;
that contract is the caller's, stated here because the digest is the one
artifact that outlives the request.

The scrubber is deliberately deterministic and conservative. A phone is a
contact number, not a mobile alone (ruling R-B): an explicit international
form (``+``/``00``, a country code, the subscriber digits) and a bare PRC
mobile (eleven digits, ``1[3-9]`` first) carry their own evidence and need no
keyword, a bare run in the HK mobile prefixes (4-9) is masked under the ruling
below, and a bare local run in the low prefixes (1-3) is left alone — it is
indistinguishable from a date- or year-shaped run (``20261006``,
``19991231``) or from a BRN — *unless* a contact word precedes it within a
short window (``電話``, ``call``, ``phone``, …). That last rule is a function,
not a pattern, because Python's ``re`` cannot express a variable-width keyword
lookbehind; it drives the same ``_ScrubTable`` as every pattern does. The same
function masks a long digit run (8-17) as an account number when a word before
it — an account label, a transfer verb, or a licensed bank's name (ruling R-C)
— says so within that window; a run with no such anchor still transits, which
is the recorded residual (D5). The OFCA numbering plan lists the HK mobile
families at 4-9; ruling R-T4-5 widened the family under D6 while that list was
still unverified, and 1-3 are spared so date- and year-shaped runs survive. An
address-ish or ID-ish shape the patterns cannot cleanly consume fails closed
rather than passing through. False refusals route to staff and templates; false
sends route to the PCPD.

Matching normalization is a matcher's tool, never a wire step.
``normalize_for_matching`` produces the comparison form — NFKC width folding,
``casefold``, whitespace collapse, and the bounded ``CJK_VARIANTS``
simplified→traditional script fold — that a caller compares client text
against the tenant's own lexicon with. The scrub and tokenize paths use it to
*find* a span, never to build the wire form: detection runs on the comparison
form and the replacement rewrites that span in the original text, so every
character outside a replaced span stays byte-for-byte the client's or staff's
own writing.
``CJK_VARIANTS`` is deliberately bounded — it carries only the pairs this
module's tests exercise — because full traditional↔simplified coverage needs a
CJK conversion dependency (``opencc`` or similar) and none is approved (the
tokenized-outbound plan's global constraint 1). An unlisted character compares
as itself, so the matcher's failure mode is a missed match, never a guessed or
invented one.

Pure and framework-free per ADR-0006: stdlib ``hashlib``, ``re`` and
``unicodedata`` only. Test data is synthetic throughout — no real person's
data appears here or in the tests.
"""

import hashlib
import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from app.domain.ai_gateway import TaskType

# The composed snippet's hard size cap (the plan's 16 KiB): a task-scoped,
# single-subject snippet has no honest reason to approach this.
SNIPPET_MAX_BYTES = 16 * 1024

# Q1.5: the prompt summary written to the audit trail is capped at creation.
DIGEST_MAX_CHARS = 500

# Q1.2's hard exclusions — the fields that never appear in ANY profile's
# allow-list. ``hkid_partial``/``passport_partial`` sit here with the full
# identifiers on purpose: a partial ID alone is a public-page value, but
# name-plus-partial-ID is the combination regulators treat as sensitive and
# it does not transit.
EXCLUDED_FIELDS = frozenset(
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
)


class RedactionRefused(Exception):
    """Fail-closed refusal: the snippet cannot be made defensible.

    ``reason`` is the stable machine-readable name (``field_not_allowed``,
    ``client_content_forbidden``, ``company_anchor_required``,
    ``anchor_not_permitted`` (the split profile's no-anchor shape, refusing
    an offered anchor), ``fields_required``, ``snippet_too_large``,
    ``unscrubbable_protected_data``, ``question_required``,
    ``non_client_text_forbidden``, ``invented_value``); ``detail`` names the
    field, class, or size at issue. Every reason but ``invented_value`` means
    nothing was sent — it is raised before the attempt. ``invented_value`` is
    raised while restoring an answer the model already returned: the snippet
    left and the attempt is audited, but the answer is refused, so nothing
    derived from it is stored.
    """

    def __init__(self, reason: str, *, detail: str = "") -> None:
        self.reason = reason
        self.detail = detail
        super().__init__(reason if not detail else f"{reason}: {detail}")


# --- Matching normalization (comparison forms, never wire forms) ---
#
# One caller-side job: when the tenant itself knows the exact value (an
# officer name, a company name) and only the *spelling* the client used
# varies, the two are compared in this normalized form. It is a matcher's
# form: the snippet the model reads never passes through it.

# A deliberately bounded simplified→traditional script fold. Traditional is
# the record form — the register, the forms and the master data are written
# that way — so a simplified spelling folds onto it and both spellings compare
# equal. Coverage is bounded on purpose: only the pairs the module's tests
# exercise live here, because a faithful full traditional↔simplified engine
# means adopting a CJK conversion dependency (``opencc`` or similar) and no
# such dependency is approved (the plan's global constraint 1). A character
# absent from the table compares as itself: the honest failure mode for a
# matcher is a missed match, never a wrong one.
CJK_VARIANTS: Mapping[str, str] = {
    "陈": "陳",  # the surname the name-matching tests pin (陈大文 / 陳大文)
    "丽": "麗",  # the village/estate address case (丽和楼)
    "楼": "樓",
    # Workstream ④ T2 (ruling D3): the pairs the simplified-script canary
    # cases and keyword pins exercise, folded onto the Traditional record
    # form like the three above. A small reviewed table on purpose — full
    # traditional↔simplified coverage means ``opencc``, which is not approved.
    "护": "護",  # 护照
    "电": "電",  # 电话 / 致电
    "话": "話",  # 电话
    "机": "機",  # 手机
    "网": "網",  # 固网
    "络": "絡",  # 联络
    "转": "轉",  # 转账
    "账": "賬",  # 转账 / 入账
    "汇": "匯",  # 汇款
    "银": "銀",  # 银行户口
    "户": "戶",  # 银行户口 / 户口
    "证": "證",  # 身份证
    "号": "號",  # 号码 / …号
    "龙": "龍",  # 九龙
    "单": "單",  # 单位
    "区": "區",  # 南山区
    "栋": "棟",  # mainland unit marker (T3)
}


def normalize_for_matching(text: str) -> str:
    """The comparison form of ``text`` for lexicon matching, never a wire form.

    Four folds in order: NFKC (full-width ASCII and digits to their half-width
    equivalents, the ideographic space to a space), ``str.casefold``,
    whitespace collapse (any run of whitespace — newlines and tabs included —
    to one space), then the bounded ``CJK_VARIANTS`` script fold character by
    character. Callers compare lexicon values against *this* form of the text;
    the text itself is never rewritten, which is what keeps the snippet the
    model reads byte-for-byte the client's or staff's own writing.

    Non-ASCII case folding can change length (``ß`` → ``ss``) and NFKC can
    expand a character too, so the result is a comparison key, not something to
    map back onto the original string.
    """
    folded = unicodedata.normalize("NFKC", text).casefold()
    collapsed = re.sub(r"\s+", " ", folded)
    return "".join(CJK_VARIANTS.get(char, char) for char in collapsed)


# --- The deterministic scrubber ---
#
# Two kinds of entries. ``_SCRUB_PATTERNS`` replace their match with a
# ``[CLASS_nn]`` marker; every marker is shaped so no pattern in either list
# can match it, which makes re-scrubbing idempotent. ``_PROTECTED_DETECTORS``
# match protected-class shapes that must never survive scrubbing but that the
# replacer will not guess at (an HKID body without its check digit, an
# address fragment without a street/district shape): their presence after
# the scrub pass is the fail-closed case.
#
# Both lists are matched against ``normalize_for_matching``'s comparison form,
# so a spaced, full-width, or otherwise folded spelling is the same shape to a
# detector; a replacement rewrites the original span the match came from, and
# the rest of the client's own writing is untouched.

# The marker family the two passes emit and the strict restore recognises:
# a label from the closed vocabularies, an underscore, then digits. The passes
# mint two-digit ordinals (``{number:02d}``), but recognition covers the whole
# family — a one-digit ordinal (``[PERSON_1]``) is marker-intent text a pass
# never wrote, and the strict restore refuses it as an invention rather than
# passing malformed marker text through to a client-facing artifact (workstream
# ④ T1, residual N3). The same shape fences both passes: a span that is already
# a marker when a pass runs is opaque to it, so neither a scrub pattern nor a
# caller lexicon value may rewrite one (residual N2).
_MARKER_SHAPE = re.compile(r"\[[A-Z][A-Z0-9_]*_\d+\]")


def _fenced_marker_spans(text: str) -> tuple[tuple[int, int], ...]:
    """The source spans of the marker-shaped text already present in ``text``.

    Markers are pure ASCII, which every matching fold leaves byte-for-byte
    intact, so raw-text spans line up with the source spans the normalized-form
    matchers map back to. One helper, both passes (ruling D1 of workstream ④).
    """
    return tuple((match.start(), match.end()) for match in _MARKER_SHAPE.finditer(text))

# The English address pattern's district tail: every HK district family,
# Island as well as Kowloon/NT — a number+street with an Island tail is no
# less an address than one ending "Kowloon".
_HK_DISTRICT_TAIL = (
    r"kowloon|hong\s+kong|new\s+territories|central|mid-levels|"
    r"sheung\s+wan|wan\s+chai|causeway\s+bay|kennedy\s+town|"
    r"sai\s+ying\s+pun|happy\s+valley|aberdeen|stanley"
)

# A bare eight-digit run in the higher prefixes (4-9): the free-text phone shape
# that carries no separator. The low prefixes (1-3) are left alone so date- and
# year-shaped runs are not shredded — unless a contact keyword precedes them,
# which is the context rule below; the OFCA numbering plan lists the mobile
# families at 4-9 (verified 2026-10-07, ratifying ruling R-T4-5, which widened
# the family under D6 while the list was still unverified). Named separately
# because the composed company anchor — a declared BRN
# (``company.br_number``) — is scrubbed without this entry: an
# eight-digit BRN can share a prefix, and re-typing the anchor as a phone would
# be a D2 type error.
_BARE_MOBILE_PATTERN = re.compile(r"(?<!\d)[4-9]\d{7}(?!\d)")

# The contact-context rule's two halves (ruling R-B): the words that make a
# bare local number a phone number rather than a date, a BRN or a reference,
# and how far back one of them may sit. The pair drives ``_context_masked_spans``
# beside ``_SCRUB_PATTERNS`` rather than inside it, because a variable-width
# keyword lookbehind has no ``re`` expression. The vocabulary is the ruling's
# contact list; the window is counted in normalized characters of the text the
# rule reads (``normalize_for_matching``), so a full-width or multi-space
# spelling does not shorten the keyword's reach. The ASCII alternatives are
# word-bounded (this slice): ``tel``, ``mobile`` and ``office`` are the words
# themselves, not substrings of ``hotel``, ``automobile`` or ``officer``. The
# CJK alternatives keep no boundary — CJK prose is not space-delimited and
# Python's word boundary would not mean the same thing — so ``電話`` anchors
# through a run as before.
_CONTACT_ANCHOR_PATTERN = re.compile(
    r"電話|電話號碼|手機|固網|辦公室|住址|聯絡|致電|"
    r"电话|电话号码|手机|固网|办公室|联络|致电|"
    r"\btel\b|\btelephone\b|\bphone\b|\bmobile\b|\boffice\b|\bcontact\b|\bcall\b",
    re.IGNORECASE,
)
CONTACT_WINDOW_CHARS = 24

# The bare local fixed-line shape: eight digits in the low prefixes (2-3).
# Unlike the mobile prefixes it carries nothing a date or a BRN does not, so
# it is a phone only under the contact keyword above (R-B).
_FIXED_LINE_PATTERN = re.compile(r"(?<!\d)[23]\d{7}(?!\d)")

# --- The address geo-vocabulary (workstream ④, rulings D4/D5/D6) -------------
#
# Geographic names are an ANCHOR family, never a class (ruling D4): a district,
# locality, province, or country name alone masks nothing and refuses nothing —
# "我上月去日本出差" is prose. A geographic name within a bounded window before
# a shape fragment extends and confirms the mask, the same anchor+window
# architecture as the context rules above. The vocabulary lists are bounded
# for v1.0 (ruling D5): the 18 HK districts, a bounded locality list, the 34
# mainland provincial-level divisions in both scripts (plus the administrative
# suffix shape — the 市-suffix carries 深圳市 generically, so no city
# enumeration), and a ~20-country subset matching HK TCSP client reality,
# including the offshore-register contexts the CR work sees. The lists are
# pinned here, beside the only consumer, and sorted longest-first so a longer
# name wins where one prefixes another (沙田區 before 沙田).

_HK_DISTRICT_NAMES: tuple[str, ...] = (
    "中西區", "灣仔區", "東區", "南區", "油尖旺區", "深水埗區", "九龍城區",
    "黃大仙區", "觀塘區", "葵青區", "荃灣區", "屯門區", "元朗區", "北區",
    "大埔區", "沙田區", "西貢區", "離島區",
)

_HK_LOCALITY_NAMES: tuple[str, ...] = (
    "旺角", "尖沙咀", "銅鑼灣", "中環", "上環", "沙田", "大圍", "將軍澳",
    "馬鞍山", "天水圍", "青衣", "荃灣", "元朗", "屯門", "上水", "粉嶺",
    "大埔", "西貢", "觀塘", "紅磡", "土瓜灣", "新蒲崗", "黃大仙", "鑽石山",
    "葵涌", "美孚", "荔枝角", "長沙灣", "何文田",
)

_MAINLAND_PROVINCE_NAMES: tuple[str, ...] = (
    "北京市", "天津市", "河北省", "山西省", "內蒙古自治區", "内蒙古自治区",
    "遼寧省", "辽宁省", "吉林省", "黑龍江省", "黑龙江省", "上海市",
    "江蘇省", "江苏省", "浙江省", "安徽省", "福建省", "江西省",
    "山東省", "山东省", "河南省", "湖北省", "湖南省", "廣東省", "广东省",
    "廣西壯族自治區", "广西壮族自治区", "海南省", "重慶市", "重庆市",
    "四川省", "貴州省", "贵州省", "雲南省", "云南省", "西藏自治區",
    "西藏自治区", "陝西省", "陕西省", "甘肅省", "甘肃省", "青海省",
    "寧夏回族自治區", "宁夏回族自治区", "新疆維吾爾自治區",
    "新疆维吾尔自治区", "台灣省", "台湾省", "香港特別行政區",
    "香港特别行政区", "澳門特別行政區", "澳门特别行政区",
)

_COUNTRY_NAMES: tuple[str, ...] = (
    "中國", "中国", "澳門", "澳门", "台灣", "台湾", "新加坡", "馬來西亞",
    "马来西亚", "英國", "英国", "美國", "美国", "加拿大", "澳洲", "澳大利亚",
    "新西蘭", "新西兰", "日本", "韓國", "韩国", "泰國", "泰国", "越南",
    "印尼", "印度尼西亚", "菲律賓", "菲律宾", "印度", "阿聯酋", "阿联酋",
    "BVI", "英屬維爾京群島", "開曼群島",
)

# The geographic anchor family: the enumerated names (longest first) plus the
# administrative-suffix shape (…省/…市/…区|區/…縣|县). The suffix shape's name
# run is bounded at 2-6 characters on both sides: a one-character name plus
# 市 (上市) is the thickest prose surface on the short side, and a 6-character
# cap on the long side keeps the greedy run from swallowing its neighbours
# (搬到北京市海淀區 as one anchor) — the enumerated names cover the long
# administrative names (香港特別行政區) directly. The combination rule below
# still requires shape evidence before anything masks.
_GEO_ANCHOR_PATTERN = re.compile(
    "|".join(
        sorted(
            set(
                _HK_DISTRICT_NAMES
                + _HK_LOCALITY_NAMES
                + _MAINLAND_PROVINCE_NAMES
                + _COUNTRY_NAMES
            ),
            key=len,
            reverse=True,
        )
    )
    + r"|[一-龥]{2,6}(?:省|市|区|區|县|縣)"
)

# How far a geographic name may sit before its shape fragment and still count
# as one address — tighter than the digit rules' 24: address fragments follow
# their names closely, and a tight window is the over-tokenization bar's first
# defence (ruling D4's combination, D7's measured rate).
GEO_WINDOW_CHARS = 12

# The shape fragments a geographic name combines with (ruling D4). Each is
# shape-evidence, never a bare word: a street fragment requires its number
# (a marker alone is prose — 道路/街道 are ordinary words), a name+unit
# fragment requires a multi-digit or lettered unit, the family fragment is the
# replacer's R-A' shape verbatim, and the postal fragment is the mainland
# six-digit shape. Bare 座/栋/幢 are deliberately absent — they are measure
# words in prose ("有23座寫字樓") and stay claimable only behind a family
# token, exactly as the replacer rules them.
_GEO_FRAGMENT_PATTERN = re.compile(
    r"[一-龥]{1,8}(?:街|道|路|巷|里|弄)\s*\d{1,4}"
    r"(?:\s*弄\s*\d{1,4}\s*(?:號|号)?|\s*(?:號|号|室|單位|单位|單元|单元|座))?"
    r"|[一-龥]{1,8}(?:\d{2,4}|[a-zA-Z])(?:室|單位|单位|單元|单元|號|号)"
    r"|(?:村|邨|苑|大廈|大厦|大樓|大楼|小區|小区|樓|栋|棟|幢)"
    r"[一-龥0-9a-z]{0,8}(?:\d|[a-zA-Z])"
    r"(?:室|單位|单位|單元|单元|號|号|座|栋|棟|幢)"
    r"|\d{6}"
)

# The long-number context rule's two halves (ruling R-C): the words that make a
# long digit run an account/bank number rather than a reference or a filing
# number, and the run itself. The account words and the transfer verbs are
# CJK-first with the English account label alongside; the licensed bank names
# are the ruling's list, so a run beside a bank's name is masked even when no
# account word is written (``匯豐 1234567890``). The ASCII alternatives are
# word-bounded for the same reason ``_CONTACT_ANCHOR_PATTERN``'s are (T1):
# ``account`` is the word, not a substring of ``accountant``, and ``BEA`` is
# not the start of ``beauty``. The CJK alternatives keep no boundary — CJK
# prose is not space-delimited — so ``匯豐`` anchors through a run. The rule is
# additive to the keyword-in-pattern ``BANK`` entry, which still claims the
# adjacent account-word shapes; this anchor reaches what that pattern cannot.
# Anchoring is a rule, not a licence to mask every long number: an unanchored
# run still transits (the recorded residual, ruling D5).
_NUMBER_ANCHOR_PATTERN = re.compile(
    r"銀行戶口|戶口|存入|轉賬|轉帳|入賬|匯款|"
    r"银行户口|户口|转账|入账|汇款|"
    r"匯豐|滙豐|汇丰|中銀|中國銀行|中国银行|渣打|恒生|東亞|东亚|花旗|星展|"
    r"\baccount\b|\ba/c\b|\bHSBC\b|\bstandard\s+chartered\b|\bhang\s+seng\b|"
    r"\bBEA\b|\bCitibank\b|\bDBS\b",
    re.IGNORECASE,
)

# The digit run the long-number rule guards (ruling R-C): eight digits or
# more, digit-fenced on both sides so a shorter reference is not claimed. The
# floor is the account-number width the ruling names — an eight-digit BRN
# shares the shape, which is why the mask waits for an anchor word before the
# run. There is deliberately no ceiling (workstream ④ T4): the old 17-digit
# bound did not clip an 18+ run but let the whole run transit — the fence
# fails at digit 18, so no sub-run matches. Once an anchor certifies the run,
# its length is the bank's own choice and the whole run masks.
_LONG_NUMBER_PATTERN = re.compile(r"(?<!\d)\d{8,}(?!\d)")

# The context-anchored rules, in the order they run: ``(label, anchor, window,
# digit pattern)``. One entry per shape whose evidence is a nearby word rather
# than its own syntax, so "the text says contact" (or "account") stays a single
# mechanism. Every span these rules produce is marked through the same
# ``_ScrubTable``, so a context mask is as reversible as a pattern match. The
# phone rule runs first: it is the more specific eight-digit shape, so a run it
# claims is gone before the wider long-number rule reads the text.
_CONTEXT_RULES: tuple[tuple[str, re.Pattern[str], int, re.Pattern[str]], ...] = (
    ("PHONE", _CONTACT_ANCHOR_PATTERN, CONTACT_WINDOW_CHARS, _FIXED_LINE_PATTERN),
    ("BANK", _NUMBER_ANCHOR_PATTERN, CONTACT_WINDOW_CHARS, _LONG_NUMBER_PATTERN),
)

_SCRUB_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "HKID",
        # Spacing between the characters is admitted (the normalized form
        # collapses runs; the original span is what is replaced), so
        # "A 1 2 3 4 5 6 (7)" is the same identifier as "A123456(7)". NFKC
        # folds a full-width spelling onto this form too.
        re.compile(r"\b[A-Z]{1,2}\s*\d(?:\s*\d){5}\s*\(\s*[0-9A]\s*\)", re.IGNORECASE),
    ),
    (
        "PASSPORT",
        re.compile(
            r"(?:\bpassport\b|護照|护照)\s*(?:no\.?|number|num|號碼|号码)?\s*[:：#]?\s*[A-Z]{1,2}\d{6,9}\b",  # noqa: RUF001
            re.IGNORECASE,
        ),
    ),
    (
        "DOB",
        re.compile(
            r"(?:\b(?:date of birth|d\.?o\.?b\.?|born)\b|出生日期|出生)"
            r"\s*[:：]?\s*\d{4}\s*[-/年.]\s*\d{1,2}\s*[-/月.]\s*\d{1,2}\s*日?",  # noqa: RUF001
            re.IGNORECASE,
        ),
    ),
    (
        "BANK",
        re.compile(
            r"(?:\b(?:bank\s+account|account|a/c)(?![a-z0-9])|銀行戶口|戶口|银行户口|户口)"
            r"\s*(?:no\.?|number|num|號碼|号码)?\s*[:：#]?\s*\d{8,17}\b",  # noqa: RUF001
            re.IGNORECASE,
        ),
    ),
    (
        "PHONE",
        # The explicit international form: a ``+``/``00`` trunk prefix, a
        # country code and grouped subscriber digits ("+86 138 1234 5678",
        # "+1 415 555 0132", "0044 20 7946 0958"). Ordered ahead of the
        # separated local form below because a grouped international tail
        # contains a 4-4 local split ("555 0132"): the weaker rule would clip
        # that tail and leave the country and area codes on the wire. The one
        # phone shape whose own syntax is the evidence, so it needs no keyword
        # (R-B's first shape in order of confidence). The optional
        # parenthesized-zero trunk group admits the spaced-trunk form common
        # in written mainland numbers ("+86 (0)10 …", workstream ④ T4):
        # without it the pattern matched only the subscriber tail and left
        # the country and area codes on the wire. The group needs the ``+``/
        # ``00`` lead and the grouped digits that follow, so a bare "(0)"
        # in prose is not a phone shape.
        re.compile(
            r"(?<!\d)(?:\+|00)\d{1,3}\s*(?:\(\s*0\s*\)\s*)?[\s-]?\d{2,4}"
            r"(?:[\s-]?\d{3,4}){1,3}(?!\d)"
        ),
    ),
    (
        "PHONE",
        # A bare PRC mobile: eleven digits, ``1[3-9]`` then nine more, with no
        # separator and no keyword ("13812345678", or "+8613812345678" in the
        # form above). A shape no date, BRN or reference number shares, so it
        # is evidence of itself (R-B's second shape).
        re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"),
    ),
    (
        "PHONE",
        # The separated forms that need no keyword: a local 4-4 split, or the
        # 852 country code. The bare mobile run is its own PHONE entry below.
        re.compile(r"(?<!\d)(?:\+?852[\s-]\d{4}[\s-]?\d{4}|\d{4}[\s-]\d{4})(?!\d)"),
    ),
    (
        "EMAIL",
        re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
    ),
    (
        "PASSPORT",
        # A bare passport-shaped token: letters plus a digit run longer than
        # a six-digit HKID body, so the two shapes do not collide. Ordered
        # after EMAIL so an address whose local part ends in digits is
        # consumed whole rather than clipped. Ruling D6: mask an
        # unrecognized ID-shaped token rather than let it through.
        re.compile(r"\b[A-Z]{1,2}\d{7,9}\b", re.IGNORECASE),
    ),
    (
        "PHONE",
        # The bare mobile run: free text only — the company anchor is scrubbed
        # without this entry (see the pattern constant's comment).
        _BARE_MOBILE_PATTERN,
    ),
    (
        "ADDRESS",
        re.compile(
            r"(?:\bflat\s+[a-z0-9]{1,4}\s*,?\s*(?:\d{1,2}\s*/\s*f\s*,?\s*)?)?"
            r"\b\d{1,4}[a-z]?\s+(?:[a-z][a-z.'-]*\s+){0,5}"
            r"(?:street|st\.?|road|rd\.?|avenue|ave\.?|lane|ln\.?|drive|dr\.?|terrace|court)\b"
            r"(?:\s*[,，]\s*[^,，\n]+)*?"  # noqa: RUF001
            rf"\s*[,，]?\s*[^,，\n]*?\b(?:{_HK_DISTRICT_TAIL})\b",  # noqa: RUF001
            re.IGNORECASE,
        ),
    ),
    (
        "ADDRESS",
        # The mainland lane shape (ruling D6): a street name and number, then
        # 弄 (the lane marker) and the lane number — "南京西路12弄5号". Runs
        # before the plain street pattern below because its number+lane+number
        # tail would otherwise clip at the first number.
        re.compile(
            r"(?:香港|九龍|新界|九龙)?[^，。；;\n]{0,20}?"  # noqa: RUF001
            r"[一-龥]{1,6}(?:街|道|路|巷|里)\s*\d{1,4}\s*弄\s*\d{1,4}\s*(?:號|号)"
            r"[^，。；;\n]*"  # noqa: RUF001
        ),
    ),
    (
        "ADDRESS",
        re.compile(
            r"(?:香港|九龍|新界|九龙)?[^，。；;\n]{0,20}?"  # noqa: RUF001
            r"[一-龥]{1,6}(?:街|道|路|巷|里)\s*\d+\s*(?:號|号)[^，。；;\n]*"  # noqa: RUF001
        ),
    ),
    (
        "ADDRESS",
        # The village/estate/building family: no street name and no 號, so
        # the two patterns above miss it. A building-family token leads the
        # span — with a locality/building name before it (太和村), or bare
        # when the token is not the bare 樓/楼, which the floor rule below
        # owns ("3樓302室") — then a short unspaced run to a unit marker,
        # whose designator immediately before the marker is a digit or a
        # single Latin letter. The run stops at a space, so an English tail
        # is not swallowed. Ruling R-A' (the final review, restoring the
        # coverage R-A's digit-only rule dropped): the designator is a digit
        # OR a single Latin letter — ``17A室``, ``A室``, ``B單位``, ``5座`` —
        # and 座 is a recognised marker, so letter-unit and 座 addresses mask
        # again. Workstream ④ T2 carries every CJK entry bilingually:
        # 村/邨/苑/大廈/大厦/大樓/大楼/小區/小区 (T4 adds the mainland
        # residential-compound token the settled lists did not carry) and the
        # markers 室/單位/单位/單元/单元/樓/號/号/座/栋/棟/幢 (ruling D6).
        # Ordinary prose with no designator before a recognised marker
        # ("本村樓上開會", "村口樓下有巴士", "大廈管理費已繳") passes untouched.
        re.compile(
            r"(?:香港|九龍|新界|九龙)?(?:[一-龥]{1,6}(?:村|邨|苑|大廈|大厦|大樓|大楼|小區|小区|樓)|(?:村|邨|苑|大廈|大厦|大樓|大楼|小區|小区))"
            r"[一-龥0-9a-z]{0,8}(?:\d|[a-zA-Z])(?:室|單位|单位|單元|单元|樓|號|号|座|栋|棟|幢)"
        ),
    ),
    (
        "ADDRESS",
        # The bare floor-plus-unit fragment: no building or street name leads
        # it ("3樓302室"), so the patterns above miss it. Ruling R-A makes it
        # address-class in its own right — its floor and unit numbers are the
        # evidence — so it is masked rather than left in the clear. The floor
        # marker is the CJK 樓/楼 or the Latin "/F"/"F"; the unit marker is
        # 室, 單位/单位 or 號/号.
        re.compile(
            r"(?<!\d)\d{1,3}\s*(?:樓|楼|/\s*[fF]\.?|[fF])\s*\d{1,4}\s*(?:室|單位|单位|號|号)"
        ),
    ),
)

# The company anchor's rule set: every scrub pattern except the free-text
# bare-mobile one. The anchor is the company row's declared BRN, so a bare
# eight-digit anchor keeps its BRN shape for the typed tokenizer; every other
# shape — a volunteered address, an obfuscated identifier — still scrubs it
# (Task 3's fix round: an unscrubbed anchor was a leak path). The bare-mobile
# entry is the one shape a declared BRN can share, so it is the whole of the
# carve-out; the context rules run for the anchor as they do for a field value
# (a declared BRN carries no contact keyword and no account or bank word to
# fire either, so a bare anchor keeps its BRN shape).
_ANCHOR_SCRUB_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (label, pattern)
    for label, pattern in _SCRUB_PATTERNS
    if pattern is not _BARE_MOBILE_PATTERN
)

# The marker labels the scrub pass can emit. The tokenizer's placeholders share
# the ``[LABEL_nn]`` namespace, so a caller lexicon keyed under one of these
# labels could mint a marker the scrub table also owns; the merged restore table
# would then re-identify a scrub marker to a lexicon value that never left the
# snippet. Ruling R-T7-2: ``tokenize`` refuses such a label (``ValueError``, the
# same programming-error signal as an unknown label under R-T4-2) rather than let
# the two tables collide.
SCRUB_LABELS: frozenset[str] = frozenset(label for label, _ in _SCRUB_PATTERNS)

# Protected-class detectors for the verify pass. A match after scrubbing
# means the scrubber could not make the text defensible without guessing;
# the gateway refuses instead.
_PROTECTED_DETECTORS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "hkid",
        re.compile(
            r"(?:\b(?:hkid|hk\s*id|identity\s+card|id\s*(?:no\.?|number|card)?)\b|身份證|身份证)"
            r"[^\n\d]{0,16}[A-Z]{1,2}\d{6}\b(?!\()",
            re.IGNORECASE,
        ),
    ),
    (
        "address",
        re.compile(
            r"(?:\b(?:residential\s+address|home\s+address|address)\b|住址|地址)"
            r"\s*[:：][^\n]{0,40}?"  # noqa: RUF001
            r"(?:\bflat\b|\broom\b|\bunit\b|\bfloor\b|street|road|avenue|lane|drive|terrace"
            r"|court|樓|楼|室|號|号|單位|单位|街|道|路)",
            re.IGNORECASE,
        ),
    ),
    (
        "street_address",
        re.compile(
            r"(?<!\w)\d{1,4}[a-z]?\s+(?:[a-z][a-z.'-]*\s+){0,5}"
            r"(?:street|st\.?|road|rd\.?|avenue|ave\.?|lane|ln\.?|drive|dr\.?|terrace"
            r"|court)(?![a-z-])",
            re.IGNORECASE,
        ),
    ),
    (
        "cjk_address_family",
        # The village/estate/building residue the replacer will not certify:
        # a building-family token (村|邨|苑|大廈|大厦|大樓|大楼|小區|小区)
        # followed by a designator
        # — a digit or a single Latin letter (ruling R-A') — and a recognised
        # unit marker within a short run fails the compose closed, as the
        # English address fragment does. The designator and marker set mirror
        # the replacer's, so prose with no designator before a recognised
        # marker is not an address: it neither masks nor refuses. A bare
        # floor-plus-unit fragment ("3樓302室") leads with a number rather than
        # the family token, so this detector does not anchor on it — the
        # replacer masks it directly.
        re.compile(
            r"(?:村|邨|苑|大廈|大厦|大樓|大楼|小區|小区)[一-龥0-9a-z]{0,8}(?:\d|[a-zA-Z])(?:室|樓|楼|單位|单位|單元|单元|號|号|座|栋|棟|幢)"
        ),
    ),
)


@dataclass(frozen=True)
class ScrubbedText:
    """A scrubbed text and the re-identification key for the masks in it.

    ``tokens`` maps each ``[CLASS_nn]`` marker the pass wrote to the exact
    original span it replaced — what makes a mask revocable rather than merely
    gone (ruling D3). It lives only in request-scoped memory (ruling D4).
    """

    text: str
    tokens: Mapping[str, str]


class _ScrubTable:
    """One pass's marker numbering and its re-identification key.

    ``mark`` numbers a match per class in the order the pass reaches it —
    left to right within a pattern, and the context rules after the patterns —
    and records the span the marker replaced. ``scrub_text`` gives each call its
    own table; ``compose_snippet`` threads one table through the anchor and
    every field in the order the wire text reads, so a marker names exactly one
    value across the whole composed snippet — two fields cannot both claim
    ``[EMAIL_01]`` for different originals, which would make a restore a coin
    flip. Request-scoped memory only (ruling D4).
    """

    def __init__(self) -> None:
        self.counters: dict[str, int] = {}
        self.tokens: dict[str, str] = {}

    def mark(self, label: str, matched: str) -> str:
        """The next ``[LABEL_nn]`` marker for ``label``, and its table entry."""
        number = self.counters.get(label, 0) + 1
        self.counters[label] = number
        marker = f"[{label}_{number:02d}]"
        self.tokens[marker] = matched
        return marker


def scrub_text(text: str) -> ScrubbedText:
    """The scrub half of cross-cutting rule 2, and ruling D3's reversibility.

    Every recognised shape is replaced with a ``[CLASS_nn]`` marker, numbered
    per class in the order the pass reaches it, and recorded against the exact
    span it replaced. Detection runs on the normalized comparison form
    (``normalize_for_matching``) so a spaced, full-width, or otherwise folded
    spelling is the same shape as its compact one; the replacement rewrites the
    *original* span the normalized match came from, so every character a match
    did not touch stays exactly as written. The verify
    pass then re-scans the normalized form for protected-class shapes; any
    residual match raises ``RedactionRefused`` (fail closed — nothing is
    sent), because a partial identifier guessed wrong by a regex is worse than
    no call at all.

    The table is request-scoped memory (ruling D4): the scrubbed text is what
    travels, the table never leaves the request — not to the wire, the audit
    chain, a log, or the database.
    """
    table = _ScrubTable()
    return ScrubbedText(text=_scrub(text, _SCRUB_PATTERNS, table), tokens=table.tokens)


def scrub_free_text(text: str) -> str:
    """``scrub_text``'s pre-D3 signature: the scrubbed text alone.

    Kept as a thin wrapper so the callers that only need the text keep working;
    anything that has to re-identify a mask asks ``scrub_text`` for the table.
    """
    return scrub_text(text).text


def _scrub(
    text: str,
    patterns: tuple[tuple[str, re.Pattern[str]], ...],
    table: _ScrubTable,
) -> str:
    """One scrub pass — the engine behind ``scrub_text`` and the composer.

    Two stages, one table. The pattern stage applies ``patterns`` in the order
    given, each entry's matches left to right; the context stage then applies
    ``_CONTEXT_RULES``, whose shapes are phones (or account numbers) only when
    a keyword precedes them and so cannot be expressed as patterns. Running the
    patterns first lets the more explicit evidence claim its span whole — an
    international number's grouped tail is also a local 4-4 split — and the
    context rule then takes what is left. Both stages mark through the same
    ``_ScrubTable``, so a marker is numbered per label and in the order the
    pass reached it, and every mask is reversible (ruling D3). The verify pass
    closes the stage, as before.

    The rule set is a parameter only so the composer can scrub the company
    anchor with ``_ANCHOR_SCRUB_PATTERNS`` (a declared BRN, not free text), and
    the table is a parameter so one composed snippet numbers its markers in the
    order the pass reaches it; ``scrub_text`` is the free-text entry point and
    every other caller uses it.

    Marker fencing (workstream ④, ruling D1): before each pattern application
    the text's current marker spans are fenced, and a match overlapping a fence
    is skipped — the pattern may not rewrite a span that is already a marker,
    wherever the pass minted it. Skipping (rather than clipping) keeps one
    masking policy, and resuming the search one character into the rejected
    match lets a shorter legal match beside the marker still claim its span.
    """
    scrubbed = text
    for label, pattern in patterns:
        fenced = _fenced_marker_spans(scrubbed)
        scrubbed = _replace_matches(scrubbed, pattern, label, table, fenced)
    for label, anchor, window, digit_pattern in _CONTEXT_RULES:
        spans = _context_masked_spans(scrubbed, anchor, window, digit_pattern, label)
        scrubbed = _apply_spans(scrubbed, spans, table)
    geo_spans = _geo_masked_spans(scrubbed)
    scrubbed = _apply_spans(scrubbed, geo_spans, table)
    normalized = normalize_for_matching(scrubbed)
    for name, detector in _PROTECTED_DETECTORS:
        if detector.search(normalized):
            raise RedactionRefused("unscrubbable_protected_data", detail=name)
    return scrubbed


def _geo_masked_spans(
    text: str,
) -> tuple[tuple[int, int, str], ...]:
    """The geo-anchored combination (ruling D4): the source spans where a
    geographic name and a nearby shape fragment make one address.

    Runs after the pattern and context stages, on the text as it stands: any
    fragment those stages already masked lives inside a marker and cannot
    match again, which is what makes the geo stage an *extension* — it claims
    the shapes only an anchor can certify (a street fragment without its 號, a
    name+unit combination, a mainland postal code) and leaves the fragments
    the replacer owns to the replacer. Detection runs on the normalized form
    with the usual source mapping; the marker fence (ruling D1) keeps a span
    from swallowing a marker that sits between its anchor and its fragment.

    One mask per fragment, taken with its qualifying anchor by one rule: an
    anchor INSIDE the fragment's span (a name the fragment's greedy run
    absorbed — 沙田 in 沙田大圍道12) wins, earliest start first; otherwise the
    last anchor ending no earlier than ``GEO_WINDOW_CHARS`` before the
    fragment starts and no later than it starts (the anchor may touch its
    fragment, as the digit rules allow). Nearest-anchor keeps the mask minimal
    — earlier names in a run (廣東省深圳市南山區…) stay in clear, because a
    geographic name alone is not a class and masks nothing.
    """
    fenced = _fenced_marker_spans(text)
    normalized, origins = _normalized_with_source(text)
    anchors = [
        (match.start(), match.end())
        for match in _GEO_ANCHOR_PATTERN.finditer(normalized)
        if not _overlaps_span(fenced, origins[match.start()], origins[match.end() - 1] + 1)
    ]
    spans: list[tuple[int, int, str]] = []
    used_anchors: set[int] = set()
    for match in _GEO_FRAGMENT_PATTERN.finditer(normalized):
        start = match.start()
        inside = [
            (anchor_start, anchor_end, index)
            for index, (anchor_start, anchor_end) in enumerate(anchors)
            if index not in used_anchors
            and start <= anchor_start
            and anchor_end <= match.end()
        ]
        if inside:
            anchor_start, _, anchor_index = min(inside)
        else:
            before = [
                (anchor_end, index)
                for index, (_, anchor_end) in enumerate(anchors)
                if index not in used_anchors
                and start - GEO_WINDOW_CHARS <= anchor_end <= start
            ]
            if not before:
                continue
            anchor_index = max(before)[1]
            anchor_start = anchors[anchor_index][0]
        span_start = origins[anchor_start]
        span_end = origins[match.end() - 1] + 1
        if _overlaps_span(fenced, span_start, span_end) or _overlaps_span(
            [(existing_start, existing_end) for existing_start, existing_end, _ in spans],
            span_start,
            span_end,
        ):
            continue
        used_anchors.add(anchor_index)
        spans.append((span_start, span_end, "ADDRESS"))
    return tuple(spans)


def _context_masked_spans(
    text: str,
    anchor: re.Pattern[str],
    window: int,
    digit_pattern: re.Pattern[str],
    label: str,
) -> tuple[tuple[int, int, str], ...]:
    """The source spans of ``digit_pattern`` that an ``anchor`` word stands before.

    The context-anchored half of the scrubber, and the reason it is a function:
    Python's ``re`` has no variable-width lookbehind, so "a contact word in the
    last ``window`` characters before this run" has no pattern expression. Both
    patterns are matched on the normalized comparison form and every span is
    mapped back to the source text it came from (``_normalized_with_source``),
    exactly as ``_replace_matches`` does, so the replacement rewrites the
    client's own characters and nothing else. The window is counted in
    normalized characters, so a full-width or multi-space spelling is the same
    distance.

    A run qualifies when an anchor match *ends* no earlier than ``window``
    characters before the run starts, and no later than the run itself — a
    keyword that follows the number is not the evidence this rule asks for, and
    the run and the anchor may touch (``電話23456789`` is a phone).

    Returns one ``(start, end, label)`` tuple per qualifying run, in the order
    the runs appear — the caller's own label carried through, so several rules'
    spans reach ``_apply_spans`` in one form and a table entry is always the
    marker plus the exact matched text. ``re``'s scanner yields disjoint
    matches in ascending order, so the spans are ascending and non-overlapping,
    which is what lets ``_apply_spans`` slice the text in one pass.
    """
    normalized, origins = _normalized_with_source(text)
    anchor_ends = [match.end() for match in anchor.finditer(normalized)]
    spans: list[tuple[int, int, str]] = []
    for match in digit_pattern.finditer(normalized):
        start = match.start()
        if not any(start - window <= end <= start for end in anchor_ends):
            continue
        spans.append((origins[start], origins[match.end() - 1] + 1, label))
    return tuple(spans)


def _replace_matches(
    text: str,
    pattern: re.Pattern[str],
    label: str,
    table: _ScrubTable,
    fenced: Sequence[tuple[int, int]] = (),
) -> str:
    """Replace ``pattern``'s matches in ``text``, detected on the normalized form.

    The pattern is applied to ``normalize_for_matching``'s comparison form and
    each normalized match is mapped back — through ``_normalized_with_source``
    — to the source span it came from, which is what the marker replaces and
    what the table records against that marker. A match overlapping an earlier
    span or a fenced (already-marker) span is skipped, so the earliest legal
    match wins exactly as ``re.sub`` would have it; the search resumes one
    character into a rejected match, so a shorter legal match beside a fence
    still masks. The masking itself is ``_apply_spans``, which the context
    stage shares.
    """
    normalized, origins = _normalized_with_source(text)
    spans: list[tuple[int, int, str]] = []
    cursor = 0
    position = 0
    while True:
        match = pattern.search(normalized, position)
        if match is None:
            break
        start = origins[match.start()]
        end = origins[match.end() - 1] + 1
        if start < cursor or _overlaps_span(fenced, start, end):
            position = match.start() + 1
            continue
        spans.append((start, end, label))
        cursor = end
        position = match.end()
    return _apply_spans(text, spans, table)


def _apply_spans(
    text: str,
    spans: Sequence[tuple[int, int, str]],
    table: _ScrubTable,
) -> str:
    """The one mask writer: replace each span with the next marker for its label.

    ``spans`` must be in ascending, non-overlapping order — what both callers
    produce. Each marker is minted by ``table.mark`` against the exact text it
    replaces, which is the whole of ruling D3's reversibility: a pattern match
    and a context match become table entries the same way, so a marker the
    model echoes back is restorable whether a regex or the keyword rule put it
    there.
    """
    if not spans:
        return text
    pieces: list[str] = []
    cursor = 0
    for start, end, label in spans:
        pieces.append(text[cursor:start])
        pieces.append(table.mark(label, text[start:end]))
        cursor = end
    pieces.append(text[cursor:])
    return "".join(pieces)


# --- The tokenizer (cross-cutting rule 3; ADR-0003 safeguard 3) ---

# Ruling D1's closed vocabulary: the only labels the typed tokenizer emits. A
# token names the *kind* of thing and nothing about its value (D2); the set is
# closed so a caller cannot invent a shape-bearing label or smuggle a value
# into one. The scrubber's markers and the gateway's name source assign from
# this set and nothing else.
TOKEN_LABELS: frozenset[str] = frozenset(
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
)

# Ruling D6's fuzzy floor, in normalized characters, made script-aware by
# ruling R-T5-1 and narrowed by ruling R-T5-2. Only name-like values (a
# ``PERSON`` or ``COMPANY`` label) are fuzzy-matched; identifiers (``BRN``,
# ``CR``) match exactly, because a one-digit difference is a *different*
# identifier (BRNs are issued incrementally, so a fuzzy BRN would mislabel a
# neighbouring company's real number). A value containing CJK takes the
# three-character floor — Chinese names are two to four characters, so the
# brief's own 陳大民/陳大文 case would never match at five — while a Latin
# value keeps five. Below the floor, matching is exact only, so a short surname
# or common word cannot pull a near ordinary word into a token ("Chan" does not
# fuzz "Channel", "the" does not fuzz "them"). One edit is generous at these
# lengths, and the over-tokenize ruling means a false match here costs a staff
# glance, never a leak.
_FUZZY_MIN_LENGTH = 5
_FUZZY_MIN_CJK_LENGTH = 3
_FUZZY_LABELS: frozenset[str] = frozenset({"PERSON", "COMPANY"})

# The CJK ranges behind R-T5-1's script test: the Unified Ideographs block and
# CJK Extension A. Extension A is a real home for rare name characters, and the
# floor ruling has to hold for those names too - a script test that scanned only
# U+4E00-U+9FFF would hand such a name the Latin five-character floor, and a
# two- or three-character name would never fuzzy-match.
_CJK_RANGES: tuple[tuple[str, str], ...] = (("\u3400", "\u4dbf"), ("\u4e00", "\u9fff"))


def _contains_cjk(text: str) -> bool:
    """True when ``text`` holds any CJK character (R-T5-1's script test)."""
    return any(start <= char <= end for char in text for start, end in _CJK_RANGES)


def _fuzzy_floor(needle: str) -> int:
    """The shortest normalized value R-T5-1's fuzzy pass will match."""
    return _FUZZY_MIN_CJK_LENGTH if _contains_cjk(needle) else _FUZZY_MIN_LENGTH


@dataclass(frozen=True)
class TokenizedText:
    """A tokenized snippet and its re-identification key.

    ``tokens`` maps each typed ``[LABEL_nn]`` placeholder (a label from
    ``TOKEN_LABELS``) to the exact original value. It lives only in
    request-scoped memory: the audit trail, logs, and the wire never see it.
    """

    text: str
    tokens: Mapping[str, str]


def tokenize(
    text: str,
    *,
    values: Mapping[str, Sequence[str]] = {},
) -> TokenizedText:
    """Replace known values with typed ``[LABEL_nn]`` placeholders.

    ``values`` keys a label from the closed ``TOKEN_LABELS`` vocabulary to the
    exact strings that label applies to (``{"PERSON": ["Chan Tai Man"],
    "COMPANY": [...]}``); a key outside that vocabulary raises ``ValueError``
    rather than composing a marker whose label D1 never admitted. So does a key
    the *scrubber* reserves (``SCRUB_LABELS``: ``HKID``, ``PHONE``, …) — ruling
    R-T7-2 — because the two passes share one marker namespace and a marker both
    tables claim would restore to a lexicon value that never left the snippet.
    The typed mapping is the tokenizer's one surface — the pre-typed
    ``persons=`` / ``identifiers=`` sugar is retired, because ``IDENTIFIER`` was
    the only label it could produce and it is not in the closed vocabulary.

    The values are **replacement sources only** — they are never part of the
    returned snippet, and the re-identification key lives in this call's map
    alone, a fresh map per call, so two snippets' keys never mix. A value
    enters that map only when it actually matched a span: a lexicon member the
    snippet never mentioned never transited, so no token for it is minted and
    nothing can restore it.

    Matching compares the normalized comparison form of each value against
    the normalized text (``normalize_for_matching``), so a width, case, or
    bounded simplified/traditional variant of the record's spelling still
    matches — while the snippet is rewritten only by substitution: every
    character a token does not replace stays exactly the client's or staff's
    own writing. Values are claimed longest-first within a label and numbered
    per label in that order, so a name that prefixes another does not split
    it. Boundary-aware, as before: a value glued to an ASCII letter or digit
    is not a match, so a short surname (``Chan``) does not rewrite the
    ordinary word that contains it (``Channel``) — while a Chinese name
    embedded in a run of CJK characters (where there are no spaces to bound
    words) still matches.

    Matching runs in two passes. The exact pass above is unchanged. A second
    bounded pass implements rulings D6/R-T5-1/R-T5-2: only name-like values
    (``PERSON``, ``COMPANY``) are fuzzy-matched, identifiers (``BRN``, ``CR``)
    are exact-only, and a snippet token within one edit of exactly one such
    value is replaced by that value's token. The fuzzy floor is script-aware
    (R-T5-1) — three normalized characters for a CJK value, five for a Latin
    one, exact-only below it. A token equidistant from two or more values is
    replaced by a fresh typed token whose table entry is the literal span it
    replaced: the ambiguous near miss is masked, never left in the clear, the
    restore never guesses which value it was, and — ruling R-T7-1 — the
    placeholder is restorable, returning the client's own words unchanged.
    """
    unknown = sorted(set(values) - TOKEN_LABELS)
    if unknown:
        raise ValueError("unknown token label(s): " + ", ".join(unknown))
    reserved = sorted(set(values) & SCRUB_LABELS)
    if reserved:
        raise ValueError("token label(s) reserved by the scrubber: " + ", ".join(reserved))
    fenced = _fenced_marker_spans(text)
    normalized, origins = _normalized_with_source(text)
    tokens: dict[str, str] = {}
    claims: list[tuple[int, int, str]] = []
    for label, raw_values in values.items():
        ordered = sorted(
            {value for value in raw_values if value}, key=lambda item: (-len(item), item)
        )
        for number, value in enumerate(ordered, start=1):
            token = f"[{label}_{number:02d}]"
            needle = normalize_for_matching(value).strip()
            if not needle:
                continue
            spans = _unclaimed_spans(normalized, needle, claims, origins, fenced)
            if not spans:
                continue
            tokens[token] = value
            for start, end in spans:
                claims.append((start, end, token))
    _add_fuzzy_claims(text, normalized, origins, values, claims, tokens, fenced)
    return TokenizedText(text=_apply_claims(text, claims), tokens=tokens)


def _normalized_with_source(text: str) -> tuple[str, list[int]]:
    """``normalize_for_matching(text)`` with a normalized-char → source map.

    Built character by character so every normalized position records which
    original character produced it — which is what lets a match on the
    normalized form rewrite the *original* span, keeping the rest of the text
    as written. Whitespace runs collapse to one space mapped to their first
    character. NFKC is applied per character here, which matches the bounded
    fold ``normalize_for_matching`` documents for the inputs this module
    handles; a fold it does not cover is a missed match, never a wrong one.
    """
    chars: list[str] = []
    origins: list[int] = []
    previous_was_space = False
    for index, char in enumerate(text):
        folded = normalize_for_matching(char)
        if not folded:
            continue
        if not folded.strip():
            if previous_was_space:
                continue
            chars.append(" ")
            origins.append(index)
            previous_was_space = True
            continue
        previous_was_space = False
        for piece in folded:
            chars.append(piece)
            origins.append(index)
    return "".join(chars), origins


def _unclaimed_spans(
    normalized: str,
    needle: str,
    claims: Sequence[tuple[int, int, str]],
    origins: Sequence[int],
    fenced: Sequence[tuple[int, int]] = (),
) -> list[tuple[int, int]]:
    """Source spans of ``needle`` not already claimed, left to right.

    Boundary-aware on the normalized text — the same fence the previous
    word-boundary rule applied: a match may not be glued to an ASCII letter
    or digit on either side. A span overlapping an earlier claim (a longer
    value, or another label) or a fenced marker span (a span already a marker
    when the pass ran, ruling D1 of workstream ④) is skipped, so the
    longest-first order decides.
    """
    spans: list[tuple[int, int]] = []
    cursor = 0
    while True:
        found = normalized.find(needle, cursor)
        if found < 0:
            break
        end = found + len(needle)
        before = normalized[found - 1] if found > 0 else ""
        after = normalized[end] if end < len(normalized) else ""
        if not _is_ascii_word(before) and not _is_ascii_word(after):
            start, stop = origins[found], origins[end - 1] + 1
            if (
                not _overlaps(claims, start, stop)
                and not _overlaps_span(spans, start, stop)
                and not _overlaps_span(fenced, start, stop)
            ):
                spans.append((start, stop))
                cursor = end
                continue
        cursor = found + 1
    return spans


def _is_ascii_word(char: str) -> bool:
    """The boundary fence: ASCII letters and digits, exactly as before."""
    return char.isascii() and char.isalnum()


def _overlaps(claims: Sequence[tuple[int, int, str]], start: int, end: int) -> bool:
    return any(
        not (end <= claimed_start or start >= claimed_end)
        for claimed_start, claimed_end, _ in claims
    )


def _overlaps_span(spans: Sequence[tuple[int, int]], start: int, end: int) -> bool:
    return any(not (end <= span_start or start >= span_end) for span_start, span_end in spans)


def _apply_claims(text: str, claims: Sequence[tuple[int, int, str]]) -> str:
    """Rebuild the text with each claimed span replaced by its token."""
    if not claims:
        return text
    pieces: list[str] = []
    cursor = 0
    for start, end, token in sorted(claims):
        pieces.append(text[cursor:start])
        pieces.append(token)
        cursor = end
    pieces.append(text[cursor:])
    return "".join(pieces)


def _add_fuzzy_claims(
    text: str,
    normalized: str,
    origins: Sequence[int],
    values: Mapping[str, Sequence[str]],
    claims: list[tuple[int, int, str]],
    tokens: dict[str, str],
    fenced: Sequence[tuple[int, int]] = (),
) -> None:
    """Pass 2 (rulings D6/R-T5-1/R-T5-2): over-tokenize a near miss of a name.

    Runs after the exact pass, per label and longest value first within it, so
    an exact claim always beats a fuzzy one and a longer fuzzy value beats a
    shorter. Only name-like labels (``_FUZZY_LABELS``) are visited; identifiers
    (``BRN``, ``CR``) match exactly, since a one-digit difference is a
    different identifier (R-T5-2). For every boundary-fenced window within one
    edit of a value at least ``_fuzzy_floor`` characters long — three for a CJK
    value, five for a Latin one (R-T5-1) — a window close to exactly one value
    of the label takes that value's token and registers it, exactly as an exact
    match does; a window equidistant from two or
    more takes a fresh typed token numbered past the label's lexicon entries
    whose entry is the *literal span* it replaces (ruling R-T7-1), so the mask
    is over-tokenized and still restorable — the local restore returns the
    client's own words rather than inventing which value it was. Windows
    overlapping any existing claim (exact or fuzzy) are skipped, so claims
    never overlap. Values below the floor are exact-only, so a short name
    cannot fuzzy-match a near ordinary word.
    """
    for label, raw_values in values.items():
        if label not in _FUZZY_LABELS:
            continue
        ordered = sorted(
            {value for value in raw_values if value}, key=lambda item: (-len(item), item)
        )
        token_values = {
            f"[{label}_{number:02d}]": value for number, value in enumerate(ordered, start=1)
        }
        entries = [
            (token, normalize_for_matching(value).strip())
            for token, value in token_values.items()
        ]
        entries = [
            (token, needle) for token, needle in entries if len(needle) >= _fuzzy_floor(needle)
        ]
        if not entries:
            continue
        next_unknown = len(ordered) + 1
        for _, needle in entries:
            for start, end, window in _fuzzy_spans(normalized, needle, claims, origins, fenced):
                close = _close_token(window, entries)
                if close is None:
                    token = f"[{label}_{next_unknown:02d}]"
                    next_unknown += 1
                    tokens[token] = text[start:end]
                else:
                    token = close
                    tokens[token] = token_values[token]
                claims.append((start, end, token))


def _fuzzy_spans(
    normalized: str,
    needle: str,
    claims: Sequence[tuple[int, int, str]],
    origins: Sequence[int],
    fenced: Sequence[tuple[int, int]] = (),
) -> list[tuple[int, int, str]]:
    """Source spans within one edit of ``needle``, with their normalized text.

    Bounded Levenshtein (stdlib, no dependency), never a regex over the
    lexicon. A window can differ from the value by at most one edit, so only
    three lengths are tried — ``len(needle)``, one shorter, one longer — at
    each position not fenced off on its left by an ASCII letter or digit, with
    the same fence on its right; the exact-length window is tried first so a
    substitution beats a spurious insertion, the first accepted window at a
    position wins, and a window overlapping an existing claim is passed over.
    Left-to-right and deterministic.
    """
    length = len(needle)
    spans: list[tuple[int, int, str]] = []
    for start_index in range(len(normalized)):
        if start_index > 0 and _is_ascii_word(normalized[start_index - 1]):
            continue
        for window_length in (length, length - 1, length + 1):
            end_index = start_index + window_length
            if window_length < 1 or end_index > len(normalized):
                continue
            if end_index < len(normalized) and _is_ascii_word(normalized[end_index]):
                continue
            # Cheap gate before the DP: a single edit touches at most one end of
            # a window this long, so a real match shares its first or its last
            # character with the value. Rejects most windows without slicing.
            if (
                normalized[start_index] != needle[0]
                and normalized[end_index - 1] != needle[-1]
            ):
                continue
            window = normalized[start_index:end_index]
            if not _edit_distance_within(window, needle, 1):
                continue
            start, end = origins[start_index], origins[end_index - 1] + 1
            if (
                _overlaps(claims, start, end)
                or _overlaps_span(
                    [(span_start, span_end) for span_start, span_end, _ in spans], start, end
                )
                or _overlaps_span(fenced, start, end)
            ):
                continue
            spans.append((start, end, window))
            break
    return spans


def _close_token(window: str, entries: Sequence[tuple[str, str]]) -> str | None:
    """The one token whose value is within one edit of ``window``, else ``None``.

    Distinct normalized needles are compared once; two or more distinct values
    inside the bound is ruling D6's ambiguous case and yields ``None``, which
    sends the caller to a fresh untabled token.
    """
    seen: set[str] = set()
    close: list[str] = []
    for token, needle in entries:
        if needle in seen:
            continue
        if _edit_distance_within(window, needle, 1):
            seen.add(needle)
            close.append(token)
    if len(close) == 1:
        return close[0]
    return None


def _edit_distance_within(left: str, right: str, limit: int) -> bool:
    """True when two normalized strings are within ``limit`` edits.

    Bounded Levenshtein dynamic program (stdlib only): a cheap length gate
    first, then a row-by-row table that abandons the work as soon as a whole
    row cannot come back inside the bound. The matcher needs only D6's ≤1
    answer, never the distance or an alignment, so nothing is stored beyond
    the two rolling rows.
    """
    if abs(len(left) - len(right)) > limit:
        return False
    if left == right:
        return True
    previous = list(range(len(right) + 1))
    for row_index, left_char in enumerate(left, start=1):
        current = [row_index]
        row_minimum = row_index
        for column_index, right_char in enumerate(right, start=1):
            cost = 0 if left_char == right_char else 1
            distance = min(
                previous[column_index] + 1,
                current[column_index - 1] + 1,
                previous[column_index - 1] + cost,
            )
            current.append(distance)
            if distance < row_minimum:
                row_minimum = distance
        if row_minimum > limit:
            return False
        previous = current
    return previous[-1] <= limit


# The placeholder shape sits with the deterministic scrubber above
# (``_MARKER_SHAPE``); the strict restore uses it to spot a marker this call's
# table never minted — a placeholder nothing can resolve.


def _is_invented_value(text: str) -> bool:
    """Whether an answer string carries material the model can only have made up.

    Two shapes, both of them things the model must reference through a token
    instead: a ``[LABEL_nn]`` marker this call's table does not know (a
    placeholder nothing can restore), and a *value literal* — an identifier,
    contact detail, or address shape the local scrub rules recognise, which the
    model never saw in the clear. Free prose is neither: a reply parse's
    evidence spans quote the client and its dates are computed from the reply
    by instruction, and the task's own grammar judges both. Called with the
    string after every known token has been removed, so a token the model
    legitimately echoed cannot mask a value beside it.
    """
    if _MARKER_SHAPE.search(text):
        return True
    normalized = normalize_for_matching(text)
    return any(pattern.search(normalized) for _, pattern in _SCRUB_PATTERNS) or any(
        detector.search(normalized) for _, detector in _PROTECTED_DETECTORS
    )


def restore_tokens(
    value: Any,
    tokens: Mapping[str, str],
    *,
    allowed_literals: frozenset[str] = frozenset(),
) -> Any:
    """Restore placeholders recursively over a decoded response JSON value.

    Strings are re-identified in place; mappings and lists are rebuilt with
    their members restored. A string that is a declared ``allowed_literals``
    member (the task's closed enum vocabulary) is admissible as it stands.
    Every other string is checked — after this call's known tokens are removed
    — for the two shapes it may not carry (see ``_is_invented_value``): an
    unknown placeholder, or a value literal the model never saw in the clear.
    Either raises ``RedactionRefused("invented_value")``: the answer is refused
    rather than stored. Free prose passes, because it is not a value — the
    task's own grammar owns it. Restoring is not a license to invent values.
    """
    if isinstance(value, str):
        if value in allowed_literals:
            return value
        remainder = value
        for token in tokens:
            remainder = remainder.replace(token, "")
        if _is_invented_value(remainder):
            raise RedactionRefused("invented_value")
        restored = value
        for token, original in tokens.items():
            restored = restored.replace(token, original)
        return restored
    if isinstance(value, Mapping):
        return {
            restore_tokens(key, tokens, allowed_literals=allowed_literals): restore_tokens(
                item, tokens, allowed_literals=allowed_literals
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [
            restore_tokens(item, tokens, allowed_literals=allowed_literals) for item in value
        ]
    return value


# --- The Q1.2 profiles as data ---


@dataclass(frozen=True)
class RedactionProfile:
    """One Q1.2 table row: what a task's snippet may contain.

    ``allowed_fields`` is the row's minimum-content plus conditional columns
    — the only field names ``compose_snippet`` will accept for the task.
    ``tokenize_names`` is cross-cutting rule 3 for this row: the default is
    ``True``, and a ``False`` carries ``tokenize_opt_out_reason`` — the
    justification recorded where the profile lives, not in a decision log
    someone has to remember to write. ``allows_client_content`` is ``False``
    only for ordinance Q&A: that task reasons over the statute and needs no
    client content, so the gateway sends none — its one admitted payload is
    the staff-authored, non-client question (ruling R11), which is scrubbed
    but never tokenized.
    """

    task_type: TaskType
    allowed_fields: frozenset[str]
    tokenize_names: bool
    tokenize_opt_out_reason: str | None
    allows_client_content: bool
    anchor_required: bool = True


PROFILES: Mapping[TaskType, RedactionProfile] = {
    TaskType.REPLY_PARSING: RedactionProfile(
        task_type=TaskType.REPLY_PARSING,
        allowed_fields=frozenset({"reply_text", "role_reference", "officer_name"}),
        tokenize_names=True,
        tokenize_opt_out_reason=None,
        allows_client_content=True,
    ),
    TaskType.CONFIRMATION_REQUEST: RedactionProfile(
        task_type=TaskType.CONFIRMATION_REQUEST,
        allowed_fields=frozenset(
            {"field_label", "current_value", "ambiguous_field", "named_person"}
        ),
        tokenize_names=False,
        tokenize_opt_out_reason=(
            "naming the person is the point of the drafted question; the name "
            "alone may transit, never name plus an identifier"
        ),
        allows_client_content=True,
    ),
    TaskType.DOCUMENT_EXTRACTION: RedactionProfile(
        task_type=TaskType.DOCUMENT_EXTRACTION,
        allowed_fields=frozenset({"document_text", "field_region", "officer_name_as_filed"}),
        tokenize_names=False,
        tokenize_opt_out_reason=(
            "names on a filed document are public-register data once filed"
        ),
        allows_client_content=True,
    ),
    TaskType.ORDINANCE_QA: RedactionProfile(
        task_type=TaskType.ORDINANCE_QA,
        allowed_fields=frozenset(),
        tokenize_names=True,
        tokenize_opt_out_reason=None,
        allows_client_content=False,
    ),
    # Workstream ③'s split (ADR-0019 §3, the Q1.2 amendment ruling D2): one
    # free-text client-content field, tokenized, **no company anchor** — the
    # deliberate second shape beside the one-anchor rule (the module
    # docstring's note extends at the compose leg, T2's task). The caller
    # tokenizes the involved companies' identifying values and keeps the
    # token→company map locally; the provider divides placeholders and prose.
    TaskType.SPLIT: RedactionProfile(
        task_type=TaskType.SPLIT,
        allowed_fields=frozenset({"content"}),
        tokenize_names=True,
        tokenize_opt_out_reason=None,
        allows_client_content=True,
        anchor_required=False,
    ),
}


@dataclass(frozen=True)
class ComposedSnippet:
    """The unit of analysis: one company anchor, one subject, scrubbed.

    ``fields`` is the scrubbed, deterministically ordered field set;
    ``text`` is the wire form; ``tokens`` is the request-scoped scrub table
    (ruling D3) — every marker the scrub pass wrote against the exact value it
    replaced, so a marker the model echoes back is restored locally. Nothing
    outside ``compose_snippet`` builds one, which is what makes the composed
    snippet — not any single field — the thing the digest hashes and the
    scrubber verified.
    """

    task_type: TaskType
    company_anchor: str
    fields: tuple[tuple[str, str], ...]
    text: str
    tokens: Mapping[str, str]


def compose_snippet(
    *,
    task_type: TaskType,
    company_anchor: str = "",
    fields: Mapping[str, str] = {},
    non_client_text: str | None = None,
) -> ComposedSnippet:
    """Compose, scrub, and bound one task-scoped snippet, or refuse.

    The invariants enforced here, in order: the profile exists; a profile
    that admits no client content (ordinance Q&A) refuses any anchor or
    field — its one admitted payload is ``non_client_text``, the
    staff-authored statute question (controller ruling R11): the anchor
    stays empty, the text passes through the same mandatory scrub (defense
    in depth — fail-closed stays) and the same 16 KiB cap, and it is never
    tokenized (there is no client content in it). For the other profiles
    ``non_client_text`` is refused rather than merged. The profiles that
    require an anchor keep its shape: one anchor, one subject — the
    signature takes a single anchor and a single subject's fields, so bulk
    composition has no shape here. The one profile that does not (the
    split, ruling D2) takes exactly one free-text field — the caller's
    already-tokenized client content — refuses an offered anchor
    (``anchor_not_permitted``), and composes an anchor-free text: the
    structural answer to the one-anchor rule for a task whose subject is
    several companies, not one. In both shapes every field must be in the
    profile's allow-list — an excluded field is refused by name; the anchor,
    where one is admitted, is a *declared company
    identifier* — the company row's BRN, never client free text — and is
    scrubbed with the reduced pattern set (every pattern except the
    free-text bare-mobile one, so a bare eight-digit anchor keeps its BRN
    shape for the tokenizer), while each value is scrubbed with the full
    set and a volunteered protected-class match in either fails the whole
    compose closed; the anchor and every field share one scrub table, numbered
    in the order the pass reaches it (ruling D3), so each marker names exactly
    one value and the composed snippet carries the whole request-scoped key;
    and the composed UTF-8 bytes must fit the 16 KiB cap.
    """
    profile = PROFILES[task_type]
    if not profile.allows_client_content:
        if company_anchor or fields:
            raise RedactionRefused("client_content_forbidden", detail=task_type.value)
        if non_client_text is None or not non_client_text.strip():
            raise RedactionRefused("question_required")
        question = scrub_text(non_client_text)
        _check_size(question.text)
        return ComposedSnippet(
            task_type=task_type,
            company_anchor="",
            fields=(),
            text=question.text,
            tokens=question.tokens,
        )
    if non_client_text is not None:
        raise RedactionRefused("non_client_text_forbidden", detail=task_type.value)
    if not profile.anchor_required:
        # The split leg (ruling D2): one tokenized free-text field, no
        # anchor. The caller tokenized the involved companies' identifying
        # values and holds the token->company map locally; what composes
        # here is placeholders and prose, scrubbed with the full set and
        # capped like every snippet — the no-anchor shape relaxes nothing
        # else. Each field carries its own fresh scrub table: the caller's
        # tokenizer table never enters the compose, so the snippet's
        # ``tokens`` is empty and the token->company binding cannot leak
        # through it.
        if company_anchor:
            raise RedactionRefused("anchor_not_permitted", detail=task_type.value)
        if not fields:
            raise RedactionRefused("fields_required")
        for name in fields:
            if name not in profile.allowed_fields:
                raise RedactionRefused("field_not_allowed", detail=name)
        ordered = tuple(
            (name, _scrub(fields[name], _SCRUB_PATTERNS, _ScrubTable()))
            for name in sorted(fields)
        )
        text = "\n".join(f"{name}: {value}" for name, value in ordered)
        _check_size(text)
        return ComposedSnippet(
            task_type=task_type,
            company_anchor="",
            fields=ordered,
            text=text,
            tokens={},
        )
    if not company_anchor:
        raise RedactionRefused("company_anchor_required")
    if not fields:
        raise RedactionRefused("fields_required")
    for name in fields:
        if name not in profile.allowed_fields:
            raise RedactionRefused("field_not_allowed", detail=name)

    table = _ScrubTable()
    scrubbed_anchor = _scrub(company_anchor, _ANCHOR_SCRUB_PATTERNS, table)
    # Sorted here as well as in the wire text below: the marker numbering then
    # follows the order the pass reaches it, and the same caller payload
    # numbers the same way whatever order its mapping arrived in.
    scrubbed = {
        name: _scrub(fields[name], _SCRUB_PATTERNS, table) for name in sorted(fields)
    }
    ordered = tuple(scrubbed.items())
    text = "\n".join(
        [f"company: {scrubbed_anchor}", *(f"{name}: {value}" for name, value in ordered)]
    )
    _check_size(text)
    return ComposedSnippet(
        task_type=task_type,
        company_anchor=scrubbed_anchor,
        fields=ordered,
        text=text,
        tokens=table.tokens,
    )


def _check_size(text: str) -> None:
    """The 16 KiB cap on any composed snippet, in UTF-8 bytes."""
    if len(text.encode("utf-8")) > SNIPPET_MAX_BYTES:
        raise RedactionRefused(
            "snippet_too_large",
            detail=f"exceeds {SNIPPET_MAX_BYTES // 1024} KiB",
        )


# --- The Q1.5 digest ---


@dataclass(frozen=True)
class SnippetDigest:
    """The audit-trail digest pair for one outbound snippet.

    ``summary`` is the machine-generated, whitespace-normalized
    ``DIGEST_MAX_CHARS``-character prefix; ``sha256`` is the hash of the
    exact bytes that left. Both describe the scrubbed and tokenized snippet
    the gateway actually sent — never the raw payload, never the
    restored-PII response.
    """

    summary: str
    sha256: str


def snippet_digest(text: str) -> SnippetDigest:
    """The Q1.5 digest of the final outbound snippet.

    Callers pass the snippet after scrubbing and tokenization; this function
    has no way to verify that, so the contract is stated at both ends —
    here, and at the single application-layer call site. Normalization
    (whitespace collapse) feeds only the summary; the hash binds the exact
    bytes, so two snippets with identical summaries but different bytes hash
    differently.
    """
    normalized = " ".join(text.split())
    return SnippetDigest(
        summary=normalized[:DIGEST_MAX_CHARS],
        sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
    )
