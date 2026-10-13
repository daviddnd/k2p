"""The multi-company resolver's seam, and its §2.7 record (scope §2.5, §7;
data-model §2.7; contract §5.1.7).

A client may serve several companies, so "which of this sender's companies is
this message about?" is a decision the ingestion cycle has to make before it
can file anything. The rule itself is deterministic and pure
(``app.domain.reply_resolver``); this module is the seam around it: it loads
the candidates' identifiers and registered names from the master data, hands
the rule the message's text, and records what the rule decided.

- ``resolve_message`` is the decision, and it writes nothing. The candidates
  are the sender's own companies — the companies their registered contact
  channel already reaches — and never a portfolio or a tenant: that property
  is what keeps a mis-addressed message from reaching a company the sender
  has no business with.
- ``record_assessment`` is the §2.7 record of that decision, written once per
  **decision record**: the ``Communication`` an attributed message became
  (``subject_type=communication``), or the §2.5 quarantine record
  (``app.adapters.persistence.quarantine``) an unattributable one left
  instead (``subject_type=quarantine``). The two calls sit in the cycle's
  order because the subject id cannot exist before the record does — the
  assessment is what answers "why this company?", and there is nothing to
  attach it to until the message has landed somewhere. A re-run that finds
  the same record appends neither another assessment nor another audit entry.

The text is the subject and the body, together: scope §2.5 has the product
identify the company "from the email subject/body", and a reply's subject is
where the confirmation request put the company (\"every AI message must
clearly identify the relevant company by name and CR number\"). Each half is
bounded — the body by ``app.domain.communications.message_text``, the subject
by the same cap here — so a hostile message cannot make the decision cost
more than the caps say.

Nothing here calls a model: the LLM fallback is the M5b slice, and it will
arrive through the AI gateway. The assessment's ``signal_log`` carries the
evidence a human reads — each matched candidate's strongest tier and the
verbatim text that matched it (a candidate the text says nothing about does
not appear) — and that text is a diagnostic in the customer's own database:
it never reaches an audit entry, a log line, or a response. What a report
carries instead is the projection ``band_for`` and its caller build — the
case, the tier, the score, the routing, and the candidate ids — and a
fragment of the mail is nowhere in it.

Services flush but never commit — the API session dependency commits at the
request edge, and the ingestion cycle's own transaction commits with it.
"""

from collections.abc import Sequence
from datetime import date
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.persistence.prior_filing import ExtractionAssessment
from app.application import company_master
from app.domain.communications import MAX_MESSAGE_TEXT_CHARS
from app.domain.confidence import overall_score, routing_for
from app.domain.prior_filing import AssessmentComponent, AssessmentSubject
from app.domain.reply_resolver import (
    CompanyIdentifiers,
    ResolutionOutcome,
    resolve,
)

# The §2.7 ``model`` of a decision no model made: the rule and its revision,
# as the deterministic extractor records ``pdf-geometry/v1``. The M5b
# fallback will carry a pinned model id and prompt version here.
RESOLVER_MODEL = "reply-resolver/v1"

# The one "field" a resolver run scores: the decision it makes about one
# communication. §2.7's ``field_scores`` maps field → score and its bands
# route on that score, so the decision is named rather than left implicit.
COMPANY_FIELD = "communication.company"


async def resolve_message(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    company_ids: Sequence[UUID],
    subject: str | None,
    text: str,
    as_of: date,
) -> ResolutionOutcome:
    """Decide which one of the sender's companies one message is about.

    ``company_ids`` are the companies the sender's address is registered
    against (``contacts.companies_for_channel_identifier``); ``subject`` and
    ``text`` are the message's own words.

    ``as_of`` is **the day the decision is made** — the cycle's own date, not
    the message's receipt date: attribution reads the master data in force
    now. That is what makes the staff remedy work. A message that arrived
    before a company was onboarded, or before a name or number was recorded,
    becomes attributable the moment staff record it and the quarantine is
    retried (``quarantined_messages``); resolving against the receipt date
    instead would leave exactly those messages unresolvable for good.

    Returns the rule's outcome unchanged; nothing is written (see
    ``record_assessment``). The identifiers are read through the tenant-scoped
    master-data seam, so a candidate outside this tenant is unknown rather
    than readable.
    """
    identifiers = await _identifiers(
        session, tenant_id=tenant_id, company_ids=company_ids, as_of=as_of
    )
    return resolve(text=resolution_text(subject=subject, text=text), candidates=identifiers)


