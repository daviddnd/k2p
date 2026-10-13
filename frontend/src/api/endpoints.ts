/**
 * The execution-plane calls this UI makes, in one place.
 *
 * Paths are relative (`/api/v1/...`): in production nginx serves the SPA and
 * proxies `/api/` to the backend; in development the Vite proxy in
 * `vite.config.ts` forwards the same path to `http://localhost:8000`.
 */

import { request } from './client'
import type { AuditReport } from '../auditReport'
import type {
  CalendarProbeResult,
  CalendarProviderName,
  CalendarResponse,
  ChangeEvent,
  ChangeEventQueueItem,
  ChangeEventTransitions,
  ClientActionsResponse,
  CompanyMasterData,
  CreateChangeEventRequest,
  DashboardResponse,
  Draft,
  DraftGenerateRequest,
  DraftTransitionRequest,
  DraftTransitions,
  ExtractionAssessment,
  ExportMode,
  FeedCredential,
  FilingPackage,
  ImportBatch,
  ImportBatchRecord,
  ImportBatchSummary,
  IssuedFeedCredential,
  LoginResponse,
  MfaEnrollmentBegin,
  MfaEnrollmentComplete,
  MfaStatus,
  OfficerAppointment,
  OfficerAppointmentCreate,
  OfficerAppointmentUpdate,
  OnboardingGuide,
  ProviderCredential,
  ProviderCredentialRequest,
  PushStatus,
  RenewalDecision,
  RiskAcknowledgment,
  ServiceRenewal,
  Shareholding,
  ShareholdingCreate,
  ShareholdingUpdate,
  TenantConfig,
  TenantConfigRequest,
  TransitionRequest,
} from './types'

export function login(email: string, password: string): Promise<LoginResponse> {
  return request<LoginResponse>('/api/v1/auth/login', {
    method: 'POST',
    body: { email, password },
    auth: false,
  })
}

// --- Staff MFA (phase 7, scope §8; slice ① opened enrollment to General
// staff, 2026-10-11): the challenge, forced enrollment, and the settings
// surface's self-service binding ---
// There is deliberately no `clearMfa` here: the R5 recovery is an operator
// procedure against the API (or the runbook), not something this UI calls.

/** The caller's own MFA facts — the read the session gate and the enrollment
 * screen route on; reachable with an unverified session (the gate allowlists
 * it). */
export function fetchMfaStatus(): Promise<MfaStatus> {
  return request<MfaStatus>('/api/v1/auth/mfa/status')
}

/** Enrollment begin: the ONLY call whose answer carries the plaintext secret.
 * The route holds it in component state — never sessionStorage — and hands it
 * back with the confirming code. */
export function beginMfaEnrollment(): Promise<MfaEnrollmentBegin> {
  return request<MfaEnrollmentBegin>('/api/v1/auth/mfa/enrollment', { method: 'POST' })
}

/** Enrollment complete: the caller-held secret returns with the confirming
 * code; a wrong or malformed code is one named 422 `totp_invalid`. */
export function completeMfaEnrollment(
  code: string,
  secret: string,
): Promise<MfaEnrollmentComplete> {
  return request<MfaEnrollmentComplete>('/api/v1/auth/mfa/enrollment/complete', {
    method: 'POST',
    body: { code, secret },
  })
}

/** The login challenge. On success the presented session is verified; a wrong,
 * malformed or replayed code is one named 422 `totp_invalid` and the session
 * stays in the challenge state. */
export function verifyMfaCode(code: string): Promise<MfaStatus> {
  return request<MfaStatus>('/api/v1/auth/mfa/verify', { method: 'POST', body: { code } })
}

// --- The pre-use risk acknowledgment (the 2026-10-04 founder rulings) --------

/** The app gate's read, fetched on load and never cached: the served statement
 * and whether this deployment's acknowledgment is current. A statement version
 * bump answers `acknowledged: false` again — the interstitial re-opens by
 * construction, not by any client-side bookkeeping. Open to every active
 * account, because every role meets the interstitial. */
export function fetchRiskAcknowledgment(): Promise<RiskAcknowledgment> {
  return request<RiskAcknowledgment>('/api/v1/risk-acknowledgment')
}

/** Records the Administrator's confirmation; the write is Administrator-only
 * and idempotent (a repeat POST is a no-op answering the current state). The
 * reply is the resulting state read back. */
export function acceptRiskAcknowledgment(): Promise<RiskAcknowledgment> {
  return request<RiskAcknowledgment>('/api/v1/risk-acknowledgment', { method: 'POST' })
}

export function fetchDashboard(): Promise<DashboardResponse> {
  return request<DashboardResponse>('/api/v1/dashboard')
}

