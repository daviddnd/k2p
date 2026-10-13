"""Prior-filing extraction over the sample corpus (scope §2.1, §7;
contract §2.7, §5.1.2).

The corpus in ``research/counter-parties/cr/sample-corpus/`` is real
filing-shaped material with per-sample ground truth (its own ``README.md``
documents the eight samples and how they were assembled): three CR specimen
copies — flattened, text layer only, with CR's own annotation text inside
some boxes — and two synthetic fills of the pinned NAR1 template through
its own widgets, one of them a **composed package** carrying two Schedule 1
sheets before the pages that follow them. The ND2A samples are not read
here: ``SUPPORTED_EXTRACTION_FORMS`` is NAR1-only, and this suite does not
widen it.

The suite is **opt-in** because of its cost, not its material — the PDFs
are committed and the NAR1 files alone are ~93 MB. Run it with::

    AICS_TEST_SAMPLE_CORPUS=1 pytest tests/test_prior_filing_corpus.py

It is real-PDF coverage in front of the generated fixtures in
``tests/pdf_fixtures.py``: the same reads, over files this repository did
not author, including the composed packages the page placement exists for.
"""

import json
import os
from datetime import date
from functools import cache
from pathlib import Path

import pytest

from app.adapters.pdf.extractor import ExtractionResult, extract
from app.domain.prior_filing import (
    AUTO_ACCEPT_THRESHOLD,
    FormCode,
    InputShape,
    ReadSignal,
)

CORPUS = (
    Path(__file__).resolve().parents[2]
    / "research"
    / "counter-parties"
    / "cr"
    / "sample-corpus"
)

pytestmark = pytest.mark.skipif(
    os.environ.get("AICS_TEST_SAMPLE_CORPUS") != "1",
    reason="the sample-corpus suite is opt-in (set AICS_TEST_SAMPLE_CORPUS=1)",
)

SAMPLES = (
    "nar1-no-change-private",
    "nar1-no-change-public",
    "nar1-no-change-guarantee",
    "nar1-director-change",
    "nar1-address-change-overflow",
)

# The corpus records the company type in the form's own words ("guarantee");
# the master-data value is the domain's (``CompanyType``).
_COMPANY_TYPE = {
    "private": "private",
    "public": "public",
    "guarantee": "company_limited_by_guarantee",
}

# The specimen copies carry CR's annotation text inside two of the boxes
# this slice reads — the company-name box and the registered office's
# flat/floor/block box — so the reader merges print into the value, always
# below the auto-accept band (contract §5.1.2). Those two are held to that
# cap rather than to the ground truth; the synthetic files carry values
# only and are checked whole.
_SPECIMEN_MERGED = frozenset({"company.name_en", "company.registered_office.flat"})


@cache
def _ground_truth(name: str) -> dict:
    """One sample's ground truth, as the corpus keeps it."""
    return json.loads((CORPUS / f"{name}.ground-truth.json").read_text(encoding="utf-8"))


@cache
def _proposals(name: str) -> ExtractionResult:
    """One sample's extraction, read once and shared by the tests."""
    return extract((CORPUS / f"{name}.pdf").read_bytes(), form=FormCode.NAR1)


def _expected(ground_truth: dict) -> dict[str, str]:
    """The master-data targets this slice proposes, from the ground truth.

    The ground truth is research data with its own schema (its README notes
    the divergence), so the mapping lives here rather than in the corpus:
    one entry per target this reader reads, and nothing for the fields the
    filing does not state (a company with share capital has no member
    count, a specimen states no business nature).
    """
    company = ground_truth["company"]
    office = ground_truth["registered_office"]
    nature = company.get("business_nature") or {}
    expected = {
        "company.br_number": company["brn"],
        "company.type": _COMPANY_TYPE[company["type"]],
        "company.name_en": company["name_en"],
        "company.name_zh": company["name_zh"],
        "company.email": company["email"],
        "company.contact_phone_hk": company["telephone_hk"],
        "company.registered_office.flat": office["flat_floor_block"],
        "company.registered_office.building": office["building"],
        "company.registered_office.street": office["street"],
        "company.registered_office.district": office.get("district"),
        "company.member_count": company.get("member_count_no_share_capital"),
        "company.business_nature_code": nature.get("code"),
        "company.business_nature_description": nature.get("description"),
    }
    return {target: str(value) for target, value in expected.items() if value is not None}


@pytest.mark.parametrize("name", SAMPLES)
def test_every_sample_is_the_pinned_specifications_own_filing(name: str) -> None:
    """The corpus is spec 2/2025's material, so the guard reads it."""
    ground_truth = _ground_truth(name)
    result = _proposals(name)

    assert ground_truth["form_spec"] == "2/2025 (May 2025)"
    assert result.document_signals == ()


@pytest.mark.parametrize("name", SAMPLES)
def test_the_shape_and_the_filings_own_date_are_read(name: str) -> None:
    ground_truth = _ground_truth(name)
    result = _proposals(name)

    synthetic = ground_truth["source"] == "synthetic"
    assert result.shape is (InputShape.ACROFORM if synthetic else InputShape.TEXT_LAYER)
    assert result.made_up_date == date.fromisoformat(ground_truth["return"]["made_up_date"])


@pytest.mark.parametrize("name", SAMPLES)
def test_the_proposals_match_the_ground_truth(name: str) -> None:
    """Every box the sample's shape reads cleanly holds what the filing says.

    This is the whole point of the suite: the corpus's own record of each
    filing, compared with what the reader proposed from its pages — the
    composed packages included, whose later template pages sit behind the
    sheets the package brings.
    """
    ground_truth = _ground_truth(name)
    expected = _expected(ground_truth)
    if ground_truth["source"] == "specimen-copy":
        expected = {
            target: value for target, value in expected.items() if target not in _SPECIMEN_MERGED
        }
    proposed = {field.target: field.value for field in _proposals(name).fields}

    assert {target: proposed.get(target) for target in expected} == expected
    if ground_truth["source"] == "synthetic":
        # A synthetic sample carries values only, so it is checked whole:
        # nothing outside the ground truth's own boxes may be proposed.
        assert set(proposed) == set(expected)


@pytest.mark.parametrize("name", SAMPLES)
def test_a_proposal_the_corpus_does_not_confirm_stays_in_staff_review(name: str) -> None:
    """Annotation text merged into a value is never silently accepted."""
    expected = _expected(_ground_truth(name))

    for field in _proposals(name).fields:
        if expected.get(field.target) == field.value:
            continue
        assert field.confidence < AUTO_ACCEPT_THRESHOLD, (name, field.target, field.value)


@pytest.mark.parametrize("name", SAMPLES)
def test_the_repeated_header_brn_agrees_across_the_package(name: str) -> None:
    """The header box is printed on every page, the inserted sheets included.

    A reader that took a page's position for its template page would read
    the wrong boxes on a composed package's shifted pages, and the repeated
    header would disagree with itself. Two of these samples are packages
    with their own Schedule 1 sheets in them, so this is where a placement
    regression shows.
    """
    fields = {field.target: field for field in _proposals(name).fields}

    assert ReadSignal.REPEATED_FIELD_DISAGREEMENT not in fields["company.br_number"].signals, (
        f"{name}: the header BRN disagrees across the package's own pages"
    )
