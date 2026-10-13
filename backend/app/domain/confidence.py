"""The §2.7 confidence bands and the routing they imply (data-model §2.7).

Single owner of contract §2.7's confidence bands and routing:

- **The bands** — an assessment at or above ``AUTO_ACCEPT_THRESHOLD``
  (0.85) is ``auto_accept``, at or above ``STAFF_REVIEW_THRESHOLD`` (0.50)
  ``staff_review``, below that ``client_clarification``. ``band_for`` is
  the one rule that maps a score to a band.
- **The aggregation behind them** — a run's ``overall_score`` is the
  **minimum** of its field scores, so one weak field routes the whole run
  (a filing is only as trustworthy as its least certain box).
- **The routing** — ``routing_for`` puts the two together: the band a
  run's field scores fall into, or ``None`` when nothing was assessed.

The vocabulary is §2.7's and is shared by every subject an assessment can
be about: prior-filing extraction writes it today, and the reply resolver
scores its target company against the same bands. That second consumer is
why the rule lives on its own rather than inside ``app.domain.prior_filing``
— a reply-parsing module importing *prior filing* for its bands would be a
wrong-named dependency. ``app.domain.prior_filing`` imports and re-exports
everything here, so extraction callers keep naming it there unchanged.

Pure and framework-free per ADR-0006: the import-linter contract forbids
framework and I/O imports in this layer.
"""

from collections.abc import Mapping
from enum import StrEnum

# §2.7 confidence bands (data-model §2.7; the review queue's routing rule).
AUTO_ACCEPT_THRESHOLD = 0.85
STAFF_REVIEW_THRESHOLD = 0.50


class ConfidenceBand(StrEnum):
    """§2.7 ``ExtractionAssessment.routing`` vocabulary.

    Also the band a single score falls into; ``band_for`` is the one rule
    that maps a score to either.
    """

    AUTO_ACCEPT = "auto_accept"
    STAFF_REVIEW = "staff_review"
    CLIENT_CLARIFICATION = "client_clarification"


def band_for(score: float) -> ConfidenceBand:
    """The §2.7 band a confidence score falls into."""
    if score >= AUTO_ACCEPT_THRESHOLD:
        return ConfidenceBand.AUTO_ACCEPT
    if score >= STAFF_REVIEW_THRESHOLD:
        return ConfidenceBand.STAFF_REVIEW
    return ConfidenceBand.CLIENT_CLARIFICATION


def overall_score(field_scores: Mapping[str, float]) -> float | None:
    """The run's overall score: the minimum field score (``None`` if none).

    A filing is only as trustworthy as its least certain box, so one
    doubtful field keeps the whole assessment out of the auto-accept band.
    """
    if not field_scores:
        return None
    return min(field_scores.values())


def routing_for(field_scores: Mapping[str, float]) -> ConfidenceBand | None:
    """The §2.7 routing for a run, or ``None`` when nothing was assessed."""
    score = overall_score(field_scores)
    return None if score is None else band_for(score)
