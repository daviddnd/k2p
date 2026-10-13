"""Hermetic tests for the reply parser's gateway call (slice M5b; the Task 7
review fix).

``parse_reply`` holds the parser's one call into the M6 gateway, and the
strict-restore wiring it passes there — ``allowed_literals=PARSE_LITERALS``
(Task 7, review focus 6) — had no hermetic cover. Every suite that reaches
``parse_reply`` fakes ``ai_gateway.infer`` (the DB scenarios) or fakes the
whole cycle (``test_api_mail_parse.py``), so deleting the keyword left every
test green while the gateway would silently start refusing a reply's
legitimate un-tokenized evidence spans. These tests drive the real call path
with its collaborators replaced and a gateway seam that records its kwargs, so
the wiring is pinned where it is made — and its refusal's landing is pinned at
the parser's end.

No database and no provider: the session, the package gate, the master-data
reads, the storage and the gateway are all fakes.
"""

from collections.abc import Awaitable, Callable
from datetime import date
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.persistence.prior_filing import ExtractionAssessment
from app.adapters.persistence.reply_parsing import ReplyParse
from app.application import reply_parsing
from app.domain.communications import Direction
from app.domain.documents import ScanStatus
from app.domain.redaction import RedactionRefused
from app.domain.reply_parsing import PARSE_LITERALS
from tests.fake_scanning import FakeScanner

TENANT_ID = uuid4()
COMPANY_ID = uuid4()
COMMUNICATION_ID = uuid4()
BRN = "22334455"

RAW_MESSAGE = (
    b"From: director@example.hk\r\n"
    b"To: secretary@mailbox.example.hk\r\n"
    b"Subject: Re: annual return confirmation\r\n"
    b"MIME-Version: 1.0\r\n"
    b"Content-Type: text/plain; charset=utf-8\r\n"
    b"\r\n"
    b"Everything is unchanged this year, thanks.\r\n"
)

REPLY_BODY = "Everything is unchanged this year, thanks."

# The known master data the faked reads return, so the recorded gateway call's
# lexicon can be asserted non-empty and attributed: an officer whose name would
# transit raw if the parser stopped threading its lexicon.
KNOWN_OFFICER = "Chan Tai Man"
KNOWN_COMPANY = "Example Limited"

InferSeam = Callable[..., Awaitable[object]]


class _FakeSession:
    """The parser's session seam: the advisory lock, then its three reads.

    ``scalar`` answers in the order ``parse_reply`` asks — the communication,
    the already-parsed check (absent), the raw document — and nothing writes:
    ``add`` records the landing's rows so a test can read what the parse
    decided, and ``flush`` is the no-op that stands in for the database.
    """

    def __init__(self) -> None:
        self._answers: list[object] = [_communication(), None, _document()]
        self.added: list[object] = []

    async def execute(self, statement: object) -> None:
        return None

    async def scalar(self, statement: object) -> object:
        return self._answers.pop(0)

    def add(self, row: object) -> None:
        self.added.append(row)

    async def flush(self) -> None:
        return None


class _FakeStore:
    """The storage seam: one scanned-clean inbound message."""

    async def put(self, key: str, data: bytes, content_type: str) -> None: ...

    async def get_bytes(self, key: str) -> bytes:
        return RAW_MESSAGE

    async def presign_get(self, key: str, *, expires_in: int) -> str: ...


def _communication() -> SimpleNamespace:
    return SimpleNamespace(
        id=COMMUNICATION_ID,
        tenant_id=TENANT_ID,
        company_id=COMPANY_ID,
        direction=Direction.INBOUND.value,
        raw_document_id=uuid4(),
        confirmation_request_id=None,
    )


def _document() -> SimpleNamespace:
    return SimpleNamespace(scan_status=ScanStatus.CLEAN.value, storage_key="raw/1")