export function fetchCalendar(from: string, to: string): Promise<CalendarResponse> {
  const query = new URLSearchParams({ from, to })
  return request<CalendarResponse>(`/api/v1/compliance/calendar?${query.toString()}`)
}

// --- One company: master data, and the ingestion review queue ---------------

export function fetchCompanyMasterData(
  companyId: string,
  asOf: string,
): Promise<CompanyMasterData> {
  const query = new URLSearchParams({ as_of: asOf })
  return request<CompanyMasterData>(
    `/api/v1/companies/${companyId}/master-data?${query.toString()}`,
  )
}

export function fetchImportBatches(companyId: string): Promise<ImportBatchSummary[]> {
  return request<ImportBatchSummary[]>(`/api/v1/companies/${companyId}/import-batches`)
}

export function fetchImportBatch(companyId: string, batchId: string): Promise<ImportBatch> {
  return request<ImportBatch>(`/api/v1/companies/${companyId}/import-batches/${batchId}`)
}

/** The §5.1 Audit compliance report: the company's entries plus the tenant-chain
 * verdict. Uncapped in every tier (ADR-0012) — the company's own audit trail. */
export function fetchCompanyAuditReport(companyId: string): Promise<AuditReport> {
  return request<AuditReport>(`/api/v1/companies/${companyId}/audit-report`)
}

/** Confirm one proposal. `effectiveDate` is the reviewer's when the filing
 * states none; the API refuses (422) rather than guessing one. */
export function confirmImportRecord(
  companyId: string,
  batchId: string,
  recordId: string,
  effectiveDate: string | null,
): Promise<ImportBatchRecord> {
  return request<ImportBatchRecord>(
    `/api/v1/companies/${companyId}/import-batches/${batchId}/records/${recordId}/confirm`,
    { method: 'POST', body: { effective_date: effectiveDate } },
  )
}

/** Reject one proposal; nothing is written to master data. */
export function rejectImportRecord(
  companyId: string,
  batchId: string,
  recordId: string,
): Promise<ImportBatchRecord> {
  return request<ImportBatchRecord>(
    `/api/v1/companies/${companyId}/import-batches/${batchId}/records/${recordId}/reject`,
    { method: 'POST' },
  )
}

// --- The calendar subscription feed and its credential ----------------------

export function fetchFeedCredentials(): Promise<FeedCredential[]> {
  return request<FeedCredential[]>('/api/v1/calendar/feed-credentials')
}

/** Creates the tenant's feed credential; the URL comes back exactly once. */
export function createFeedCredential(): Promise<IssuedFeedCredential> {
  return request<IssuedFeedCredential>('/api/v1/calendar/feed-credentials', {
    method: 'POST',
  })
}

export function revokeFeedCredential(credentialId: string): Promise<FeedCredential> {
  return request<FeedCredential>(`/api/v1/calendar/feed-credentials/${credentialId}/revoke`, {
    method: 'POST',
  })
}

// --- The calendar push (slice M8): provider credential, probe, status, guide

/** The stored provider credential's metadata, or `connected: false`. Never
 *  calls the provider, so it answers during an outage; the probe is what
 *  asks the provider. */
export function fetchProviderCredential(): Promise<ProviderCredential> {
  return request<ProviderCredential>('/api/v1/calendar/provider-credentials')
}

/** Store (or replace) the tenant's provider credential; 422 on a malformed
 *  body, metadata only in the reply. Replacing resets the probe flag. */
export function saveProviderCredential(
  body: ProviderCredentialRequest,
): Promise<ProviderCredential> {
  return request<ProviderCredential>('/api/v1/calendar/provider-credentials', {
    method: 'POST',
    body,
  })
}

/** Forget the provider credential; the tenant falls back to the feed.
 *  Nothing already pushed is deleted (ADR-0012). */
export function deleteProviderCredential(): Promise<ProviderCredential> {
  return request<ProviderCredential>('/api/v1/calendar/provider-credentials', {
    method: 'DELETE',
  })
}

/** Ruling R5's honest probe: refresh, list, write a probe event, delete it.
 *  Every leg reports its own outcome; the run moves the credential's
 *  `probe_passed` flag to the answer. 409 (`not_connected`) with no credential. */
export function runCalendarProbe(): Promise<CalendarProbeResult> {
  return request<CalendarProbeResult>('/api/v1/calendar/probe', { method: 'POST' })
}

/** How each company's deadlines reach its calendar: pushed, fed, or refused
 *  with the §5.1 gate's own reason. Reads; never calls the provider. */
export function fetchPushStatus(): Promise<PushStatus> {
  return request<PushStatus>('/api/v1/calendar/push-status')
}

/** The per-provider registration walk (ruling R6) from the one static
 *  source — the wizard renders this, nothing hard-coded client-side. */
