"""Validate the SSR verification fixtures against the backend's own response
models — the check that the hand-written TypeScript types and the fixtures are
not guesses about the wire shape.

Run after `npm run verify` (which writes `dist-ssr/fixtures.json`), with the
backend virtualenv, because the models live in `backend/`:

    backend/.venv/bin/python frontend/verify/check-fixtures.py

Each fixture must validate against its endpoint's Pydantic response model with
`extra="forbid"`, and then round-trip to itself under
`model_dump(mode="json", by_alias=True)` — that equality is what catches a
misspelled, missing, or extra field at any nesting depth, plus alias drift
(`CalendarResponse` carries the range start as the Python field `from_`, which
FastAPI puts on the wire under its serialization alias `from`).

A fixture may be a list (`[Model]`); every element is checked.

Read-only: it imports the backend modules, it never writes anything there.
"""

from __future__ import annotations

import json
import pathlib
import sys

from pydantic import BaseModel

HERE = pathlib.Path(__file__).resolve()
REPO = HERE.parents[2]
sys.path.insert(0, str(REPO / "backend"))

from app.api.audit_report import AuditReportResponse  # noqa: E402
from app.api.auth import LoginResponse, MfaStatusResponse  # noqa: E402
from app.api.calendar_sync import (  # noqa: E402
    CalendarProbeResponse,
    FeedCredentialResponse,
    IssuedFeedCredentialResponse,
    OnboardingGuideResponse,
    ProviderCredentialResponse,
    PushStatusResponse,
)
from app.api.change_events import (  # noqa: E402
    ChangeEventQueueItemResponse,
    ChangeEventResponse,
    ChangeEventTransitionsResponse,
)
from app.api.communications import CommunicationResponse, ReplyParseResponse  # noqa: E402
from app.api.companies import CompanyMasterDataResponse  # noqa: E402
from app.api.compliance import CalendarResponse, DashboardResponse  # noqa: E402
from app.api.drafts import (  # noqa: E402
    DraftResponse,
    DraftTransitionsResponse,
    FilingPackageResponse,
    SignaturePackageResponse,
)
from app.api.officers import OfficerAppointmentResponse, ShareholdingResponse  # noqa: E402
from app.api.prior_filing import (  # noqa: E402
    ImportBatchResponse,
    ImportBatchSummaryResponse,
)
from app.api.risk_acknowledgment import RiskAcknowledgmentResponse  # noqa: E402
from app.api.service_renewals import ClientActionsResponse, RenewalResponse  # noqa: E402
from app.api.tenant_config import TenantConfigResponse  # noqa: E402

FIXTURES_PATH = HERE.parents[1] / "dist-ssr" / "fixtures.json"

MODELS: dict[str, type[BaseModel] | list[type[BaseModel]]] = {
    "login": LoginResponse,
    "mfaStatus": MfaStatusResponse,
    "auditReport": AuditReportResponse,
    "auditReportChainProblems": AuditReportResponse,
    "dashboardPopulated": DashboardResponse,
    "dashboardEmpty": DashboardResponse,
    "dashboardDormantOnly": DashboardResponse,
    "dashboardUnlicensed": DashboardResponse,
    "calendarPopulated": CalendarResponse,
    "companyMasterData": CompanyMasterDataResponse,
    "importBatches": [ImportBatchSummaryResponse],
    "importBatch": ImportBatchResponse,
    "importBatchNoEffectiveDate": ImportBatchResponse,
    "importBatchDecided": ImportBatchResponse,
    "importBatchNeedsOcr": ImportBatchResponse,
    "feedCredentials": [FeedCredentialResponse],
    "issuedFeedCredential": IssuedFeedCredentialResponse,
    "providerCredentialNone": ProviderCredentialResponse,
    "providerCredentialSaved": ProviderCredentialResponse,
    "providerCredentialProbed": ProviderCredentialResponse,
    "probePassed": CalendarProbeResponse,
    "probeFailed": CalendarProbeResponse,
    "pushStatusFeed": PushStatusResponse,
    "pushStatusPushing": PushStatusResponse,
    "pushStatusUnprobed": PushStatusResponse,
    "onboardingGuideMicrosoft": OnboardingGuideResponse,
    "onboardingGuideGoogle": OnboardingGuideResponse,
    "tenantConfig": TenantConfigResponse,
    "tenantConfigDefaults": TenantConfigResponse,
    "tenantConfigUnreadable": TenantConfigResponse,
    "clientActions": ClientActionsResponse,
    "clientActionsEmpty": ClientActionsResponse,
    "serviceRenewal": RenewalResponse,
    "companyChangeEvents": [ChangeEventResponse],
    "changeEventQueue": [ChangeEventQueueItemResponse],
    "changeEventTransitions": ChangeEventTransitionsResponse,
    "changeEventTransitionsEmpty": ChangeEventTransitionsResponse,
    "companyCommunications": [CommunicationResponse],
    "companyCommunicationsEmpty": [CommunicationResponse],
    "replyParse": ReplyParseResponse,
    "riskAckUnacknowledged": RiskAcknowledgmentResponse,
    "riskAckAcknowledged": RiskAcknowledgmentResponse,
    "riskAckReopened": RiskAcknowledgmentResponse,
    "officers": [OfficerAppointmentResponse],
    "officersEmpty": [OfficerAppointmentResponse],
    "shareholdings": [ShareholdingResponse],
    "shareholdingsEmpty": [ShareholdingResponse],
    "companyDrafts": [DraftResponse],
    "companyDraftsEmpty": [DraftResponse],
    "draftDetail": DraftResponse,
    "draftDetailMinimal": DraftResponse,
    "draftTransitions": DraftTransitionsResponse,
    "draftTransitionsEmpty": DraftTransitionsResponse,
    "draftSignaturePackage": SignaturePackageResponse,
    "filingPackage": FilingPackageResponse,
}


def python_names(payload: dict, model: type[BaseModel]) -> dict:
    """The wire keys, mapped back to the Python field names to validate."""
    aliases = {
        field.serialization_alias: name
        for name, field in model.model_fields.items()
        if field.serialization_alias and field.serialization_alias != name
    }
    return {aliases.get(key, key): value for key, value in payload.items()}


def check_one(name: str, fixture: object, model: type[BaseModel]) -> bool:
    assert isinstance(fixture, dict)
    parsed = model.model_validate(python_names(fixture, model), extra="forbid")
    dumped = parsed.model_dump(mode="json", by_alias=True)
    if dumped == fixture:
        return True
    print(f"FAIL {name}: does not round-trip as {model.__name__}")
    print(f"     model:   {json.dumps(dumped, sort_keys=True)[:400]}")
    print(f"     fixture: {json.dumps(fixture, sort_keys=True)[:400]}")
    return False


def main() -> int:
    fixtures = json.loads(FIXTURES_PATH.read_text(encoding="utf-8"))
    failures = 0
    checked = 0
    for name, model in MODELS.items():
        fixture = fixtures[name]
        if isinstance(model, list):
            items = [check_one(f"{name}[{index}]", item, model[0]) for index, item in enumerate(fixture)]
            passed = all(items)
            count = len(items)
        else:
            passed = check_one(name, fixture, model)
            count = 1
        checked += count
        if passed:
            printed = model[0].__name__ if isinstance(model, list) else model.__name__
            suffix = f" ({count} items)" if isinstance(model, list) else ""
            print(f"ok   {name}: validates as {printed}{suffix} and round-trips exactly")
        else:
            failures += 1
    print(f"{len(MODELS) - failures}/{len(MODELS)} fixtures match the backend models ({checked} values)")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