def _install(monkeypatch: pytest.MonkeyPatch, infer: InferSeam) -> None:
    """Replace every collaborator ``parse_reply`` needs before its gateway call."""

    async def fake_gate(session: object, **kwargs: object) -> object:
        return SimpleNamespace(allowed=True, reason=None)

    async def fake_master(session: object, **kwargs: object) -> object:
        return SimpleNamespace(
            name=SimpleNamespace(name_en=KNOWN_COMPANY, name_zh=None),
            company=SimpleNamespace(br_number=BRN),
        )

    async def fake_appointments(session: object, **kwargs: object) -> list[object]:
        return [
            SimpleNamespace(
                person=SimpleNamespace(
                    name_en=KNOWN_OFFICER,
                    surname_en=None,
                    other_names_en=None,
                    name_zh=None,
                )
            )
        ]

    monkeypatch.setattr(reply_parsing.service_packages, "check_automation", fake_gate)
    monkeypatch.setattr(reply_parsing.company_master, "get_company_master", fake_master)
    monkeypatch.setattr(reply_parsing.officers, "list_appointments", fake_appointments)
    monkeypatch.setattr(reply_parsing.ai_gateway, "infer", infer)


async def _drive_parse_reply(
    session: _FakeSession | None = None,
) -> reply_parsing.ParseReport:
    """Run the real ``parse_reply`` over the faked seams."""
    return await reply_parsing.parse_reply(
        cast(AsyncSession, _FakeSession() if session is None else session),
        store=_FakeStore(),
        scanner=FakeScanner(),
        tenant_id=TENANT_ID,
        communication_id=COMMUNICATION_ID,
        as_of=date(2026, 10, 6),
    )


async def test_the_parser_hands_the_gateway_the_closed_enum_literals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two wirings, pinned where they are made. The Task 7 one: the parser
    declares its grammar's closed enum vocabulary (``PARSE_LITERALS``) as the
    strict restore's ``allowed_literals`` — drop the keyword and the gateway
    sees an empty vocabulary, so every enum word is checked as if the model had
    written it itself. The T3 one: the parser hands the gateway its typed
    master-data lexicon (``tokenize_values``) — drop the keyword and the
    gateway tokenizes nothing, restoring the T1 leak of a known officer's name
    transiting raw inside ``reply_text``. Each is exactly the kind of silent
    scope change this test exists to stop."""
    calls: list[dict[str, Any]] = []

    async def record_infer(session: object, **kwargs: Any) -> object:
        calls.append(kwargs)
        raise _GatewayReached

    _install(monkeypatch, record_infer)

    with pytest.raises(_GatewayReached):
        await _drive_parse_reply()

    (call,) = calls
    assert call["allowed_literals"] == PARSE_LITERALS
    # The typed master-data lexicon rides the same call, non-empty and
    # attributed: ``reply_parsing`` passes ``context.tokenize_values``, so a
    # known officer's name written in the free-text reply field is a
    # replacement source at the gateway. Without it the lexicon is empty and
    # the name would transit raw — the T1 leak this assertion stops regressing.
    lexicon = call["tokenize_values"]
    assert set(lexicon) >= {"PERSON", "COMPANY", "BRN"}
    assert lexicon["PERSON"] and lexicon["COMPANY"] and lexicon["BRN"]
    assert KNOWN_OFFICER in lexicon["PERSON"]
    assert KNOWN_COMPANY in lexicon["COMPANY"]
    assert lexicon["BRN"] == [BRN]
    # The recorded call is the parser's own, not a mock of it: the real
    # profile-admitted content (the stored reply body) travelled with it.
    assert call["task_type"].value == "reply_parsing"
    assert REPLY_BODY in call["content"]["reply_text"]


async def test_the_parser_refuses_a_value_the_answer_invented(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The refusal's landing, at the parser's end: the gateway's strict-restore
    ``invented_value`` refusal is one of the compose-shaped refusals — the reply
    waits for staff with a bounded ``refused:<reason>`` outcome, and no parse
    row, event or letter is produced from the answer."""
    calls: list[dict[str, Any]] = []

    async def refusing_infer(session: object, **kwargs: Any) -> object:
        calls.append(kwargs)
        raise RedactionRefused("invented_value")

    _install(monkeypatch, refusing_infer)

    report = await _drive_parse_reply()

    assert len(calls) == 1
    assert calls[0]["allowed_literals"] == PARSE_LITERALS
    assert report.outcome == "refused:invented_value"
    assert report.parse_id is None


class _GatewayReached(Exception):
    """The recording seam's sentinel: the parser reached its gateway call."""


# --- R-D's landing: the register read reaches the band decision ---------------