def band_for(outcome: ResolutionOutcome) -> str | None:
    """The §2.7 routing band this decision's score falls into.

    The one place the band is derived for a decision: ``record_assessment``
    stores it on the assessment row through this call, and the report a
    message's outcome carries reads it through the same one — so the record
    and the report cannot disagree about where the rule's answer routes. It
    is well defined even where no assessment could be written (a decision the
    §5.1 gate then denied leaves no record to hang one on): the band is a
    property of the score, not of the row.
    """
    routing = routing_for({COMPANY_FIELD: outcome.score})
    return None if routing is None else routing.value


def resolution_text(*, subject: str | None, text: str) -> str:
    """The text the rule reads: the subject and the body, as one string.

    Scope §2.5 puts the company in the subject *or* the body of a reply, and
    the product's own confirmation requests name the company and its CR
    number in the subject, so both are read. The subject is capped here by
    the same limit the body reader applies — it never passed a size check of
    its own (``app.domain.communications.MAX_MESSAGE_TEXT_CHARS``), and a
    huge one must not decide how much the rule scans.
    """
    parts: list[str] = []
    if subject:
        parts.append(subject[:MAX_MESSAGE_TEXT_CHARS])
    if text:
        parts.append(text)
    return "\n".join(parts)


async def record_assessment(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    subject_type: AssessmentSubject,
    subject_id: UUID,
    outcome: ResolutionOutcome,
) -> ExtractionAssessment:
    """Record one resolver decision as the §2.7 assessment of one message.

    The subject is the message's own record, and ``subject_type`` names the
    table that id lives in — ``communication`` for a message that was
    attributed (the ``Communication`` row it became) and ``quarantine`` for
    one that could not be (the §2.5 record it left instead). The caller names
    it because only the caller knows which record exists; the pair is what
    lets a reader resolve the assessment to a real row, which is the whole
    point of the vocabulary.

    ``field_scores`` holds the one decision's score, ``overall_score`` and
    ``routing`` come from the §2.7 band rule (``app.domain.confidence``) —
    the resolver's own score being the only field means the bands route
    exactly it: a decided message is ``auto_accept``, an undecided one
    ``client_clarification``. The ``signal_log`` is an object in the shape
    the extraction slice fixed: ``candidates`` maps company id →
    the evidence tiers that matched, and ``matched_text`` carries the
    verbatim text behind each, keyed identically — diagnostics, in the
    customer's own database, and the one place a fragment of the mail is kept.
    """
    field_scores = {COMPANY_FIELD: outcome.score}
    score = overall_score(field_scores)
    assessment = ExtractionAssessment(
        tenant_id=tenant_id,
        subject_type=subject_type.value,
        subject_id=subject_id,
        component=AssessmentComponent.MULTI_COMPANY_RESOLVER.value,
        model=RESOLVER_MODEL,
        field_scores=field_scores,
        signal_log=_signal_log(outcome),
        overall_score=None if score is None else _score(score),
        routing=band_for(outcome),
    )
    session.add(assessment)
    await session.flush()
    return assessment


async def _identifiers(
    session: AsyncSession, *, tenant_id: UUID, company_ids: Sequence[UUID], as_of: date
) -> dict[UUID, CompanyIdentifiers]:
    """Each candidate's identifiers and current registered names.

    The registered names are the effective-dated name record in force on
    ``as_of`` — the decision's own day (see ``resolve_message``), so what is
    recorded later is what a retry resolves against. A company with no name
    record in force has no name to match on, and its numbers still are.
    """
    identifiers: dict[UUID, CompanyIdentifiers] = {}
    for company_id in company_ids:
        master = await company_master.get_company_master(
            session, tenant_id=tenant_id, company_id=company_id, as_of=as_of
        )
        identifiers[company_id] = CompanyIdentifiers(
            br_number=master.company.br_number,
            cr_number=master.company.cr_number,
            name_en=None if master.name is None else master.name.name_en,
            name_zh=None if master.name is None else master.name.name_zh,
        )
    return identifiers


def _signal_log(outcome: ResolutionOutcome) -> dict[str, Any]:
    """The §2.7 signal log: which case the rule reached, and its evidence.

    ``candidates`` keeps the extraction slice's ``{key: [signal names]}``
    leaves — one list per company, holding the tier the candidate matched on.
    """
    return {
        "resolution": outcome.resolution.value,
        "candidates": {
            str(candidate.company_id): [candidate.evidence.value]
            for candidate in outcome.candidates
        },
        "matched_text": {
            str(candidate.company_id): candidate.matched_text
            for candidate in outcome.candidates
        },
    }


def _score(value: float) -> Decimal:
    """A confidence score within the assessment column's ``Numeric(4, 3)``."""
    return Decimal(str(value)).quantize(Decimal("0.001"))