export function fetchOnboardingGuide(provider: CalendarProviderName): Promise<OnboardingGuide> {
  const query = new URLSearchParams({ provider })
  return request<OnboardingGuide>(`/api/v1/calendar/onboarding-guide?${query.toString()}`)
}

// --- The tenant configuration, the panel, and the renewal record -----------

/** The effective configuration; the shipped defaults when none is stored. */
export function fetchTenantConfig(): Promise<TenantConfig> {
  return request<TenantConfig>('/api/v1/tenant-config')
}

/** Saves the **whole** configuration: both blocks travel, and the PUT replaces both. */
export function saveTenantConfig(payload: TenantConfigRequest): Promise<TenantConfig> {
  return request<TenantConfig>('/api/v1/tenant-config', { method: 'PUT', body: payload })
}

/** The §5.1.9 panel: what each period needs next, within the read's window. */
export function fetchClientActions(dueWithin: number): Promise<ClientActionsResponse> {
  const query = new URLSearchParams({ due_within: String(dueWithin) })
  return request<ClientActionsResponse>(`/api/v1/client-actions?${query.toString()}`)
}

/** Create-or-return the period's record (idempotent, always 200). */
export function openServiceRenewal(
  companyId: string,
  anniversaryYear: number,
): Promise<ServiceRenewal> {
  return request<ServiceRenewal>(`/api/v1/companies/${companyId}/service-renewals`, {
    method: 'POST',
    body: { anniversary_year: anniversaryYear },
  })
}

/** Record the client's answer; a decline is refused without a note. */
export function recordRenewalDecision(
  companyId: string,
  anniversaryYear: number,
  decision: RenewalDecision,
  note: string | null,
): Promise<ServiceRenewal> {
  return request<ServiceRenewal>(
    `/api/v1/companies/${companyId}/service-renewals/${anniversaryYear}/decision`,
    { method: 'POST', body: { decision, note } },
  )
}

/** Record that the fee arrived: a timestamp, the actor, a note — nothing more. */
export function recordRenewalPayment(
  companyId: string,
  anniversaryYear: number,
  note: string | null,
): Promise<ServiceRenewal> {
  return request<ServiceRenewal>(
    `/api/v1/companies/${companyId}/service-renewals/${anniversaryYear}/payment`,
    { method: 'POST', body: { note } },
  )
}

/** Put a recorded decision right; the reason is required by the route. */
export function correctRenewalDecision(
  companyId: string,
  anniversaryYear: number,
  decision: RenewalDecision,
  note: string | null,
): Promise<ServiceRenewal> {
  return request<ServiceRenewal>(
    `/api/v1/companies/${companyId}/service-renewals/${anniversaryYear}/correction`,
    { method: 'POST', body: { decision, note } },
  )
}

// --- Change events: the queue, one company's events, the lifecycle ---------

/** The cross-company review queue: current versions awaiting staff, soonest
 * statutory deadline first. Grant-scoped by the API; it carries no actions. */
export function fetchChangeEventQueue(): Promise<ChangeEventQueueItem[]> {
  return request<ChangeEventQueueItem[]>('/api/v1/change-events')
}

/** One company's events, newest first, every entry with its version history. */
export function fetchCompanyChangeEvents(companyId: string): Promise<ChangeEvent[]> {
  return request<ChangeEvent[]>(`/api/v1/companies/${companyId}/change-events`)
}

/** Enter one change event by hand (scope §2.7's fallback input). */
export function createChangeEvent(
  companyId: string,
  body: CreateChangeEventRequest,
): Promise<ChangeEvent> {
  return request<ChangeEvent>(`/api/v1/companies/${companyId}/change-events`, {
    method: 'POST',
    body,
  })
}

/** The actions **this actor** may perform on the event right now — the sole
 * allowed-actions authority; the UI renders buttons from this and no rule of
 * its own. */
export function fetchChangeEventTransitions(eventId: string): Promise<ChangeEventTransitions> {
  return request<ChangeEventTransitions>(`/api/v1/change-events/${eventId}/transitions`)
}

/** Perform one action; the reply is the updated event echo. */
export function postChangeEventTransition(
  eventId: string,
  body: TransitionRequest,
): Promise<ChangeEvent> {
  return request<ChangeEvent>(`/api/v1/change-events/${eventId}/transitions`, {
    method: 'POST',
    body,
  })
}

/** The §2.7 assessments behind a parse of this event; truthfully empty until
 * the reply parser exists. */
export function fetchChangeEventAssessments(eventId: string): Promise<ExtractionAssessment[]> {
  return request<ExtractionAssessment[]>(`/api/v1/change-events/${eventId}/assessments`)
}

// --- The officer appointments and the shareholdings (F1's reads) -----------

/** The company's appointments, newest `valid_from` first. `asOf` narrows to
 * the rows in office that day; without it every row is returned. */
