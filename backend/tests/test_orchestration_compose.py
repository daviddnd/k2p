"""The composed sweep's rules (the orchestration command's rulebook).

``schedule_orchestration`` composes the five existing cycles — ingest,
parse, discovery, reminders, send — on the command worker's session, in that
pinned order, containing each leg's failure so one broken cycle cannot take
the command (or the legs after it) down. These tests pin the composition
with the legs stubbed: no database, no mailbox, no blob store — each cycle's
own suite owns its behaviour; only the composition is under test here.
"""

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.application import commands, mail, mail_ingest
from app.application.mail_send import SendDisposition

NOW = datetime(2026, 10, 2, 9, 30, tzinfo=UTC)
TENANT_ID = uuid4()


class _Legs:
    """The five cycle functions, stubbed: recording call order and the session.

    Defaults return distinct counts so the telemetry-key mapping test can
    tell the legs apart; each test then overrides the behaviour it cares
    about. ``session``/``store``/``scanner`` are passed through untouched —
    the composition's only job is to hand every leg the same ones.
    """

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.calls: list[tuple[str, object]] = []
        self.ingested = 3
        self.parsed = 2
        self.generated = 6
        self.refused = 1
        self.skipped = 2
        self.enqueued = 4
        self.sent = 5
        self.ingest_raises: Exception | None = None
        self.discovery_raises: Exception | None = None
        self.discovery_config_error: str | None = None
        self.reminders_raises: Exception | None = None
        self.send_raises: Exception | None = None
        self.sweep_config_error: str | None = None
        monkeypatch.setattr(commands.mail_ingest, "run_cycle", self._ingest)
        monkeypatch.setattr(commands.reply_parsing, "run_cycle", self._parse)
        monkeypatch.setattr(commands.confirmation_discovery, "sweep", self._discovery)
        monkeypatch.setattr(commands.reminders, "sweep", self._reminders)
        monkeypatch.setattr(commands.mail_send, "run_cycle", self._send)

    async def _ingest(self, session: object, **kwargs: object) -> object:
        self.calls.append(("ingest", session))
        if self.ingest_raises is not None:
            raise self.ingest_raises
        return SimpleNamespace(ingested=self.ingested)

    async def _parse(self, session: object, **kwargs: object) -> object:
        self.calls.append(("parse", session))
        return SimpleNamespace(parsed=self.parsed)

    async def _discovery(self, session: object, *args: object, **kwargs: object) -> object:
        self.calls.append(("discovery", session))
        if self.discovery_raises is not None:
            raise self.discovery_raises
        return SimpleNamespace(
            generated=self.generated,
            refused=self.refused,
            skipped=self.skipped,
            config_error=self.discovery_config_error,
        )

    async def _reminders(self, session: object, *args: object, **kwargs: object) -> object:
        self.calls.append(("reminders", session))
        if self.reminders_raises is not None:
            raise self.reminders_raises
        return SimpleNamespace(enqueued=self.enqueued, config_error=self.sweep_config_error)

    async def _send(self, session: object, *args: object, **kwargs: object) -> object:
        self.calls.append(("send", session))
        if self.send_raises is not None:
            raise self.send_raises
        return SimpleNamespace(
            count=lambda disposition: self.sent if disposition is SendDisposition.SENT else 0
        )


async def _run(legs: _Legs) -> tuple[commands.OrchestrationReport, object]:
    session = object()
    report = await commands._run_orchestration(
        session=session, store=object(), scanner=object(), tenant_id=TENANT_ID, now=NOW
    )
    return report, session


async def test_legs_run_in_the_pinned_order(monkeypatch: pytest.MonkeyPatch) -> None:
    legs = _Legs(monkeypatch)
    report, session = await _run(legs)
    assert [name for name, _ in legs.calls] == [
        "ingest",
        "parse",
        "discovery",
        "reminders",
        "send",
    ]
    assert {seen for _, seen in legs.calls} == {session}
    assert report.causes == ()


async def test_mail_not_connected_is_a_skip_not_a_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    legs = _Legs(monkeypatch)
    legs.ingest_raises = mail.MailNotConnected()
    report, _ = await _run(legs)
    assert [name for name, _ in legs.calls] == [
        "ingest",
        "parse",
        "discovery",
        "reminders",
        "send",
    ]
    assert report.counts["mail_ingested"] == 0
    assert report.counts["cycles_failed"] == 0
    assert report.causes == ()


async def test_a_raising_leg_is_contained_and_named(monkeypatch: pytest.MonkeyPatch) -> None:
    legs = _Legs(monkeypatch)
    legs.reminders_raises = RuntimeError("boom")
    report, _ = await _run(legs)
    assert [name for name, _ in legs.calls] == [
        "ingest",
        "parse",
        "discovery",
        "reminders",
        "send",
    ]
    assert report.counts["reminders_enqueued"] == 0
    assert report.counts["cycles_failed"] == 1
    assert report.causes == ("reminders:RuntimeError",)


