"""Which of the sender's own companies a reply is about (scope §2.5, §7;
data-model §2.7).

Scope §2.5 has one client serving several companies — a director, a holding
company's contact — and scope §7 asks for the resolver to be "rule-based
matching on CR number/company name plus LLM fallback". This module is the
rule; the fallback is the M5b model call, and nothing here makes one.

**The candidates passed in are always the sender's own companies.** The rule
never searches a portfolio or a tenant for what a message might mean: the set
is the companies the sender's registered contact channel already reaches, and
resolving only ever chooses among them. That is the property that keeps a
mis-addressed message from reaching a third party — it can be attributed to a
company the sender already has business with, never to one they do not.

**A tie never resolves, and fuzzy evidence never resolves.** The Phase 0
spike measured the model listing two candidates with equal ``name_exact``
evidence and answering ``resolved`` anyway (reply R16 of the eval run of
2026-08-17; the deterministic post-check was what made the run correct).
Attribution is where a confidentiality breach starts — one client's
correspondence filed against another client's company — so the decisive
routing is this code: exactly one decisive candidate is ``resolved``, more
than one is ``ambiguous`` whatever evidence tiers are involved, and none is
``no_match`` even when a fuzzy or context match exists. ``context_only`` is
vocabulary this rule never produces; it exists for the M5b fallback to fill.

**A Latin name has to be a word of its own; a Chinese one is matched as a
substring.** ``Eurasia Limited`` does not name ``Asia Limited``, so a
Latin-script match may not sit inside a longer *Latin* word — and that test
is ASCII, because Hong Kong mail glues a Latin name straight to Chinese with
no space (``Asia Limited的地址``, ``通知Asia Limited``) and a CJK character
does not continue a Latin word. CJK names are matched as substrings on
purpose: Chinese has no word delimiters, so a registered name is routinely
glued to the verb before it (``請通知太平有限公司的地址``). The residual
that accepts is named here rather than left implicit — a Traditional Chinese
mention embedded inside a longer Chinese name can still match a candidate.
What contains it is the property above: the candidates are only ever the
sender's own registered companies, so a wrong match files a message against
a company the sender already has business with, never against a third party.
The M5b model fallback is the designed answer for the cases this
deterministic rule cannot call.

Pure and framework-free per ADR-0006: stdlib ``re`` and ``difflib`` only.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from difflib import SequenceMatcher
from enum import StrEnum
from uuid import UUID

# §2.7 field scores for a decision this rule does make: an identifier is the
# client stating the number, a name is the client naming the company.
IDENTIFIER_MATCH_SCORE = 1.0
NAME_MATCH_SCORE = 0.9

# A decision this rule does not make carries no score: nothing was resolved,
# so nothing is attributed. It sits at the bottom of the range on purpose,
# below every band the §2.7 rule owns — ``app.domain.confidence`` is that
# rule's single owner and its numbers are deliberately not restated here —
# so an unresolved message routes to clarification and staff review rather
# than to anything automatic.
UNRESOLVED_SCORE = 0.0

# The ``difflib`` ratio at which a registered name and a run of the message
# count as the same name loosely. High enough that the words have to agree
# substantially — this is evidence for a human to read, not a decision.
FUZZY_NAME_RATIO = 0.7

# One run of digits: a number in the text is a whole run, never a fragment of
# a longer one (``1234567`` inside ``91234567`` is the number ``91234567``).
_DIGIT_RUN = re.compile(r"\d+")

# A CJK ideograph, which is how this module tells a name that has to be a word
# of its own (Latin script) from one that is matched as a substring (CJK, a
# script without word delimiters). Ranges: the unified ideographs, extension
# A, and the compatibility ideographs.
_CJK_IDEOGRAPH = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")

# What would make a Latin name part of a longer *Latin* word — an ASCII
# letter, digit or underscore — and nothing else. Deliberately not ``\w``:
# that class is Unicode-aware, so it counts 的 and 知 as word characters, and
# in Hong Kong mail a Latin name is routinely glued to Chinese with no space
# (``Asia Limited的地址``, ``通知Asia Limited``). Reverting this to ``\w``
# would refuse exactly those mentions and quarantine them.
_LATIN_WORD_CHARACTER = r"[A-Za-z0-9_]"

# One run of non-whitespace: a word of the text, with its position, so the
# evidence a candidate carries is the message's own characters rather than a
# normalized copy of them.
_WORD = re.compile(r"\S+")


class MatchEvidence(StrEnum):
    """How a candidate matched (the §2.7 ``signal_log``).

    ``brn_exact``, ``name_exact``, ``name_fuzzy`` and ``context_only`` are the
    Phase 0 spike schema's own set; **``cr_exact`` is this module's addition**
    to it — a CR number is an identifier in its own right, and answering
    ``brn_exact`` for it would lose which number the client actually quoted.
    The M5b fallback's schema has no ``cr_exact`` to answer in, so the
    raw-model-versus-enforced comparison §2.7 asks for
    (``raw_model_resolution``, ``enforced_resolution``) has to map it.

    ``brn_exact`` and ``cr_exact`` are one tier: both are the sender stating
    the company's number. ``name_exact`` is the sender naming the company.
    ``name_fuzzy`` and ``context_only`` are evidence only — they never decide
    anything here — and ``context_only`` is not produced by this module at
    all: it is the M5b model fallback's own tier.
    """

    CR_EXACT = "cr_exact"
    BRN_EXACT = "brn_exact"
    NAME_EXACT = "name_exact"
    NAME_FUZZY = "name_fuzzy"
    CONTEXT_ONLY = "context_only"


class Resolution(StrEnum):
    """The three cases a decision can be in (the spike schema's values).

    ``ResolutionOutcome.resolution`` carries one of these, and it is what a
    caller branches on: the two cases that are not ``resolved`` are two
    different refusals — a sender registered against several companies
    (``ambiguous``) versus a message naming none of them (``no_match``) —
    and the caller (the ingestion cycle's quarantine record) acts on which.
    Reading that off the candidate list instead would be state nobody
    declared.

    The M5b fallback answers in the same three values, so its raw answer and
    this rule's decision stay comparable field for field — §2.7 records both
    for the resolver (``raw_model_resolution``, ``enforced_resolution``).
    """

    RESOLVED = "resolved"
    AMBIGUOUS = "ambiguous"
    NO_MATCH = "no_match"


# The evidence tiers that decide, most decisive first (the BRN is the
# ``(tenant_id, br_number)`` anchor of the master data, and the CR number is
# the other statement of the same kind) — a candidate is reported with the
# strongest tier it matched on.
_IDENTIFIER_EVIDENCE = frozenset({MatchEvidence.BRN_EXACT, MatchEvidence.CR_EXACT})
_DECISIVE_EVIDENCE = _IDENTIFIER_EVIDENCE | {MatchEvidence.NAME_EXACT}

# A candidate whose length differs from the name's by more than this cannot
# reach ``FUZZY_NAME_RATIO`` at all: the ratio divides by the sum of the two
# lengths, so ``2 * shorter / (shorter + longer)`` falls short once the
# longer side is this many times the shorter. Derived, not tuned.
_LENGTH_SIMILARITY = 2 / FUZZY_NAME_RATIO - 1


@dataclass(frozen=True)
class CompanyIdentifiers:
    """One candidate company's identifiers, as the master data holds them.

    Every field is optional because the master data makes it optional: a
    company always has a BRN, has a CR number once the TCSP recorded one,
    and has one or both registered names.
    """

    br_number: str | None = None
    cr_number: str | None = None
    name_en: str | None = None
    name_zh: str | None = None


@dataclass(frozen=True)
class Candidate:
    """One company the message matched, and the text that matched it."""

    company_id: UUID
    evidence: MatchEvidence
    matched_text: str


@dataclass(frozen=True)
class ResolutionOutcome:
    """The resolver's decision, and the evidence it decided on.

    ``resolution`` is the case this outcome is in, and the fields agree with
    it by construction: ``resolved`` always carries a ``company_id`` and its
    ``evidence``, and ``ambiguous`` and ``no_match`` always carry neither.
    The two refusals are *not* interchangeable — a message naming two of the
    sender's companies and one naming none of them are different remedies
    for staff — which is why the case is named rather than inferred from
    whether ``candidates`` happens to be empty.

    ``candidates`` is every company the message matched, ordered by company
    id so repeated runs and their assessments agree; a candidate the text
    says nothing about is simply not in it.
    """

    resolution: Resolution
    company_id: UUID | None
    evidence: MatchEvidence | None
    candidates: tuple[Candidate, ...]
    score: float


def decisive_candidate_ids(outcome: ResolutionOutcome) -> tuple[UUID, ...]:
    """The ids of the outcome's decisive candidates — workstream ③'s E1
    involved set: the sender's own companies the message named decisively
    (identifier evidence or an exact name), the companies a split divides
    the content across. Empty for ``resolved`` and ``no_match`` by
    construction, exactly the ``ambiguous`` case's set.
    """
    return tuple(
        candidate.company_id
        for candidate in outcome.candidates
        if candidate.evidence in _DECISIVE_EVIDENCE
    )


def resolve(
    *, text: str, candidates: Mapping[UUID, CompanyIdentifiers]
) -> ResolutionOutcome:
    """Decide which one of the sender's companies ``text`` is about.

    ``candidates`` is the sender's own companies (see the module docstring);
    ``text`` is the message text (``app.domain.communications.message_text``),
    and an empty one resolves nothing. The decision is the rule, and only the
    rule: one decisive candidate resolves it, a tie does not, and nothing
    fuzzy or contextual ever does.
    """
    matches: list[Candidate] = []
    for company_id in sorted(candidates):
        matched = _match(company_id, candidates[company_id], text)
        if matched is not None:
            matches.append(matched)
    decisive = [item for item in matches if item.evidence in _DECISIVE_EVIDENCE]
    if len(decisive) != 1:
        return ResolutionOutcome(
            resolution=Resolution.AMBIGUOUS if decisive else Resolution.NO_MATCH,
            company_id=None,
            evidence=None,
            candidates=tuple(matches),
            score=UNRESOLVED_SCORE,
        )
    winner = decisive[0]
    return ResolutionOutcome(
        resolution=Resolution.RESOLVED,
        company_id=winner.company_id,
        evidence=winner.evidence,
        candidates=tuple(matches),
        score=(
            IDENTIFIER_MATCH_SCORE
            if winner.evidence in _IDENTIFIER_EVIDENCE
            else NAME_MATCH_SCORE
        ),
    )


def _match(company_id: UUID, identifiers: CompanyIdentifiers, text: str) -> Candidate | None:
    """The strongest evidence one company has in the text, or ``None``.

    Strongest first — the BRN, the CR number, the registered names exactly,
    then a near-miss name — and only that one is reported: a message that
    both names the company and quotes its number is one candidate with one
    piece of evidence, not two.
    """
    for number, evidence in (
        (identifiers.br_number, MatchEvidence.BRN_EXACT),
        (identifiers.cr_number, MatchEvidence.CR_EXACT),
    ):
        span = _number_span(text, number)
        if span is not None:
            return Candidate(company_id=company_id, evidence=evidence, matched_text=span)
    for name in (identifiers.name_en, identifiers.name_zh):
        span = _name_span(text, name)
        if span is not None:
            return Candidate(
                company_id=company_id, evidence=MatchEvidence.NAME_EXACT, matched_text=span
            )
    for name in (identifiers.name_en, identifiers.name_zh):
        if name is None:
            continue
        span = _fuzzy_span(text, name)
        if span is not None:
            return Candidate(
                company_id=company_id, evidence=MatchEvidence.NAME_FUZZY, matched_text=span
            )
    return None


def _number_span(text: str, number: str | None) -> str | None:
    """The digit run in the text that is exactly ``number``, or ``None``.

    The comparison is verbatim against the stored identifier — the BRN as
    the master-data write path normalizes it, the CR number as the TCSP
    recorded it — so a formatted or mistyped stored number matches nothing
    at all rather than the wrong company.
    """
    if not number:
        return None
    for found in _DIGIT_RUN.finditer(text):
        run = found.group(0)
        if run == number:
            return run
    return None


def _name_span(text: str, name: str | None) -> str | None:
    """The text that carries a registered name exactly, or ``None``.

    Case-insensitive and whitespace-flexible — the normalization the rule is
    stated in — and applied to the message as sent, so the evidence is the
    words the client typed rather than a normalized copy of them. All of the
    registered name's words have to be there, in order: part of a name is
    not the name (the spike's "never treat a partial name as name_exact").

    A Latin-script name must additionally be a word of its own, not a
    fragment of a longer *Latin* word: ``Eurasia Limited`` does not name
    ``Asia Limited``. That fragment test is ASCII, so a name glued to Chinese
    still matches — a CJK character does not continue a Latin word, and
    refusing ``Asia Limited的地址`` or ``通知Asia Limited`` would quarantine
    ordinary Hong Kong mail (see ``_LATIN_WORD_CHARACTER``). CJK names are
    matched as substrings on purpose — Chinese has no word delimiters, and a
    guard there would reject the ordinary ``請通知太平有限公司的地址``; the
    module docstring names the residual that accepts.
    """
    if name is None:
        return None
    words = name.split()
    if not words:
        return None
    pattern = r"\s+".join(re.escape(word) for word in words)
    if not _CJK_IDEOGRAPH.search(name):
        pattern = rf"(?<!{_LATIN_WORD_CHARACTER}){pattern}(?!{_LATIN_WORD_CHARACTER})"
    found = re.search(pattern, text, re.IGNORECASE)
    return None if found is None else found.group(0)


def _fuzzy_span(text: str, name: str) -> str | None:
    """A near-miss of a registered name in the text, or ``None``.

    Every run of as many words as the registered name has is compared with
    it — the length-similar candidate a ratio means anything against — using
    ``difflib``'s own ratio, so a typo, a shortened word, or a wrong word in
    an otherwise right name still shows up as evidence. The best one wins.
    This is for a human and for the M5b fallback to look at, never a
    decision: see the module docstring.
    """
    name_words = name.split()
    if not name_words:
        return None
    wanted = " ".join(name_words).casefold()
    shortest = len(wanted) / _LENGTH_SIMILARITY
    longest = len(wanted) * _LENGTH_SIMILARITY
    words = list(_WORD.finditer(text))
    size = len(name_words)
    best: tuple[float, int] | None = None
    for index in range(len(words) - size + 1):
        window = text[words[index].start() : words[index + size - 1].end()]
        folded = window.casefold()
        if not shortest <= len(folded) <= longest:
            continue
        ratio = SequenceMatcher(None, wanted, folded, autojunk=False).ratio()
        if ratio >= FUZZY_NAME_RATIO and (best is None or ratio > best[0]):
            best = (ratio, index)
    if best is None:
        return None
    index = best[1]
    return text[words[index].start() : words[index + size - 1].end()]
