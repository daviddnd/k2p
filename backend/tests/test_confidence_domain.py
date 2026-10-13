"""Domain tests for the §2.7 confidence-band rule (data-model §2.7).

Pure and database-free: the band boundaries at their exact threshold values,
the minimum aggregation behind a run's overall score, and the routing an
assessment carries out of it. The rule has its own module because two
consumers score against it — prior-filing extraction, and the reply
resolver's target-company score — and the extraction callers keep naming it
through ``app.domain.prior_filing``, which re-exports it unchanged.
"""

from app.domain import prior_filing
from app.domain.confidence import (
    AUTO_ACCEPT_THRESHOLD,
    STAFF_REVIEW_THRESHOLD,
    ConfidenceBand,
    band_for,
    overall_score,
    routing_for,
)


def test_band_thresholds_are_the_contract_values() -> None:
    assert (AUTO_ACCEPT_THRESHOLD, STAFF_REVIEW_THRESHOLD) == (0.85, 0.50)


def test_band_boundaries_are_inclusive_from_below() -> None:
    assert band_for(1.0) is ConfidenceBand.AUTO_ACCEPT
    assert band_for(0.85) is ConfidenceBand.AUTO_ACCEPT
    assert band_for(0.8499) is ConfidenceBand.STAFF_REVIEW
    assert band_for(0.50) is ConfidenceBand.STAFF_REVIEW
    assert band_for(0.4999) is ConfidenceBand.CLIENT_CLARIFICATION
    assert band_for(0.0) is ConfidenceBand.CLIENT_CLARIFICATION


def test_overall_score_is_the_minimum_of_the_field_scores() -> None:
    assert overall_score({"a": 0.99, "b": 0.7}) == 0.7
    assert overall_score({"a": 0.55}) == 0.55
    assert overall_score({"a": 0.2, "b": 0.95}) == 0.2


def test_overall_score_of_nothing_is_none() -> None:
    assert overall_score({}) is None


def test_routing_for_maps_each_band_to_its_outcome() -> None:
    assert routing_for({"a": 0.99, "b": 0.95}) is ConfidenceBand.AUTO_ACCEPT
    assert routing_for({"a": 0.99, "b": 0.7}) is ConfidenceBand.STAFF_REVIEW
    assert routing_for({"a": 0.4}) is ConfidenceBand.CLIENT_CLARIFICATION
    assert routing_for({}) is None


def test_prior_filing_reexports_the_same_rule() -> None:
    assert prior_filing.AUTO_ACCEPT_THRESHOLD is AUTO_ACCEPT_THRESHOLD
    assert prior_filing.STAFF_REVIEW_THRESHOLD is STAFF_REVIEW_THRESHOLD
    assert prior_filing.ConfidenceBand is ConfidenceBand
    assert prior_filing.band_for is band_for
    assert prior_filing.overall_score is overall_score
    assert prior_filing.routing_for is routing_for