async def test_two_failed_legs_count_two_and_name_both_in_leg_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    legs = _Legs(monkeypatch)
    legs.reminders_raises = RuntimeError("boom")
    legs.send_raises = ConnectionError("smtp down")
    report, _ = await _run(legs)
    assert report.counts["cycles_failed"] == 2
    assert report.causes == ("reminders:RuntimeError", "send:ConnectionError")


async def test_sweep_config_error_is_a_failed_leg(monkeypatch: pytest.MonkeyPatch) -> None:
    legs = _Legs(monkeypatch)
    legs.enqueued = 0
    legs.sweep_config_error = "stored configuration unreadable"
    report, _ = await _run(legs)
    assert report.counts["cycles_failed"] == 1
    assert report.causes == ("reminders:config_error",)


async def test_a_raising_discovery_leg_is_contained_and_named(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The discovery leg fails the same way reminders does: contained, named
    ``discovery:<Class>``, and the legs after it still run."""
    legs = _Legs(monkeypatch)
    legs.discovery_raises = RuntimeError("boom")
    report, _ = await _run(legs)
    assert [name for name, _ in legs.calls] == [
        "ingest",
        "parse",
        "discovery",
        "reminders",
        "send",
    ]
    assert report.counts["confirmations_generated"] == 0
    assert report.counts["cycles_failed"] == 1
    assert report.causes == ("discovery:RuntimeError",)


async def test_discovery_config_error_is_a_failed_leg(monkeypatch: pytest.MonkeyPatch) -> None:
    legs = _Legs(monkeypatch)
    legs.discovery_config_error = "stored configuration unreadable"
    report, _ = await _run(legs)
    assert report.counts["confirmations_generated"] == 6
    assert report.counts["cycles_failed"] == 1
    assert report.causes == ("discovery:config_error",)


async def test_no_mailbox_still_runs_discovery_without_a_failure_cause(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No mailbox is not a leg failure: discovery's generation is local, so
    the leg runs and any undeliverable request is one of the sweep's own
    refused counts — never a cause. Delivery belongs to the send leg."""
    legs = _Legs(monkeypatch)
    legs.ingest_raises = mail.MailNotConnected()
    report, _ = await _run(legs)
    assert [name for name, _ in legs.calls] == [
        "ingest",
        "parse",
        "discovery",
        "reminders",
        "send",
    ]
    assert report.counts["mail_ingested"] == 0
    assert report.counts["confirmations_generated"] == 6
    assert report.counts["cycles_failed"] == 0
    assert report.causes == ()


async def test_counts_map_to_the_eight_telemetry_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    legs = _Legs(monkeypatch)
    report, _ = await _run(legs)
    assert report.counts == {
        "mail_ingested": 3,
        "replies_parsed": 2,
        "confirmations_generated": 6,
        "confirmations_refused": 1,
        "confirmations_skipped": 2,
        "reminders_enqueued": 4,
        "messages_sent": 5,
        "cycles_failed": 0,
    }


async def test_no_local_tenant_is_a_skip(monkeypatch: pytest.MonkeyPatch) -> None:
    legs = _Legs(monkeypatch)
    legs.ingest_raises = mail_ingest.NoLocalTenantError(reason="no tenants row")
    report, _ = await _run(legs)
    assert [name for name, _ in legs.calls] == [
        "ingest",
        "parse",
        "discovery",
        "reminders",
        "send",
    ]
    assert report.counts["mail_ingested"] == 0
    assert report.counts["cycles_failed"] == 0
    assert report.causes == ()


async def test_the_handler_wraps_causes_in_orchestration_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed sweep is not a success with low counts: the handler raises,
    carrying the counts and the leg-ordered causes for the outcome record."""

    async def failed_sweep(**kwargs: object) -> commands.OrchestrationReport:
        return commands.OrchestrationReport(
            counts={
                "mail_ingested": 3,
                "replies_parsed": 0,
                "reminders_enqueued": 0,
                "messages_sent": 0,
                "cycles_failed": 1,
            },
            causes=("reminders:RuntimeError",),
        )

    monkeypatch.setattr(commands, "_run_orchestration", failed_sweep)
    monkeypatch.setattr(commands.documents, "get_storage", lambda: object())
    monkeypatch.setattr(commands.scanning, "scanner", lambda: object())

    with pytest.raises(commands.OrchestrationFailedError) as exc_info:
        await commands._schedule_orchestration(
            envelope=None,  # the orchestration handler reads nothing from the envelope
            session=None,  # and takes the session it was handed, straight through
            tenant_id=TENANT_ID,
            now=NOW,
        )
    assert exc_info.value.causes == ("reminders:RuntimeError",)
    assert exc_info.value.counts["cycles_failed"] == 1