export function fetchOfficers(
  companyId: string,
  asOf?: string,
): Promise<OfficerAppointment[]> {
  const query = asOf === undefined ? '' : `?${new URLSearchParams({ as_of: asOf }).toString()}`
  return request<OfficerAppointment[]>(`/api/v1/companies/${companyId}/officers${query}`)
}

/** Record one office: a new person's fields travel in `person`. */
export function createOfficer(
  companyId: string,
  body: OfficerAppointmentCreate,
): Promise<OfficerAppointment> {
  return request<OfficerAppointment>(`/api/v1/companies/${companyId}/officers`, {
    method: 'POST',
    body,
  })
}

/** Correct one appointment in place (appointment fields only — the person's
 * identity is the Administrator-only persons surface's). */
export function updateOfficer(
  appointmentId: string,
  body: OfficerAppointmentUpdate,
): Promise<OfficerAppointment> {
  return request<OfficerAppointment>(`/api/v1/officers/${appointmentId}`, {
    method: 'PATCH',
    body,
  })
}

/** Remove one appointment; 204. A cessation is a window correction, not this. */
export function deleteOfficer(appointmentId: string): Promise<void> {
  return request<void>(`/api/v1/officers/${appointmentId}`, { method: 'DELETE' })
}

/** The company's shareholdings, newest first; `asOf` narrows to the effective ones. */
export function fetchShareholdings(
  companyId: string,
  asOf?: string,
): Promise<Shareholding[]> {
  const query = asOf === undefined ? '' : `?${new URLSearchParams({ as_of: asOf }).toString()}`
  return request<Shareholding[]>(`/api/v1/companies/${companyId}/shareholdings${query}`)
}

export function createShareholding(
  companyId: string,
  body: ShareholdingCreate,
): Promise<Shareholding> {
  return request<Shareholding>(`/api/v1/companies/${companyId}/shareholdings`, {
    method: 'POST',
    body,
  })
}

export function updateShareholding(
  shareholdingId: string,
  body: ShareholdingUpdate,
): Promise<Shareholding> {
  return request<Shareholding>(`/api/v1/shareholdings/${shareholdingId}`, {
    method: 'PATCH',
    body,
  })
}

export function deleteShareholding(shareholdingId: string): Promise<void> {
  return request<void>(`/api/v1/shareholdings/${shareholdingId}`, { method: 'DELETE' })
}

// --- The drafts: generation, the list, one draft and its actions -----------

/** Generate one draft version — the gated automation. A denial is the API's
 * 403 `automation_denied: <reason>` and writes nothing. */
export function generateDraft(
  companyId: string,
  body: DraftGenerateRequest,
): Promise<Draft> {
  return request<Draft>(`/api/v1/companies/${companyId}/drafts`, { method: 'POST', body })
}

/** The company's drafts: each lineage's current version, newest first. */
export function fetchCompanyDrafts(companyId: string): Promise<Draft[]> {
  return request<Draft[]>(`/api/v1/companies/${companyId}/drafts`)
}

/** One draft: the current version in full, plus the lineage's earlier versions. */
export function fetchDraft(draftId: string): Promise<Draft> {
  return request<Draft>(`/api/v1/drafts/${draftId}`)
}

/** The actions **this actor** may perform on the draft right now — the sole
 * allowed-actions authority; the UI renders buttons from this and no rule of
 * its own. */
export function fetchDraftTransitions(draftId: string): Promise<DraftTransitions> {
  return request<DraftTransitions>(`/api/v1/drafts/${draftId}/transitions`)
}

/** Perform one action; the reply is the updated draft echo. */
export function postDraftTransition(
  draftId: string,
  body: DraftTransitionRequest,
): Promise<Draft> {
  return request<Draft>(`/api/v1/drafts/${draftId}/transitions`, { method: 'POST', body })
}

/**
 * The client-signed copy (scope §2.10): the file's own bytes ride as the raw
 * body with its type as the declared `Content-Type` — never a JSON wrapper —
 * and the route stores the WORM `signed_draft` document and moves the draft to
 * `signature_received` in the one request. The reply is the moved draft echo.
 */
export function uploadSignedDraft(draftId: string, file: File): Promise<Draft> {
  return request<Draft>(`/api/v1/drafts/${draftId}/signature`, {
    method: 'POST',
    rawBody: file,
    contentType: file.type,
  })
}

/**
 * Export one approved draft as a submission package (§2.12): the canonical
 * export door — the mode travels in this route's body, not the transitions
 * door's. The reply is the FilingPackage (the stored artifact's document id,
 * the derived envelope, the exporting actor); no mode submits anything.
 */
export function exportDraft(draftId: string, mode: ExportMode): Promise<FilingPackage> {
  return request<FilingPackage>(`/api/v1/drafts/${draftId}/exports`, {
    method: 'POST',
    body: { mode },
  })
}
