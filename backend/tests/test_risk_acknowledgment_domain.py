"""Risk-acknowledgment domain tests (the 2026-10-04 founder rulings).

Pure rules, no I/O: the statement source's invariants (versioned, non-empty,
the required plain claims present) and the version-compare rule — an
acceptance counts only while it carries the statement version the code
currently serves, so a wording change re-opens the acknowledgment by
construction. The statement copy is founder-gated (the M3 precedent): these
tests pin the **v3** claims — the 2026-10-10 ruling (ADR-0019 §2, option 甲)
replaces the per-company opt-in section with the TCSP-layer basis while
keeping every other disclosure element — and a copy change bumps the version
and updates them together.
"""

from app.domain.risk_acknowledgment import (
    STATEMENT_TEXT,
    STATEMENT_VERSION,
    acceptance_counts,
)

# Assertions read the statement as one flattened line, so a phrase that wraps
# across two lines is still found (the copy is line-wrapped for display).
FLAT = " ".join(STATEMENT_TEXT.split())


def test_the_statement_version_is_v3() -> None:
    assert STATEMENT_VERSION == "v3"


def test_the_statement_is_substantive() -> None:
    """Non-empty is the floor; a disclosure this short would disclose nothing."""
    assert len(STATEMENT_TEXT) > 400


def test_the_statement_is_bilingual() -> None:
    """The 2026-10-07 ruling settled the bilingual question: the served copy
    carries the Chinese disclosure alongside the English one."""
    assert "AI 功能" in STATEMENT_TEXT
    assert "個人資料保護法" in STATEMENT_TEXT


def test_the_statement_discloses_what_is_sent_and_kept() -> None:
    """The plain-language core: only the message content is sent, with client
    values replaced first by placeholders, and the real details stay in the
    deployment (the minimization the old copy called "minimized, redacted")."""
    assert "only the content of the message is sent" in FLAT
    assert "plain placeholders" in FLAT
    assert "real details stay in this deployment" in FLAT


def test_the_statement_discloses_the_fail_closed_rule() -> None:
    """The 2026-10-07 approved copy's addition: when the system cannot certify
    a replacement, nothing is sent — the message goes to staff."""
    assert "cannot confirm that a detail has been replaced" in FLAT
    assert "the message is not sent" in FLAT


def test_the_statement_discloses_no_control_plane_retention() -> None:
    """The founder's first plain claim: the vendor control plane retains no
    client information — its metering is metadata-only."""
    assert "uses metadata only" in FLAT
    assert "never sent to the vendor control plane" in FLAT


def test_the_statement_discloses_the_snippet_flow_under_the_providers_terms() -> None:
    """The founder's second plain claim: the placeholders reach the LLM
    platform — SiliconFlow, the Singapore contracting entity under the PDPA —
    for further processing, under the provider's standard terms."""
    assert "SiliconFlow" in STATEMENT_TEXT
    assert "Singapore" in STATEMENT_TEXT
    assert "PDPA" in STATEMENT_TEXT
    assert "standard terms" in STATEMENT_TEXT


def test_the_statement_names_the_tcsp_layer_basis_and_the_objection_valve() -> None:
    """ADR-0019 §2 (option 甲): the per-company opt-in retires with the Q1.8
    gate — the disclosure says there is no per-company opt-in to record, and
    that a company's objection still disables the features for it, nothing
    sent."""
    assert "no per-company opt-in to record" in FLAT
    assert "nothing for that company is sent" in FLAT


def test_an_acceptance_of_the_served_version_counts() -> None:
    assert acceptance_counts(STATEMENT_VERSION)


def test_an_acceptance_of_an_older_version_stops_counting() -> None:
    """Ruling R4: the version-compare rule — a wording change bumps the
    version and every prior acceptance stops counting."""
    assert not acceptance_counts("v1")