# A payload-complete appointment proposal naming one person. Every span is the
# whole reply (verbatim by construction), the date is a real calendar day and
# the officer block carries the split name, so the no-guessing rule is silent,
# every field scores 1.0 and the only thing that can move the run's band is the
# register rule under test.
UNREGISTERED = "Wong New Director"


def _appointment_parse(name: str) -> dict[str, Any]:
    return {
        "status": "changes_reported",
        "language": "en",
        "confirmations": [],
        "change_events": [
            {
                "type": "director_appointment",
                "party": {
                    "name_as_written": name,
                    "name_en": name,
                    "name_zh": None,
                    "partial_id": None,
                    "evidence_span": REPLY_BODY,
                    "uncertain": False,
                },
                "effective_date": "2026-05-01",
                "effective_date_text": "1 May 2026",
                "informed_date": None,
                "new_values": None,
                "evidence_span": REPLY_BODY,
                "uncertain": False,
                "notes": None,
            }
        ],
        "multi_company_signals": {"company_mentions": []},
    }


def _install_landing(
    monkeypatch: pytest.MonkeyPatch, parse_result: dict[str, Any]
) -> None:
    """``_install`` plus every write ``_land`` makes, faked.

    The event write path, the audit entry and the reconfirmer's letter are the
    DB scenario suite's subject; here they are seams, so what this test reads
    is the decision the parser reached — the report, the parse row and the
    assessment it built — and not the database's copy of it.
    """

    async def answering_infer(session: object, **kwargs: Any) -> object:
        return SimpleNamespace(
            result=parse_result,
            provider="stub-provider",
            model_id="stub-model",
            prompt_name="reply_parser",
            prompt_version=1,
        )

    async def fake_event(session: object, **kwargs: Any) -> object:
        return SimpleNamespace(id=uuid4())

    async def fake_audit(session: object, **kwargs: Any) -> None: ...

    async def fake_letter(*args: object, **kwargs: Any) -> None: ...

    _install(monkeypatch, answering_infer)
    monkeypatch.setattr(reply_parsing.change_events, "create", fake_event)
    monkeypatch.setattr(reply_parsing.audit, "record_event", fake_audit)
    monkeypatch.setattr(reply_parsing.reconfirmation, "enqueue_letter", fake_letter)


def _landed(session: _FakeSession) -> tuple[ReplyParse, ExtractionAssessment]:
    """The two rows one valid parse lands on the caller's session."""
    return (
        next(row for row in session.added if isinstance(row, ReplyParse)),
        next(row for row in session.added if isinstance(row, ExtractionAssessment)),
    )


async def test_a_person_off_the_register_holds_the_run_at_staff_review(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ruling R-D, landed: the fields score a flawless 1.0 and the proposal is
    complete, so the band rule's own answer is ``auto_accept`` — but the person
    it names is absent from ``officers.list_appointments`` (the company's only
    officer is the faked ``Chan Tai Man``), so the run is capped at
    ``staff_review`` on the report, on the parse row and on the assessment, and
    the diagnostic log names the person the register missed — the reason staff
    read beside the routing."""
    _install_landing(monkeypatch, _appointment_parse(UNREGISTERED))
    session = _FakeSession()

    report = await _drive_parse_reply(session)

    assert report.outcome == "parsed"
    assert report.routing == "staff_review"
    parse_row, assessment = _landed(session)
    assert parse_row.routing == "staff_review"
    assert assessment.routing == "staff_review"
    assert assessment.field_scores == {"change_events.0": 1.0}
    assert assessment.signal_log["unregistered_person_names"] == [UNREGISTERED]
    # The proposal itself still lands: the cap decides who looks, not whether
    # the client's change is recorded for review.
    assert len(report.event_ids) == 1


async def test_a_person_on_the_register_still_auto_accepts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The control, through the same real call path: identical scores,
    identical proposal, a person who *is* on the register (the faked
    appointment) — so nothing bends and no diagnostic reason is recorded. This
    is what pins the wiring: ``_land`` passes the names ``_content`` read from
    the company's appointments, not an empty register."""
    _install_landing(monkeypatch, _appointment_parse(KNOWN_OFFICER))
    session = _FakeSession()

    report = await _drive_parse_reply(session)

    assert report.routing == "auto_accept"
    parse_row, assessment = _landed(session)
    assert parse_row.routing == "auto_accept"
    assert assessment.routing == "auto_accept"
    assert "unregistered_person_names" not in assessment.signal_log
