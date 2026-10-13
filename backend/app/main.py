"""FastAPI application entry point for the execution plane."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.activation import router as activation_router
from app.api.attribution import router as attribution_router
from app.api.audit_report import router as audit_report_router
from app.api.auth import router as auth_router
from app.api.bootstrap import router as bootstrap_router
from app.api.calendar_sync import router as calendar_sync_router
from app.api.change_events import router as change_events_router
from app.api.commands import router as commands_router
from app.api.communications import router as communications_router
from app.api.companies import router as companies_router
from app.api.compliance import router as compliance_router
from app.api.confirmation import router as confirmation_router
from app.api.contacts import router as contacts_router
from app.api.documents import get_storage
from app.api.documents import router as documents_router
from app.api.documents import scan_router as documents_scan_router
from app.api.drafts import router as drafts_router
from app.api.entitlement import router as entitlement_router
from app.api.health import router as health_router
from app.api.identity import router as identity_router
from app.api.integrity import router as integrity_router
from app.api.mail import router as mail_router
from app.api.officers import router as officers_router
from app.api.packs import router as packs_router
from app.api.prior_filing import router as prior_filing_router
from app.api.quarantine import router as quarantine_router
from app.api.risk_acknowledgment import router as risk_acknowledgment_router
from app.api.service_packages import router as service_packages_router
from app.api.service_renewals import router as service_renewals_router
from app.api.split_items import router as split_items_router
from app.api.tenant_config import router as tenant_config_router
from app.core.config import APP_VERSION, settings
from app.workers import calendar_push as calendar_push_worker
from app.workers import commands as command_worker
from app.workers import confirmation_discovery as confirmation_discovery_worker
from app.workers import integrity as integrity_worker
from app.workers import mail_ingest as mail_ingest_worker
from app.workers import mail_send as mail_send_worker
from app.workers import parse_replies as parse_replies_worker
from app.workers import reminders as reminders_worker


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """Own the background workers' lifetimes — and only when the deployment opts in.

    Eight loops live here, each off by default and each with a synchronous
    trigger that works without it: the command worker polls the vendor control
    plane for command envelopes (ADR-0012), the mail-ingestion worker reads the
    connected mailbox in cycles (ADR-0005), the send worker delivers what
    the release path queued (ADR-0011), the reminder worker sweeps the
    cadence the sweep owns (contract §5.1.12), the reply-parser worker
    parses the replies clients sent (slice M5b, contract §5.1.13), the
    confirmation-discovery worker sweeps the companies whose data-confirmation
    ask date has arrived with no request yet (the discovery slice), the
    calendar-push worker reconciles the registered tenant's provider calendar
    with the projection the feed serves (slice M8, ruling R4), and the
    integrity worker re-verifies the audit chain and the records registry
    on its interval (ADR-0009/0011). A deployment that runs none of them is
    fully functional — the manual path, the send route, the reminder route,
    the parse route, the Administrator's integrity-check route and every read
    path never depended on any of them — and the blob store the ingestion,
    parse, send and integrity loops use is the same process-wide client the
    document routes use, so the deployment holds one storage client, not one
    per caller.
    """
    command_task = (
        command_worker.start_command_worker() if settings.command_worker_enabled else None
    )
    ingest_task = (
        mail_ingest_worker.start_mail_ingest_worker(get_storage())
        if settings.mail_ingest_enabled
        else None
    )
    send_task = (
        mail_send_worker.start_mail_send_worker(get_storage())
        if settings.mail_send_enabled
        else None
    )
    reminders_task = (
        reminders_worker.start_reminders_worker(get_storage())
        if settings.mail_reminders_enabled
        else None
    )
    parse_replies_task = (
        parse_replies_worker.start_parse_replies_worker(get_storage())
        if settings.mail_parse_replies_enabled
        else None
    )
    confirmation_discovery_task = (
        confirmation_discovery_worker.start_confirmation_discovery_worker(get_storage())
        if settings.mail_confirmation_discovery_enabled
        else None
    )
    calendar_push_task = (
        calendar_push_worker.start_calendar_push_worker()
        if settings.calendar_push_enabled
        else None
    )
    integrity_task = (
        integrity_worker.start_integrity_worker(get_storage())
        if settings.integrity_check_enabled
        else None
    )
    try:
        yield
    finally:
        if integrity_task is not None:
            await integrity_worker.stop_integrity_worker(integrity_task)
        if calendar_push_task is not None:
            await calendar_push_worker.stop_calendar_push_worker(calendar_push_task)
        if confirmation_discovery_task is not None:
            await confirmation_discovery_worker.stop_confirmation_discovery_worker(
                confirmation_discovery_task
            )
        if parse_replies_task is not None:
            await parse_replies_worker.stop_parse_replies_worker(parse_replies_task)
        if reminders_task is not None:
            await reminders_worker.stop_reminders_worker(reminders_task)
        if send_task is not None:
            await mail_send_worker.stop_mail_send_worker(send_task)
        if ingest_task is not None:
            await mail_ingest_worker.stop_mail_ingest_worker(ingest_task)
        if command_task is not None:
            await command_worker.stop_command_worker(command_task)


def create_app() -> FastAPI:
    app = FastAPI(title="ai-com-sec execution plane", version=APP_VERSION, lifespan=lifespan)
    app.include_router(health_router)
    app.include_router(bootstrap_router)
    app.include_router(identity_router)
    app.include_router(auth_router)
    app.include_router(companies_router)
    app.include_router(contacts_router)
    app.include_router(tenant_config_router)
    app.include_router(service_packages_router)
    app.include_router(service_renewals_router)
    app.include_router(change_events_router)
    app.include_router(officers_router)
    app.include_router(drafts_router)
    app.include_router(documents_router)
    app.include_router(documents_scan_router)
    app.include_router(prior_filing_router)
    app.include_router(communications_router)
    app.include_router(audit_report_router)
    app.include_router(confirmation_router)
    app.include_router(compliance_router)
    app.include_router(calendar_sync_router)
    app.include_router(entitlement_router)
    app.include_router(activation_router)
    app.include_router(commands_router)
    app.include_router(packs_router)
    app.include_router(mail_router)
    app.include_router(integrity_router)
    app.include_router(quarantine_router)
    app.include_router(attribution_router)
    app.include_router(split_items_router)
    app.include_router(risk_acknowledgment_router)
    return app


app = create_app()
