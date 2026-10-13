/**
 * SSR verification for the views and the API client — run with:
 *
 *   npm run verify
 *   (vite build --ssr verify/ssr-verify.tsx --outDir dist-ssr && node dist-ssr/ssr-verify.js)
 *
 * It is not a test framework: it renders the pure views with the fixtures in
 * `verify/fixtures.ts` (React's SSR renderer runs the components but no
 * effects, which is why the views are pure and the routes do the fetching),
 * asserts on the produced HTML and on the client's failure modes, and writes
 * the fixtures it used to `dist-ssr/fixtures.json` so
 * `verify/check-fixtures.py` can validate them against the backend's own
 * Pydantic response models. Any failed check exits non-zero.
 *
 * What SSR cannot reach: anything a click reveals (a record's confirm/reject
 * form, the revoke confirmation, typing in the search box) and anything a
 * effect produces. Those are covered where they can be — the filter rule is
 * called directly, the request shapes the decision calls send are asserted
 * against a stubbed `fetch` — and reviewed by a human in a browser.
 */

import { writeFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { renderToStaticMarkup } from 'react-dom/server'
import { ApiError, errorText } from '../src/api/client'
import type {
  ChangeEventTransitions,
  DraftTransitions,
  ExtractionAssessment,
  MilestoneBlockReason,
  OfficerCapacity,
} from '../src/api/types'
import {
  beginMfaEnrollment,
  acceptRiskAcknowledgment,
  completeMfaEnrollment,
  confirmImportRecord,
  correctRenewalDecision,
  createChangeEvent,
  createFeedCredential,
  createOfficer,
  createShareholding,
  deleteOfficer,
  deleteProviderCredential,
  deleteShareholding,
  exportDraft,
  fetchChangeEventAssessments,
  fetchChangeEventQueue,
  fetchChangeEventTransitions,
  fetchClientActions,
  fetchCompanyChangeEvents,
  fetchCompanyDrafts,
  fetchDashboard,
  fetchDraft,
  fetchDraftTransitions,
  fetchImportBatch,
  fetchImportBatches,
  fetchMfaStatus,
  fetchOfficers,
  fetchOnboardingGuide,
  fetchProviderCredential,
  fetchPushStatus,
  fetchRiskAcknowledgment,
  fetchShareholdings,
  fetchTenantConfig,
  generateDraft,
  login,
  openServiceRenewal,
  postChangeEventTransition,
  postDraftTransition,
  recordRenewalDecision,
  recordRenewalPayment,
  rejectImportRecord,
  revokeFeedCredential,
  runCalendarProbe,
  saveProviderCredential,
  saveTenantConfig,
  updateOfficer,
  updateShareholding,
  uploadSignedDraft,
  verifyMfaCode,
} from '../src/api/endpoints'
import { calendarSyncCoverage } from '../src/coverage'
import {
  ENTERABLE_CHANGE_EVENT_TYPES,
  OFFERED_OFFICER_KEYS,
  PROTECTED_CLASS_FIELDS,
  actionOpensForm,
  blankEventForm,
  buildPayload,
  capacitiesForRole,
  changeEventActionLabel,
  changeEventActionNote,
  changeEventSourceLabel,
  changeEventStatusLabel,
  changeEventTypeLabel,
  createRequestFrom,
  describeChangeWindow,
  editChangesFrom,
  formStateForType,
  formStateFromChangeEvent,
  payloadRows,
  rolesForType,
  samePayload,
  type EventFormState,
} from '../src/change_events'
import {
  appointmentChangesFrom,
  appointmentCreateFrom,
  appointmentFormFrom,
  appointmentWithRole,
  addressFrom,
  addressLine,
  blankAppointment,
  blankPerson,
  blankShareholding,
  capacityChoices,
  capacityText,
  inOffice,
  namePairsFrom,
  officerRoleLabel,
  personName,
  personTypeLabel,
  personWriteFrom,
  shareholdingChangesFrom,
  shareholdingCreateFrom,
} from '../src/officers'
import {
  DELIVERY_CHOICES,
  EXPORT_MODES,
  companyFactRows,
  draftActionLabel,
  draftActionNote,
  draftEditRequest,
  draftStatusLabel,
  exportModeDescription,
  exportModeLabel,
  filingMetadataFormFrom,
  filingMetadataFrom,
  filingPackageEnvelopeRows,
  formVersionText,
  provenanceRows,
  qcFlagRows,
  resolvedChanges,
  resolvedCompanyName,
  resolvedMembers,
  resolvedOfficers,
  shareCapital,
  signaturePackageEscalationLabel,
  signatureStatusLabel,
  sourceLabel,
  statementConfirmationPending,
  statementFrom,
} from '../src/drafts'
import { blockedReasonNote, correctionTargets, describeRefusal, knownRenewalStatus } from '../src/renewals'
import { hrefForChangeEvent, hrefForCompany, hrefForDraft, parseRoute } from '../src/route'
import { expireSession, getSessionState, mfaChallengePassed, mfaEnrollmentSealed, mfaRequiredFromServer, startSession } from '../src/session'
import { MfaChallengeView } from '../src/views/MfaChallengeView'
import { MfaEnrollmentView } from '../src/views/MfaEnrollmentView'
import { RiskAcknowledgmentView } from '../src/views/RiskAcknowledgmentView'
import { CalendarFeedView } from '../src/views/CalendarFeedView'
import { CalendarPushView } from '../src/views/CalendarPushView'
import { CalendarView } from '../src/views/CalendarView'
import { ChangeEventDetailView } from '../src/views/ChangeEventDetailView'
import { ChangeEventsQueueView } from '../src/views/ChangeEventsQueueView'
import { ClientActionsView } from '../src/views/ClientActionsView'
import { CompanyView, CompanyAuditPanel } from '../src/views/CompanyView'
import { ChangeEventForm } from '../src/components/ChangeEventForm'
import { AddressFields, PersonFields } from '../src/components/PersonFields'
import { FilingMetadataForm } from '../src/components/FilingMetadataForm'
import { DraftDetailView } from '../src/views/DraftDetailView'
import { MilestoneConfigView } from '../src/views/MilestoneConfigView'
import { cardMatches } from '../src/filters'
import { DashboardView } from '../src/views/DashboardView'
import { LoginView } from '../src/views/LoginView'
import { ErrorPanel } from '../src/components/ErrorPanel'
import { RefusalNotice } from '../src/components/RefusalNotice'
import { FilingPackagePanel } from '../src/components/FilingPackagePanel'
import { SignaturePackagePanel } from '../src/components/SignaturePackagePanel'
import {
  auditReport,
  auditReportChainProblems,
  calendarPopulated,
  changeEventAssessments,
  changeEventQueue,
  changeEventQueueEmpty,
  changeEventTransitions,
  changeEventTransitionsEmpty,
  clientActions,
  clientActionsEmpty,
  companyChangeEvents,
  companyChangeEventsEmpty,
  companyCommunications,
  companyCommunicationsEmpty,
  companyDrafts,
  companyDraftsEmpty,
  companyMasterData,
  dashboardDormantOnly,
  dashboardEmpty,
  dashboardPopulated,
  dashboardUnlicensed,
  draftDetail,
  draftDetailMinimal,
  draftSignaturePackage,
  draftTransitions,
  draftTransitionsEmpty,
  feedCredentials,
  feedCredentialsEmpty,
  filingPackage,
  importBatch,
  importBatchDecided,
  importBatchNeedsOcr,
  importBatchNoEffectiveDate,
  importBatches,
  importBatchesEmpty,
  issuedFeedCredential,
  loginFixture,
  mfaStatus,
  officers,
  officersEmpty,
  onboardingGuideGoogle,
  onboardingGuideMicrosoft,
  probeFailed,
  probePassed,
  providerCredentialNone,
  providerCredentialProbed,
  providerCredentialSaved,
  pushStatusFeed,
  pushStatusPushing,
  pushStatusUnprobed,
  replyParse,
  riskAckAcknowledged,
  riskAckReopened,
  riskAckUnacknowledged,
  serviceRenewal,
  shareholdings,
  shareholdingsEmpty,
  tenantConfig,
  tenantConfigDefaults,
  tenantConfigUnreadable,
} from './fixtures'

const results: string[] = []
let failures = 0

function check(label: string, passed: boolean): void {
  if (!passed) failures += 1
  results.push(`${passed ? 'ok  ' : 'FAIL'} ${label}`)
}

function contains(scenario: string, html: string, needle: string): void {
  check(`${scenario}: contains ${JSON.stringify(needle)}`, html.includes(needle))
}

function notContains(scenario: string, html: string, needle: string): void {
  check(`${scenario}: does not contain ${JSON.stringify(needle)}`, !html.includes(needle))
}

/** The whole tag around the first occurrence of `needle` — for asserting an
 * attribute without pinning React's own attribute order. */
function elementHTML(html: string, needle: string): string {
  const index = html.indexOf(needle)
  if (index === -1) return ''
  const start = html.lastIndexOf('<', index)
  const end = html.indexOf('>', index)
  return html.slice(start, end + 1)
}

async function captureError(run: () => Promise<unknown>): Promise<unknown> {
  try {
    await run()
    return null
  } catch (error) {
    return error
  }
}

interface Stubbed {
  url: string
  method: string
  body: unknown
  /** The declared Content-Type, for the raw upload's check. */
  contentType: string | null
}

let lastRequest: Stubbed | null = null

function stubFetch(status: number, payload: unknown): void {
  const stub = async (url: string, init?: RequestInit) => {
    const headers = (init?.headers ?? {}) as Record<string, string>
    lastRequest = {
      url,
      method: init?.method ?? 'GET',
      body:
        typeof init?.body === 'string'
          ? JSON.parse(init.body)
          : (init?.body ?? undefined),
      contentType: headers['Content-Type'] ?? null,
    }
    // A 204 must carry no body at all, so the response is built without one.
    return new Response(status === 204 ? null : JSON.stringify(payload), {
      status,
      headers: { 'Content-Type': 'application/json' },
    })
  }
  ;(globalThis as unknown as { fetch: typeof fetch }).fetch = stub as unknown as typeof fetch
}

function stubOffline(): void {
  const stub = async () => {
    throw new TypeError('fetch failed')
  }
  ;(globalThis as unknown as { fetch: typeof fetch }).fetch = stub as unknown as typeof fetch
}

const noop = async () => undefined

// --- The fixtures the views are rendered with -----------------------------

writeFileSync(
  new URL('./fixtures.json', import.meta.url),
  JSON.stringify(
    {
      login: loginFixture,
      mfaStatus,
      auditReport,
      auditReportChainProblems,
      dashboardPopulated,
      dashboardEmpty,
      dashboardDormantOnly,
      dashboardUnlicensed,
      calendarPopulated,
      companyMasterData,
      importBatches,
      importBatch,
      importBatchNoEffectiveDate,
      importBatchDecided,
      importBatchNeedsOcr,
      feedCredentials,
      issuedFeedCredential,
      providerCredentialNone,
      providerCredentialSaved,
      providerCredentialProbed,
      probePassed,
      probeFailed,
      pushStatusFeed,
      pushStatusPushing,
      pushStatusUnprobed,
      onboardingGuideMicrosoft,
      onboardingGuideGoogle,
      tenantConfig,
      tenantConfigDefaults,
      tenantConfigUnreadable,
      clientActions,
      clientActionsEmpty,
      serviceRenewal,
      companyChangeEvents,
      changeEventQueue,
      changeEventTransitions,
      changeEventTransitionsEmpty,
      companyCommunications,
      companyCommunicationsEmpty,
      replyParse,
      riskAckUnacknowledged,
      riskAckAcknowledged,
      riskAckReopened,
      officers,
      officersEmpty,
      shareholdings,
      shareholdingsEmpty,
      companyDrafts,
      companyDraftsEmpty,
      draftDetail,
      draftDetailMinimal,
      draftSignaturePackage,
      draftTransitions,
      draftTransitionsEmpty,
      filingPackage,
    },
    null,
    2,
  ),
)

// --- Dashboard -----------------------------------------------------------

const populated = renderToStaticMarkup(
  <DashboardView data={dashboardPopulated} viewerRole="administrator" />,
)
contains('dashboard/populated', populated, 'Alpha Traders Limited')
contains('dashboard/populated', populated, 'BRN 11112222')
contains('dashboard/populated', populated, '· CR 1234567')
contains('dashboard/populated', populated, 'class="tile-value">4<')
contains('dashboard/populated', populated, '3 days overdue')
contains('dashboard/populated', populated, 'in 14 days')
contains('dashboard/populated', populated, '7-day alert')
contains('dashboard/populated', populated, '15-day alert')
contains('dashboard/populated', populated, 'risk-red')
contains('dashboard/populated', populated, 'risk-amber')
contains('dashboard/populated', populated, '4 records pending')
contains('dashboard/populated', populated, 'Showing 4 of 4 companies')
contains('dashboard/populated', populated, 'href="#/companies/aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"')
contains('dashboard/populated', populated, '>Alpha Traders Limited</a>')
contains('dashboard/populated', populated, 'Delta Public Company Limited')
contains('dashboard/populated', populated, 'nar1_rule_not_established')
contains('dashboard/populated', populated, 'Document extraction')
contains('dashboard/populated', populated, 'does not carry the capability')
contains('dashboard/populated', populated, 'no active service package is assigned')
notContains('dashboard/populated', populated, 'does not carry any automation capability')
// The pending-event-review widget (the payload CE2 shipped): counts per
// company, and the link into the queue that lists the same set.
contains('dashboard/populated', populated, 'Change events awaiting review')
contains('dashboard/populated', populated, '3 in total across the companies you can see')
contains('dashboard/populated', populated, '— 2 events awaiting review')
contains('dashboard/populated', populated, '— 1 event awaiting review')
contains('dashboard/populated', populated, 'href="#/change-events"')
contains('dashboard/populated', populated, 'open the review queue')
// The pending-draft-review widget (the Phase 2 item-3 deferral, slice F3):
// counts per lineage, and each company opening its screen's Drafts section.
contains('dashboard/populated', populated, 'Drafts awaiting review')
contains('dashboard/populated', populated, '2 in total across the companies you can see')
contains('dashboard/populated', populated, '— 1 draft awaiting review')
contains('dashboard/populated', populated, 'where the Drafts section lists the current versions')
contains(
  'dashboard/populated',
  populated,
  `href="${hrefForCompany(dashboardPopulated.pending_draft_review.companies[0].company_id)}"`,
)
// The pending-confirmations widget (slice R, contract §5.1.12): each awaiting
// request with its days elapsed and its reminder chain — one mid-cadence with
// the next due date, one exhausted (staff take over by hand), one the §5.1
// gate blocks (named, not implied).
contains('dashboard/populated', populated, 'Confirmation requests awaiting reply')
contains('dashboard/populated', populated, '3 in total across the companies you can see')
contains('dashboard/populated', populated, '— 2 requests awaiting reply')
contains('dashboard/populated', populated, '— 1 request awaiting reply')
contains('dashboard/populated', populated, '5 days elapsed')
contains('dashboard/populated', populated, '1 of 3 reminders sent')
contains('dashboard/populated', populated, 'next reminder due')
contains('dashboard/populated', populated, 'Reminders exhausted')
contains('dashboard/populated', populated, 'no further reminder will be sent; contact the client by hand')
contains('dashboard/populated', populated, 'no reminder goes out automatically')
contains('dashboard/populated', populated, 'no active service package is assigned')
contains('dashboard/populated', populated, 'open the client-actions panel')
// The old footnote claimed the queue screen was not built; it exists now.
notContains('dashboard/populated', populated, 'the queue screen that renders them is not built yet')
// The old footnote claimed pending client confirmations had no widget; §5.1.12 built it.
notContains('dashboard/populated', populated, 'pending client confirmations')
// Late-fee exposure (slice L, contract §5.1.4): the portfolio total is the
// late fees the overdue deadlines would pay if filed today, and each overdue
// deadline names its own fee; on-time deadlines stay silent about fees.
contains('dashboard/populated', populated, 'Late fees if filed today')
contains('dashboard/populated', populated, 'class="tile-value">870<')
contains('dashboard/populated', populated, 'late fee HK$870 if filed today')
notContains('dashboard/populated', populated, 'HK$105 if filed today')

const emptyAdmin = renderToStaticMarkup(
  <DashboardView data={dashboardEmpty} viewerRole="administrator" />,
)
contains('dashboard/empty', emptyAdmin, 'This portfolio has no companies yet')
// With no companies the widgets do not render — the dashboard is the
// no-companies state, as it always was.
notContains('dashboard/empty', emptyAdmin, 'Change events awaiting review')
notContains('dashboard/empty', emptyAdmin, 'Drafts awaiting review')
notContains('dashboard/empty', emptyAdmin, 'Confirmation requests awaiting reply')
notContains('dashboard/empty', emptyAdmin, '<h2>Companies</h2>')
// The portfolio tiles still render: an empty portfolio's late-fee exposure
// is an honest zero, not an absent tile.
contains('dashboard/empty', emptyAdmin, 'Late fees if filed today')
contains('dashboard/empty', emptyAdmin, 'class="tile-value">0<')

const emptyGeneral = renderToStaticMarkup(<DashboardView data={dashboardEmpty} viewerRole="general" />)
contains('dashboard/empty/general', emptyGeneral, 'no company access grants')

const dormant = renderToStaticMarkup(
  <DashboardView data={dashboardDormantOnly} viewerRole="administrator" />,
)
contains('dashboard/dormant', dormant, 'flagged dormant')
contains('dashboard/dormant', dormant, 'excluded from NAR1 deadline generation')
contains('dashboard/dormant', dormant, 'Showing 2 of 2 companies')
// A portfolio with companies and nothing awaiting event review gets the
// widgets' own empty lines, not a blank.
contains('dashboard/dormant', dormant, 'Change events awaiting review')
contains('dashboard/dormant', dormant, 'No change event is awaiting review.')
contains('dashboard/dormant', dormant, 'Drafts awaiting review')
contains('dashboard/dormant', dormant, 'No draft is awaiting review.')
// The pending-confirmations widget renders its own honest empty line too.
contains('dashboard/dormant', dormant, 'Confirmation requests awaiting reply')
contains('dashboard/dormant', dormant, 'No confirmation request is awaiting a reply.')

const unlicensed = renderToStaticMarkup(
  <DashboardView data={dashboardUnlicensed} viewerRole="administrator" />,
)
contains('dashboard/unlicensed', unlicensed, 'tier_capability_missing')
contains('dashboard/unlicensed', unlicensed, 'does not carry any automation capability')
contains('dashboard/unlicensed', unlicensed, 'No automation: the licence tier carries none.')

// The §2.3 search rule, which the rendered markup cannot exercise: the CR
// number now matches as well as the name and the BRN.
const alphaCard = dashboardPopulated.company_cards[0]
const betaCard = dashboardPopulated.company_cards[1]
const ALL = { search: '', status: 'all', risk: 'all', dueFrom: '', dueTo: '' }
check('search: the CR number matches', cardMatches(alphaCard, { ...ALL, search: '1234567' }))
check('search: the name still matches', cardMatches(alphaCard, { ...ALL, search: 'alpha' }))
check('search: the BRN still matches', cardMatches(alphaCard, { ...ALL, search: '11112222' }))
check('search: another company does not match', !cardMatches(betaCard, { ...ALL, search: '1234567' }))
check(
  'search: a company without a CR number is not matched by one',
  !cardMatches(betaCard, { ...ALL, search: '0000000' }),
)

// --- Company review ------------------------------------------------------

const companyReview = renderToStaticMarkup(
  <CompanyView
    master={companyMasterData}
    batches={importBatches}
    batch={importBatch}
    selectedBatchId={importBatch.id}
    events={companyChangeEvents}
    officers={officers}
    shareholdings={shareholdings}
    drafts={companyDrafts}
    onSelectBatch={() => undefined}
    onDecide={noop}
    onCreate={noop}
    onCreateOfficer={noop}
    onUpdateOfficer={noop}
    onDeleteOfficer={noop}
    onCreateShareholding={noop}
    onUpdateShareholding={noop}
    onDeleteShareholding={noop}
    onGenerateDraft={noop}
    onRefresh={() => undefined}
    auditReport={null}
    auditError={null}
    onOpenAudit={() => undefined}
    onRetryAudit={noop}
    onDownloadAudit={() => undefined}
  />,
)
contains('company/review', companyReview, 'Alpha Traders Limited')
contains('company/review', companyReview, 'BRN 11112222')
contains('company/review', companyReview, '· CR 1234567')
contains('company/review', companyReview, 'Private company')
contains('company/review', companyReview, 'Master data as of')
contains('company/review', companyReview, 'NAR1 — annual return')
contains('company/review', companyReview, 'Scanned image (local OCR)')
contains('company/review', companyReview, '4 runs')
contains('company/review', companyReview, '4 records awaiting a decision')
contains('company/review', companyReview, '7 records — 2 pending, 4 confirmed, 1 rejected')
// The evidence behind a proposal: what it targets, the value, the confidence,
// how it was read, and what the run's routing was.
contains('company/review', companyReview, 'Registered office — flat')
contains('company/review', companyReview, 'company.registered_office.flat')
contains('company/review', companyReview, 'FLAT 99')
contains('company/review', companyReview, 'read from the form field itself')
contains('company/review', companyReview, 'bilingual box split by script')
contains('company/review', companyReview, 'text found inside the field box')
contains('company/review', companyReview, 'pdf-geometry/v1')
contains('company/review', companyReview, 'Staff review')
contains('company/review', companyReview, 'overall score 0.7')
// Confirm writes master data; reject writes nothing. Both said plainly.
contains('company/review', companyReview, 'Confirming a proposal writes it into')
contains('company/review', companyReview, 'Rejecting writes nothing to master data')
// A static target needs no date; a fact the filing did not date says so.
contains('company/review', companyReview, 'BRN (anchor static)')
contains('company/review', companyReview, 'Not applicable — corrected in place')
notContains('company/review', companyReview, 'Not stated by the filing')

const companyUndated = renderToStaticMarkup(
  <CompanyView
    master={companyMasterData}
    batches={importBatches}
    batch={importBatchNoEffectiveDate}
    selectedBatchId={importBatchNoEffectiveDate.id}
    events={companyChangeEvents}
    officers={officers}
    shareholdings={shareholdings}
    drafts={companyDrafts}
    onSelectBatch={() => undefined}
    onDecide={noop}
    onCreate={noop}
    onCreateOfficer={noop}
    onUpdateOfficer={noop}
    onDeleteOfficer={noop}
    onCreateShareholding={noop}
    onUpdateShareholding={noop}
    onDeleteShareholding={noop}
    onGenerateDraft={noop}
    onRefresh={() => undefined}
    auditReport={null}
    auditError={null}
    onOpenAudit={() => undefined}
    onRetryAudit={noop}
    onDownloadAudit={() => undefined}
  />,
)
contains('company/undated', companyUndated, 'Not stated by the filing')
contains('company/undated', companyUndated, 'Confirm…')
contains('company/undated', companyUndated, 'Reject…')

const companyDecided = renderToStaticMarkup(
  <CompanyView
    master={companyMasterData}
    batches={importBatches}
    batch={importBatchDecided}
    selectedBatchId={importBatchDecided.id}
    events={companyChangeEvents}
    officers={officers}
    shareholdings={shareholdings}
    drafts={companyDrafts}
    onSelectBatch={() => undefined}
    onDecide={noop}
    onCreate={noop}
    onCreateOfficer={noop}
    onUpdateOfficer={noop}
    onDeleteOfficer={noop}
    onCreateShareholding={noop}
    onUpdateShareholding={noop}
    onDeleteShareholding={noop}
    onGenerateDraft={noop}
    onRefresh={() => undefined}
    auditReport={null}
    auditError={null}
    onOpenAudit={() => undefined}
    onRetryAudit={noop}
    onDownloadAudit={() => undefined}
  />,
)
contains('company/decided', companyDecided, 'Confirmed')
contains('company/decided', companyDecided, 'Rejected')
contains('company/decided', companyDecided, 'staff 99999999')
contains('company/decided', companyDecided, 'Every proposal in this run has been decided')
notContains('company/decided', companyDecided, 'Confirm…')
notContains('company/decided', companyDecided, 'Reject…')

const companyNeedsOcr = renderToStaticMarkup(
  <CompanyView
    master={companyMasterData}
    batches={importBatches}
    batch={importBatchNeedsOcr}
    selectedBatchId={importBatchNeedsOcr.id}
    events={companyChangeEventsEmpty}
    officers={officersEmpty}
    shareholdings={shareholdingsEmpty}
    drafts={companyDraftsEmpty}
    onSelectBatch={() => undefined}
    onDecide={noop}
    onCreate={noop}
    onCreateOfficer={noop}
    onUpdateOfficer={noop}
    onDeleteOfficer={noop}
    onCreateShareholding={noop}
    onUpdateShareholding={noop}
    onDeleteShareholding={noop}
    onGenerateDraft={noop}
    onRefresh={() => undefined}
    auditReport={null}
    auditError={null}
    onOpenAudit={() => undefined}
    onRetryAudit={noop}
    onDownloadAudit={() => undefined}
  />,
)
// Nothing was proposed, and the screen says why instead of showing an empty
// table as if the run had failed. (No change event either, so the change-event
// panel renders its own empty state rather than a table.)
contains('company/needs-ocr', companyNeedsOcr, 'This run proposed nothing')
contains('company/needs-ocr', companyNeedsOcr, 'the page frame does not match the pinned form')
contains('company/needs-ocr', companyNeedsOcr, 'Nothing assessed')
notContains('company/needs-ocr', companyNeedsOcr, '<table')

const companyNoRuns = renderToStaticMarkup(
  <CompanyView
    master={companyMasterData}
    batches={importBatchesEmpty}
    batch={null}
    selectedBatchId={null}
    events={companyChangeEventsEmpty}
    officers={officers}
    shareholdings={shareholdings}
    drafts={companyDrafts}
    onSelectBatch={() => undefined}
    onDecide={noop}
    onCreate={noop}
    onCreateOfficer={noop}
    onUpdateOfficer={noop}
    onDeleteOfficer={noop}
    onCreateShareholding={noop}
    onUpdateShareholding={noop}
    onDeleteShareholding={noop}
    onGenerateDraft={noop}
    onRefresh={() => undefined}
    auditReport={null}
    auditError={null}
    onOpenAudit={() => undefined}
    onRetryAudit={noop}
    onDownloadAudit={() => undefined}
  />,
)
contains('company/no-runs', companyNoRuns, 'No prior filing has been read for this company yet')
notContains('company/no-runs', companyNoRuns, 'Proposals in this run')
contains('company/no-runs', companyNoRuns, 'No change event has been recorded for this company yet')

// --- The company's officers, members and drafts (slices F1–F3) -------------

// The panels are checked on the same render the review screen uses; the
// fixture rows carry the in-office marker, an out-of-office window, a
// corporate officer with its registered office, and a member's strings.
contains('company/records', companyReview, 'Officers')
contains('company/records', companyReview, 'Members (shareholdings)')
contains('company/records', companyReview, 'Drafts')
contains('company/records', companyReview, 'Wong Ka Ming · 黃嘉明')
contains('company/records', companyReview, 'Company secretary')
contains('company/records', companyReview, 'Director (董事), Alternate director (候補董事)')
contains('company/records', companyReview, 'alternate to Wong Ka Ming')
contains('company/records', companyReview, 'badge status-active">In office')
contains('company/records', companyReview, 'Out of office')
contains('company/records', companyReview, 'Body corporate')
contains('company/records', companyReview, 'Registered office: 21/F, ALPHA TOWER')
// The panels say where a person's identity is corrected.
contains('company/records', companyReview, 'Administrator-only persons surface')
contains('company/records', companyReview, 'Record an appointment…')
contains('company/records', companyReview, 'Record a member…')
contains('company/records', companyReview, '1,000 × Ordinary')
contains('company/records', companyReview, '66.67%')
contains('company/records', companyReview, 'jointly held: Wong Ka Ming')
// The drafts section: each lineage's current version, its status and link.
contains('company/records', companyReview, 'NAR1 — annual return')
contains('company/records', companyReview, 'badge draft-generated')
contains('company/records', companyReview, 'badge draft-invalidated')
contains('company/records', companyReview, 'v2')
contains('company/records', companyReview, '1 earlier version')
contains('company/records', companyReview, 'Wed, 30 Sept 2026')
contains(
  'company/records',
  companyReview,
  `href="${hrefForDraft(companyDrafts[0].company_id, companyDrafts[0].id)}"`,
)
contains('company/records', companyReview, 'Generate a draft…')

const companyNoRecords = renderToStaticMarkup(
  <CompanyView
    master={companyMasterData}
    batches={importBatches}
    batch={importBatch}
    selectedBatchId={importBatch.id}
    events={companyChangeEventsEmpty}
    officers={officersEmpty}
    shareholdings={shareholdingsEmpty}
    drafts={companyDraftsEmpty}
    onSelectBatch={() => undefined}
    onDecide={noop}
    onCreate={noop}
    onCreateOfficer={noop}
    onUpdateOfficer={noop}
    onDeleteOfficer={noop}
    onCreateShareholding={noop}
    onUpdateShareholding={noop}
    onDeleteShareholding={noop}
    onGenerateDraft={noop}
    onRefresh={() => undefined}
    auditReport={null}
    auditError={null}
    onOpenAudit={() => undefined}
    onRetryAudit={noop}
    onDownloadAudit={() => undefined}
  />,
)
contains('company/no-records', companyNoRecords, 'No officer is recorded for this company yet')
contains('company/no-records', companyNoRecords, 'No member position is recorded for this company yet')
contains('company/no-records', companyNoRecords, 'No draft exists for this company yet')

// --- The Audit tab (the §5.1 Audit row; slice A2) ---------------------------

// On the overview the tab bar is present and the report is not: the route
// reads it only once the Audit tab is opened.
contains('company/review', companyReview, 'role="tab"')
contains('company/review', companyReview, '>Audit</button>')
notContains('company/review', companyReview, 'Download JSON')

const auditPanel = renderToStaticMarkup(
  <CompanyAuditPanel
    report={auditReport}
    error={null}
    onRetry={noop}
    onDownload={() => undefined}
  />,
)
contains('audit/verified', auditPanel, 'Download JSON')
contains('audit/verified', auditPanel, 'Chain verified ✓ — 42 entries checked')
contains(
  'audit/verified',
  auditPanel,
  'verified over the tenant&#x27;s full audit chain',
)
contains('audit/verified', auditPanel, 'report version 1')
// The five areas render in the §2.13 order with their counts, empty ones
// saying so honestly.
contains('audit/verified', auditPanel, 'Client communications')
contains('audit/verified', auditPanel, 'Event lifecycle')
contains('audit/verified', auditPanel, 'Data changes')
contains('audit/verified', auditPanel, 'AI suggestions')
contains('audit/verified', auditPanel, 'Approvals')
// A row's columns: the localized occurred time's row, the actor ("system" for
// platform-written entries, the short id otherwise), the stored action, and
// the entity summary.
contains('audit/verified', auditPanel, 'system')
contains('audit/verified', auditPanel, '99999999')
contains('audit/verified', auditPanel, '<code>REMINDER_ENQUEUED</code>')
contains('audit/verified', auditPanel, 'change_event ce000000')
// Newest first: the later reminder precedes the earlier sent message even
// though the sent message sits earlier in the response's list.
check(
  'audit/verified: entries are newest-first',
  auditPanel.indexOf('REMINDER_ENQUEUED') !== -1 &&
    auditPanel.indexOf('REMINDER_ENQUEUED') < auditPanel.indexOf('MAIL_MESSAGE_SENT'),
)

const auditPanelProblems = renderToStaticMarkup(
  <CompanyAuditPanel
    report={auditReportChainProblems}
    error={null}
    onRetry={noop}
    onDownload={() => undefined}
  />,
)
contains('audit/problems', auditPanelProblems, 'Chain verification found problems')
contains('audit/problems', auditPanelProblems, 'entry 17: prev_hash mismatch')
notContains('audit/problems', auditPanelProblems, 'Chain verified ✓')
// An empty area says so honestly instead of rendering an empty table.
contains('audit/problems', auditPanelProblems, 'No entries in this area.')

const auditPanelError = renderToStaticMarkup(
  <CompanyAuditPanel report={null} error={new ApiError(403, 'forbidden')} onRetry={noop} onDownload={() => undefined} />,
)
contains('audit/error', auditPanelError, 'Not permitted: this account is inactive or has no access to this data.')
contains('audit/error', auditPanelError, 'Retry')

// The person and address groups the add forms open with — rendered directly,
// the entry-form precedent: a click is what SSR cannot run.
const personGroup = renderToStaticMarkup(
  <PersonFields legend="The person this appointment names" value={blankPerson()} onChange={() => undefined} />,
)
contains('person/fields', personGroup, 'The person this appointment names')
contains('person/fields', personGroup, 'Person type')
contains('person/fields', personGroup, 'Name (English)')
contains('person/fields', personGroup, 'Surname (English)')
contains('person/fields', personGroup, 'Previous names')
contains('person/fields', personGroup, 'one per line; English | Chinese')
contains('person/fields', personGroup, 'TCSP licence not required')
// The public-page partial identifiers are ordinary, offered fields…
contains('person/fields', personGroup, 'name="hkid_partial"')
contains('person/fields', personGroup, 'name="passport_partial"')
contains('person/fields', personGroup, 'name="passport_issuing_country_region"')
// …and the §1 protected class is not a control of this group at all.
for (const field of PROTECTED_CLASS_FIELDS) {
  notContains('person/fields', personGroup, `name="${field}"`)
}
contains('person/fields', personGroup, 'not stored, and the API refuses one by name')

const addressGroup = renderToStaticMarkup(
  <AddressFields
    legend="Correspondence address as filed"
    value={{ flat: '', building: '', street: '', district: '', country: '' }}
    onChange={() => undefined}
    note="A natural person files a correspondence address; a registered office address is not applicable to them."
  />,
)
contains('address/fields', addressGroup, 'Correspondence address as filed')
contains('address/fields', addressGroup, 'name="flat"')
contains('address/fields', addressGroup, 'leave blank for a Hong Kong address')
contains('address/fields', addressGroup, 'not applicable to them')

// The builders the panels' forms send through — called directly, because the
// forms only run on a click (the grammar-rule precedent above).
const blankPersonWrite = personWriteFrom(blankPerson())
check(
  'records: a blank person sends only the type and the (empty) name history',
  samePayload(blankPersonWrite, {
    person_type: 'natural_person',
    previous_names: [],
    aliases: [],
  }),
)
const filledPerson = personWriteFrom({
  ...blankPerson(),
  nameEn: 'Wong Ka Ming',
  nameZh: '  黃嘉明  ',
  previousNames: 'Wong Kar Ming | 黃家明\n\n',
  email: 'km.wong@example-tcsp.hk',
  hkidPartial: 'A123***',
  tcspNotRequired: true,
  tcspNotRequiredReason: 'the licence is held by the firm',
})
check(
  'records: a filled person sends its stated fields, trimmed, with the licence branch',
  samePayload(filledPerson, {
    person_type: 'natural_person',
    name_en: 'Wong Ka Ming',
    name_zh: '黃嘉明',
    previous_names: [{ name_en: 'Wong Kar Ming', name_zh: '黃家明' }],
    aliases: [],
    email: 'km.wong@example-tcsp.hk',
    hkid_partial: 'A123***',
    tcsp_licence: {
      licence_no: null,
      not_required: true,
      not_required_reason: 'the licence is held by the firm',
    },
  }),
)
check(
  'records: a licence number and the not-required branch are alternatives',
  personWriteFrom({ ...blankPerson(), tcspLicenceNo: 'TC-001234' }).tcsp_licence?.licence_no ===
    'TC-001234',
)
check(
  'records: a name-history line without a separator is an English-only pair',
  JSON.stringify(namePairsFrom('Wong Kar Ming\n')) ===
    JSON.stringify([{ name_en: 'Wong Kar Ming' }]),
)
check(
  'records: the capacity boxes are the role’s own, and a role change drops the rest',
  JSON.stringify(capacityChoices('company_secretary')) === JSON.stringify(['company_secretary']) &&
    appointmentWithRole(
      { ...blankAppointment(), capacities: ['company_secretary'] },
      'director',
    ).capacities.length === 0,
)
const secretaryCreate = appointmentCreateFrom(
  {
    ...blankAppointment(),
    role: 'company_secretary',
    capacities: ['company_secretary'],
    validFrom: '2021-01-04',
    correspondence: {
      flat: 'FLAT 12A',
      building: 'HARBOUR VIEW COURT',
      street: '88 CONNAUGHT ROAD WEST',
      district: 'SHEUNG WAN',
      country: '',
    },
    registeredOffice: { flat: 'X', building: 'Y', street: 'Z', district: 'D', country: '' },
  },
  { ...blankPerson(), nameEn: 'Wong Ka Ming' },
)
check(
  'records: a natural person files the correspondence address only',
  JSON.stringify(secretaryCreate) ===
    JSON.stringify({
      person: { person_type: 'natural_person', name_en: 'Wong Ka Ming', previous_names: [], aliases: [] },
      role: 'company_secretary',
      capacity: ['company_secretary'],
      valid_from: '2021-01-04',
      correspondence_address: {
        flat: 'FLAT 12A',
        building: 'HARBOUR VIEW COURT',
        street: '88 CONNAUGHT ROAD WEST',
        district: 'SHEUNG WAN',
      },
    }),
)
const alternateCreate = appointmentCreateFrom(
  {
    ...blankAppointment(),
    role: 'director',
    capacities: ['director', 'alternate_director'],
    alternateTo: 'Wong Ka Ming',
    validFrom: '2026-09-10',
    validTo: '2027-09-09',
  },
  { ...blankPerson(), nameEn: 'Chan Tai Man' },
)
check(
  'records: the alternate name travels exactly with the alternate box, and the window end rides along',
  alternateCreate.alternate_to === 'Wong Ka Ming' && alternateCreate.valid_to === '2027-09-09',
)
const discontinuedAlternate = appointmentChangesFrom({
  ...blankAppointment(),
  role: 'director',
  capacities: ['director'],
  alternateTo: 'Wong Ka Ming',
  validFrom: '2019-06-01',
})
check(
  'records: an edit with the alternate box off clears the name (explicit null, not a kept value)',
  discontinuedAlternate.alternate_to === null && discontinuedAlternate.valid_to === null,
)
check(
  'records: the address object omits a blank country and is null when nothing is stated',
  addressFrom({ flat: 'A', building: 'B', street: 'C', district: 'D', country: '' })?.country ===
    undefined &&
    addressFrom({ flat: '', building: '', street: '', district: '', country: '' }) === null &&
    addressLine({ flat: 'A', building: 'B', street: 'C', district: 'D' }) === 'A, B, C, D',
)
const memberCreate = shareholdingCreateFrom(
  {
    ...blankShareholding(),
    class_name: 'Ordinary',
    shares_held: '1500',
    percentage_held: '66.67',
    amount_paid: '1000.00',
    validFrom: '2010-03-01',
  },
  { ...blankPerson(), nameEn: 'Wong Ka Ming' },
)
check(
  'records: money and the percentage stay strings, the share count is a number',
  samePayload(memberCreate, {
    person: { person_type: 'natural_person', name_en: 'Wong Ka Ming', previous_names: [], aliases: [] },
    class_name: 'Ordinary',
    shares_held: 1500,
    percentage_held: '66.67',
    amount_paid: '1000.00',
    valid_from: '2010-03-01',
  }),
)
const memberEdit = shareholdingChangesFrom({
  ...blankShareholding(),
  class_name: 'Ordinary',
  shares_held: '1000',
  validFrom: '2010-03-01',
})
check(
  'records: an edit clears the blank optional fields with explicit nulls',
  memberEdit.remarks === null &&
    memberEdit.amount_unpaid === null &&
    memberEdit.percentage_held === null &&
    memberEdit.valid_to === null,
)
check(
  'records: the window marker is the API’s half-open rule',
  inOffice('2021-01-04', null, '2026-09-28') &&
    !inOffice('2019-06-01', '2026-08-31', '2026-09-28') &&
    !inOffice('2019-06-01', '2026-08-31', '2026-08-31') &&
    inOffice('2019-06-01', null, '2026-08-30'),
)
check(
  'records: the labels and the person name read as the panels show them',
  officerRoleLabel('reserve_director') === 'Reserve director' &&
    capacityText(['director', 'alternate_director']) ===
      'Director (董事), Alternate director (候補董事)' &&
    personTypeLabel('body_corporate') === 'Body corporate' &&
    personName(officers[0].person) === 'Wong Ka Ming · 黃嘉明' &&
    personName(officers[2].person) === 'PACIFIC CORPORATE SERVICES LIMITED',
)
check(
  'records: the appointment form reads its record back without changing it',
  JSON.stringify(appointmentChangesFrom(appointmentFormFrom(officers[1]))) ===
    JSON.stringify({
      role: 'director',
      capacity: ['director', 'alternate_director'],
      alternate_to: 'Wong Ka Ming',
      valid_from: '2019-06-01',
      valid_to: '2026-08-31',
      correspondence_address: {
        flat: 'UNIT 3',
        building: 'GOLDEN GATE BUILDING',
        street: '20 DES VOEUX ROAD CENTRAL',
        district: 'CENTRAL',
      },
      registered_office_address: null,
    }),
)

// --- The company's change-event list and the manual-entry form ------------

const companyWithEvents = renderToStaticMarkup(
  <CompanyView
    master={companyMasterData}
    batches={importBatches}
    batch={importBatch}
    selectedBatchId={importBatch.id}
    events={companyChangeEvents}
    officers={officers}
    shareholdings={shareholdings}
    drafts={companyDrafts}
    onSelectBatch={() => undefined}
    onDecide={noop}
    onCreate={noop}
    onCreateOfficer={noop}
    onUpdateOfficer={noop}
    onDeleteOfficer={noop}
    onCreateShareholding={noop}
    onUpdateShareholding={noop}
    onDeleteShareholding={noop}
    onGenerateDraft={noop}
    onRefresh={() => undefined}
    auditReport={null}
    auditError={null}
    onOpenAudit={() => undefined}
    onRetryAudit={noop}
    onDownloadAudit={() => undefined}
  />,
)
contains('company/events', companyWithEvents, 'Change events')
contains('company/events', companyWithEvents, 'Record a change event…')
contains('company/events', companyWithEvents, 'Director appointment')
contains('company/events', companyWithEvents, 'Secretary cessation')
contains('company/events', companyWithEvents, 'Registered office change')
contains('company/events', companyWithEvents, 'badge event-under_review')
contains('company/events', companyWithEvents, 'badge event-pending')
contains('company/events', companyWithEvents, 'badge event-confirmed')
contains('company/events', companyWithEvents, 'badge event-deleted')
// The current version and the lineage are both on the row.
contains('company/events', companyWithEvents, 'version 2')
contains('company/events', companyWithEvents, 'earlier: v1 · 18 Sept 2026')
contains('company/events', companyWithEvents, 'no earlier version')
// The window, in the API's own numbers.
contains('company/events', companyWithEvents, '5 days overdue')
contains('company/events', companyWithEvents, 'in 4 days')
contains('company/events', companyWithEvents, 'deadline Fri, 25 Sept 2026')
// The row's link opens the event's detail.
contains(
  'company/events',
  companyWithEvents,
  `href="${hrefForChangeEvent(companyChangeEvents[0].company_id, companyChangeEvents[0].id)}"`,
)

// The form itself, rendered directly (the company screen opens it by click —
// an interaction SSR does not run).
const formProps = {
  onSubmit: noop,
  onCancel: () => undefined,
}
const appointmentForm = renderToStaticMarkup(
  <ChangeEventForm
    {...formProps}
    legend="Record a change event"
    submitLabel="Record the change"
    initial={blankEventForm()}
  />,
)
contains('form/appointment', appointmentForm, 'Record a change event')
contains('form/appointment', appointmentForm, 'Director appointment')
contains('form/appointment', appointmentForm, 'value="company_name_change"')
contains('form/appointment', appointmentForm, 'reserved for Wave 2 (NNC2)')
contains('form/appointment', appointmentForm, 'Effective date')
contains('form/appointment', appointmentForm, 'Informed date')
contains('form/appointment', appointmentForm, 'Appointment date')
// The capacity group: the boxes a director's block prints, and the alternate
// name box — which appears only with the alternate box ticked.
contains('form/appointment', appointmentForm, 'name="capacity" value="director"')
contains('form/appointment', appointmentForm, 'name="capacity" value="alternate_director"')
notContains('form/appointment', appointmentForm, 'name="capacity" value="company_secretary"')
contains('form/appointment', appointmentForm, 'Alternate director (候補董事)')
check(
  'form/appointment: no alternate-to name box without the alternate capacity',
  elementHTML(appointmentForm, 'name="alternate_to"') === '',
)
// The public-page partial identifiers are ordinary, offered fields…
contains('form/appointment', appointmentForm, 'name="hkid_partial"')
contains('form/appointment', appointmentForm, 'name="passport_partial"')
contains('form/appointment', appointmentForm, 'name="passport_issuing_country_region"')
// …and the §1 protected class is not a control of this form at all.
for (const field of PROTECTED_CLASS_FIELDS) {
  notContains('form/appointment', appointmentForm, `name="${field}"`)
}

const alternateTicked = renderToStaticMarkup(
  <ChangeEventForm
    {...formProps}
    legend="Record a change event"
    submitLabel="Record the change"
    initial={{
      ...blankEventForm(),
      officer: { ...blankEventForm().officer, capacities: ['alternate_director'], alternateTo: '' },
    }}
  />,
)
const alternateInput = elementHTML(alternateTicked, 'name="alternate_to"')
check(
  'form/appointment/alternate: the alternate-to name box is required with the alternate tick',
  alternateInput.includes('required'),
)
contains('form/appointment/alternate', alternateTicked, 'required with the alternate-director box')

const secretaryForm = renderToStaticMarkup(
  <ChangeEventForm
    {...formProps}
    legend="Record a change event"
    submitLabel="Record the change"
    initial={formStateForType(blankEventForm(), 'secretary_appointment')}
  />,
)
contains('form/secretary', secretaryForm, 'Company secretary (公司秘書)')
contains('form/secretary', secretaryForm, 'name="capacity" value="company_secretary"')
notContains('form/secretary', secretaryForm, 'name="capacity" value="alternate_director"')
notContains('form/secretary', secretaryForm, 'name="capacity" value="director"')

const cessationForm = renderToStaticMarkup(
  <ChangeEventForm
    {...formProps}
    legend="Record a change event"
    submitLabel="Record the change"
    initial={formStateForType(blankEventForm(), 'director_cessation')}
  />,
)
contains('form/cessation', cessationForm, 'Cessation date')
contains('form/cessation', cessationForm, 'Cessation reason')
contains('form/cessation', cessationForm, 'Resignation or other (non-death)')
contains('form/cessation', cessationForm, 'Deceased')
notContains('form/cessation', cessationForm, 'Appointment date')

const officeForm = renderToStaticMarkup(
  <ChangeEventForm
    {...formProps}
    legend="Record a change event"
    submitLabel="Record the change"
    initial={formStateForType(blankEventForm(), 'registered_office_change')}
  />,
)
contains('form/office', officeForm, 'name="flat"')
contains('form/office', officeForm, 'name="building"')
contains('form/office', officeForm, 'name="street"')
contains('form/office', officeForm, 'name="district"')
contains('form/office', officeForm, 'name="country"')
notContains('form/office', officeForm, 'name="capacity"')

const otherForm = renderToStaticMarkup(
  <ChangeEventForm
    {...formProps}
    legend="Record a change event"
    submitLabel="Record the change"
    initial={formStateForType(blankEventForm(), 'other')}
  />,
)
contains('form/other', otherForm, 'name="summary"')
contains('form/other', otherForm, 'One line of staff')
notContains('form/other', otherForm, 'name="capacity"')

// The edit form reads the current version back: every value it offers is the
// version's own, and the payload parts it does not offer stay on the record.
const editFormState = formStateFromChangeEvent(companyChangeEvents[0])
const editForm = renderToStaticMarkup(
  <ChangeEventForm
    {...formProps}
    legend="Edit this event"
    submitLabel="Save the new version"
    note="Saving appends a new version — the current one becomes history."
    initial={editFormState}
  />,
)
contains('form/edit', editForm, 'value="Wong Ka Ming"')
contains('form/edit', editForm, 'value="黃嘉明"')
contains('form/edit', editForm, 'value="A123***"')
contains('form/edit', editForm, 'value="2026-09-10"')
contains('form/edit', editForm, 'value="10 September 2026"')
contains('form/edit', editForm, 'value="2026-09-12"')
contains('form/edit', editForm, 'Saving appends a new version')

// The grammar's rules, called directly: what a form's state becomes as a
// payload, and what an edit sends — only what changed, with the payload parts
// the form does not offer carried through from the version.
const appointmentState: EventFormState = {
  ...blankEventForm(),
  effectiveDate: '2026-09-10',
  effectiveDateText: '10 September 2026',
  informedDate: '2026-09-12',
  officer: {
    ...blankEventForm().officer,
    nameEn: 'Wong Ka Ming',
    nameZh: '黃嘉明',
    capacities: ['director', 'alternate_director'],
    alternateTo: 'Chan Tai Man',
    appointmentDate: '2026-09-10',
    hkidPartial: 'A123***',
  },
}
check(
  'grammar: the appointment payload is the officer block the registry prints',
  samePayload(buildPayload(appointmentState), {
    appointment: {
      officer: {
        role: 'director',
        capacity: ['director', 'alternate_director'],
        alternate_to: 'Chan Tai Man',
        name_zh: '黃嘉明',
        name_en: 'Wong Ka Ming',
        hkid_partial: 'A123***',
        appointment_date: '2026-09-10',
      },
    },
  }),
)
const noAlternate = {
  ...appointmentState,
  officer: { ...appointmentState.officer, capacities: ['director'] as OfficerCapacity[] },
}
check(
  'grammar: the alternate-to name travels exactly with the alternate capacity',
  !('alternate_to' in (buildPayload(noAlternate).appointment as { officer: object }).officer),
)
check(
  'grammar: the entry body carries the dates and the built payload',
  samePayload(createRequestFrom(appointmentState), {
    type: 'director_appointment',
    effective_date: '2026-09-10',
    effective_date_text: '10 September 2026',
    informed_date: '2026-09-12',
    payload: buildPayload(appointmentState),
  }),
)
check(
  'grammar: the role is the type’s own',
  JSON.stringify(rolesForType('secretary_appointment')) === JSON.stringify(['company_secretary']) &&
    JSON.stringify(rolesForType('director_cessation')) ===
      JSON.stringify(['director', 'reserve_director']),
)
check(
  'grammar: the capacity boxes are the office’s own',
  JSON.stringify(capacitiesForRole('company_secretary')) === JSON.stringify(['company_secretary']) &&
    capacitiesForRole('director').includes('alternate_director') &&
    capacitiesForRole('reserve_director').includes('alternate_director'),
)
check(
  'grammar: the form cannot offer a §1 protected-class field',
  PROTECTED_CLASS_FIELDS.every((field) => !OFFERED_OFFICER_KEYS.has(field)),
)
check(
  'labels: the six enterable types exclude the reserved one',
  ENTERABLE_CHANGE_EVENT_TYPES.length === 6 &&
    !ENTERABLE_CHANGE_EVENT_TYPES.includes('company_name_change'),
)
check(
  'labels: the lifecycle speaks in the product’s words',
  changeEventTypeLabel('registered_office_change') === 'Registered office change' &&
    changeEventStatusLabel('under_review') === 'Under review' &&
    changeEventStatusLabel('applied_to_draft') === 'Applied to a draft' &&
    changeEventActionLabel('mark_under_review') === 'Start review' &&
    changeEventSourceLabel('manual') === 'Entered by staff',
)
check(
  'actions: the ones that open a form or a confirmation are marked',
  actionOpensForm('reject') &&
    actionOpensForm('client_withdraw') &&
    actionOpensForm('edit') &&
    actionOpensForm('delete') &&
    !actionOpensForm('confirm'),
)
check(
  'actions: an unwired action carries no note (the API never offers one)',
  changeEventActionNote('apply') === null && (changeEventActionNote('reject') ?? '').includes('reason'),
)

const readBack = formStateFromChangeEvent(companyChangeEvents[0])
check(
  'edit: reading a version back into the form changes nothing',
  Object.keys(editChangesFrom(companyChangeEvents[0], readBack)).length === 0,
)
const revisedState: EventFormState = {
  ...readBack,
  officer: { ...readBack.officer, nameEn: 'Wong Ka Ming (revised)' },
}
const revisedChanges = editChangesFrom(companyChangeEvents[0], revisedState)
check(
  'edit: only the payload travels when only the payload changed, and the unoffered parts survive',
  JSON.stringify(Object.keys(revisedChanges)) === JSON.stringify(['payload']) &&
    samePayload(revisedChanges.payload, {
      appointment: {
        officer: {
          role: 'director',
          capacity: ['director'],
          name_en: 'Wong Ka Ming (revised)',
          name_zh: '黃嘉明',
          hkid_partial: 'A123***',
          appointment_date: '2026-09-10',
          previous_names: [{ name_en: 'Wong Kar Ming' }],
        },
      },
    }),
)
check(
  'edit: a date-only change sends the date and nothing else',
  JSON.stringify(
    Object.keys(editChangesFrom(companyChangeEvents[0], { ...readBack, effectiveDate: '2026-09-11' })),
  ) === JSON.stringify(['effective_date']),
)
check(
  'edit: clearing an optional date sends an explicit null (clear, not keep)',
  editChangesFrom(companyChangeEvents[0], { ...readBack, informedDate: '' }).informed_date === null,
)

const appointmentRows = payloadRows(companyChangeEvents[0].payload)
check(
  'payload rows: the registry paths are labelled and values translated',
  appointmentRows.some(
    (row) =>
      row.path === 'payload.appointment.officer.role' &&
      row.label === 'Role' &&
      row.value === 'Director',
  ) &&
    appointmentRows.some(
      (row) =>
        row.path === 'payload.appointment.officer.capacity' &&
        row.label === 'Capacity (registry boxes)' &&
        row.value === 'Director (董事)',
    ) &&
    appointmentRows.some(
      (row) => row.path === 'payload.appointment.officer.name_en' && row.value === 'Wong Ka Ming',
    ) &&
    appointmentRows.some(
      (row) =>
        row.path === 'payload.appointment.officer.previous_names[0].name_en' &&
        row.value === 'Wong Kar Ming',
    ),
)

check(
  'window: an approaching window reads as its deadline and days',
  describeChangeWindow(companyChangeEvents[0].change_window).includes('Fri, 25 Sept 2026') &&
    describeChangeWindow(companyChangeEvents[0].change_window).includes('in 4 days'),
)
check(
  'window: the deadline day itself is not late',
  describeChangeWindow(changeEventQueue[1].change_window) ===
    'The 15-day window ends today (Mon, 21 Sept 2026).',
)
check(
  'window: a passed window reads as late',
  describeChangeWindow(changeEventQueue[0].change_window).includes('Wed, 16 Sept 2026') &&
    describeChangeWindow(changeEventQueue[0].change_window).includes('5 days past it'),
)
check(
  'samePayload: key order does not matter, value does',
  samePayload({ a: 1, b: { c: [1, 2] } }, { b: { c: [1, 2] }, a: 1 }) &&
    !samePayload({ a: 1 }, { a: 2 }),
)

// --- The change-event review queue ---------------------------------------

const queue = renderToStaticMarkup(
  <ChangeEventsQueueView items={changeEventQueue} onRefresh={() => undefined} />,
)
contains('queue/populated', queue, 'Change events awaiting review')
contains('queue/populated', queue, '3 events awaiting review')
contains('queue/populated', queue, 'Alpha Traders Limited')
contains('queue/populated', queue, 'Beta Holdings Limited')
contains('queue/populated', queue, 'BRN 11112222')
contains('queue/populated', queue, 'Secretary cessation')
contains('queue/populated', queue, 'Director appointment')
contains('queue/populated', queue, 'badge event-pending')
contains('queue/populated', queue, 'badge event-under_review')
contains('queue/populated', queue, '5 days overdue')
contains('queue/populated', queue, 'due today')
contains('queue/populated', queue, 'in 4 days')
contains('queue/populated', queue, 'risk-red')
contains('queue/populated', queue, '7-day alert')
// All three rows are inside the 7-day band (4 days remaining is red — the
// boundary is inclusive), so the queue renders no amber badge.
notContains('queue/populated', queue, 'risk-amber')
// The company link goes to the company screen; the row's own link to the
// event's detail, which is where the allowed actions live.
contains('queue/populated', queue, `href="${hrefForCompany(changeEventQueue[0].company_id)}"`)
contains(
  'queue/populated',
  queue,
  `href="${hrefForChangeEvent(changeEventQueue[0].company_id, changeEventQueue[0].id)}"`,
)
// The API's order is the order: soonest statutory deadline first.
check(
  'queue: the rows keep the API’s deadline-ascending order',
  queue.indexOf(hrefForChangeEvent(changeEventQueue[0].company_id, changeEventQueue[0].id)) <
    queue.indexOf(hrefForChangeEvent(changeEventQueue[1].company_id, changeEventQueue[1].id)) &&
    queue.indexOf(hrefForChangeEvent(changeEventQueue[1].company_id, changeEventQueue[1].id)) <
      queue.indexOf(hrefForChangeEvent(changeEventQueue[2].company_id, changeEventQueue[2].id)),
)

const queueEmpty = renderToStaticMarkup(<ChangeEventsQueueView items={changeEventQueueEmpty} />)
contains('queue/empty', queueEmpty, 'No change event is awaiting staff review')
contains('queue/empty', queueEmpty, 'scoped to the companies this account can see')
notContains('queue/empty', queueEmpty, '<table')

// --- The event detail -----------------------------------------------------

const detail = renderToStaticMarkup(
  <ChangeEventDetailView
    companyName="Alpha Traders Limited"
    brNumber="11112222"
    event={companyChangeEvents[0]}
    transitions={changeEventTransitions}
    assessments={changeEventAssessments}
    onAct={noop}
    onRefresh={() => undefined}
  />,
)
contains('detail', detail, 'Director appointment')
contains('detail', detail, 'badge event-under_review')
contains('detail', detail, 'Alpha Traders Limited')
contains('detail', detail, 'BRN 11112222')
contains('detail', detail, 'version 2')
contains('detail', detail, 'recorded 20 Sept 2026')
contains('detail', detail, 'Entered by staff')
contains('detail', detail, 'staff 99999999')
// The current version's dates, and the source text the date was read from.
contains('detail', detail, 'Effective date')
contains('detail', detail, 'as stated: “10 September 2026”')
contains('detail', detail, 'Sat, 12 Sept 2026')
contains('detail', detail, 'Not reviewed yet')
// The statutory window, said in the API's own numbers.
contains('detail', detail, 'The statutory 15-day window')
contains('detail', detail, 'Fri, 25 Sept 2026')
contains('detail', detail, 'in 4 days')
contains('detail', detail, 'risk-red')
contains('detail', detail, '7-day alert')
// The payload, path-labelled — including the part the entry form does not
// offer, which the read still shows.
contains('detail', detail, 'payload.appointment.officer.name_en')
contains('detail', detail, 'Name (English)')
contains('detail', detail, 'Wong Ka Ming')
contains('detail', detail, 'Capacity (registry boxes)')
contains('detail', detail, 'Director (董事)')
contains('detail', detail, 'A123***')
contains('detail', detail, '1. Name (English)')
contains('detail', detail, 'Wong Kar Ming')
// The allowed actions, in the transitions read's own order, and only them.
contains('detail', detail, 'What you may do now')
contains('detail', detail, 'Mark reviewed')
contains('detail', detail, 'Revert')
contains('detail', detail, 'Reject…')
contains('detail', detail, 'Edit…')
contains('detail', detail, 'Delete…')
contains('detail', detail, 'Record client withdrawal…')
check(
  'detail: the actions render in the read’s order',
  detail.indexOf('Mark reviewed') < detail.indexOf('Revert') &&
    detail.indexOf('Revert') < detail.indexOf('Reject…'),
)
// Unwired rows are never offered by the read, so no button exists for them.
notContains('detail', detail, 'Apply to the draft')
notContains('detail', detail, 'Send for reconfirmation')
notContains('detail', detail, 'Supersede')
// The version history: the current version marked, the earlier one listed.
contains('detail', detail, 'Version history')
contains('detail', detail, 'v2')
contains('detail', detail, '(current)')
contains('detail', detail, 'v1')
contains('detail', detail, 'Pending review')
contains('detail', detail, '18 Sept 2026')
contains('detail', detail, 'Tue, 08 Sept 2026')
// The assessments read is truthfully empty, and the screen says why.
contains('detail', detail, 'No extraction assessments')
contains('detail', detail, 'Nothing is estimated here')
// The links back to both the queue and the company.
contains('detail', detail, 'href="#/change-events"')
contains('detail', detail, `href="${hrefForCompany(companyChangeEvents[0].company_id)}"`)

// A terminal event: the read offers nothing, and the screen says that.
const terminalDetail = renderToStaticMarkup(
  <ChangeEventDetailView
    companyName="Alpha Traders Limited"
    brNumber="11112222"
    event={companyChangeEvents[3]}
    transitions={changeEventTransitionsEmpty}
    assessments={changeEventAssessments}
    onAct={noop}
    onRefresh={() => undefined}
  />,
)
contains('terminal-detail', terminalDetail, 'badge event-deleted')
contains('terminal-detail', terminalDetail, 'No action is available to this account')
contains('terminal-detail', terminalDetail, 'Client called on the 14th to withdraw the change.')
notContains('terminal-detail', terminalDetail, 'Mark reviewed')
// The confirmed event: the approval stamps are shown, and the address payload
// reads as its labelled leaves.
const confirmedDetail = renderToStaticMarkup(
  <ChangeEventDetailView
    companyName="Alpha Traders Limited"
    brNumber="11112222"
    event={companyChangeEvents[2]}
    transitions={{
      event_id: companyChangeEvents[2].id,
      company_id: companyChangeEvents[2].company_id,
      version_no: 1,
      status: 'confirmed',
      actions: [
        { action: 'revert', to_status: 'reviewed', rationale_required: false },
        { action: 'edit', to_status: 'confirmed', rationale_required: false },
        { action: 'delete', to_status: 'deleted', rationale_required: false },
        { action: 'client_withdraw', to_status: 'withdrawn', rationale_required: true },
      ],
    }}
    assessments={changeEventAssessments}
    onAct={noop}
    onRefresh={() => undefined}
  />,
)
contains('confirmed-detail', confirmedDetail, 'badge event-confirmed')
contains('confirmed-detail', confirmedDetail, 'Reviewed by staff 99999999')
contains('confirmed-detail', confirmedDetail, 'Approved by staff 99999999')
contains('confirmed-detail', confirmedDetail, 'Registered office — Building')
contains('confirmed-detail', confirmedDetail, 'HARBOUR BUILDING')
contains('confirmed-detail', confirmedDetail, 'an action that is not here is not offered')
contains('confirmed-detail', confirmedDetail, 'an action that is not here is not offered')

// The review queue's reconfirmation button (M5b task 5's wiring), verified to
// arrive from the transitions read alone: the view renders whatever the read
// offers with no component change — `send_reconfirmation` carries its label
// already, needs no form and no rationale (the read says so), and posts the
// bare action. This render is the proof by construction.
const pendingCessation = companyChangeEvents[1]
const reconfirmationTransitions: ChangeEventTransitions = {
  event_id: pendingCessation.id,
  company_id: pendingCessation.company_id,
  version_no: pendingCessation.version_no,
  status: 'pending',
  actions: [
    { action: 'send_reconfirmation', to_status: 'client_reconfirming', rationale_required: false },
  ],
}
const reconfirmationDetail = renderToStaticMarkup(
  <ChangeEventDetailView
    companyName="Alpha Traders Limited"
    brNumber="11112222"
    event={pendingCessation}
    transitions={reconfirmationTransitions}
    assessments={changeEventAssessments}
    onAct={noop}
    onRefresh={() => undefined}
  />,
)
contains('reconfirmation-detail', reconfirmationDetail, 'badge event-pending')
contains('reconfirmation-detail', reconfirmationDetail, '>Send for reconfirmation</button>')
// No input is collected first: no ellipsis (the read offers it rationale-free)
// and no rationale form opens for it.
notContains('reconfirmation-detail', reconfirmationDetail, 'Send for reconfirmation…')
notContains('reconfirmation-detail', reconfirmationDetail, 'Reason — required')
// The action has no note of its own yet: the honest fallback, not silence.
contains(
  'reconfirmation-detail',
  reconfirmationDetail,
  'the API lists this action for this account now',
)

// The non-empty assessments branch (a future parser's rows) renders as what it
// is; the empty state above is what this build's read returns.
const syntheticAssessment: ExtractionAssessment = {
  id: 'f6000000-0000-4000-8000-000000000001',
  subject_type: 'change_event',
  subject_id: companyChangeEvents[0].id,
  component: 'reply_parser',
  model: 'reply-parse/v1',
  field_scores: { 'appointment.officer.name_en': 0.9 },
  signal_log: { fields: { 'appointment.officer.name_en': ['exact_field_value'] } },
  overall_score: 0.9,
  routing: 'auto_accept',
  created_at: '2026-09-20T09:00:00Z',
}
const assessedDetail = renderToStaticMarkup(
  <ChangeEventDetailView
    companyName="Alpha Traders Limited"
    brNumber="11112222"
    event={companyChangeEvents[0]}
    transitions={changeEventTransitions}
    assessments={[syntheticAssessment]}
    onAct={noop}
    onRefresh={() => undefined}
  />,
)
contains('assessed-detail', assessedDetail, 'reply_parser')
contains('assessed-detail', assessedDetail, 'reply-parse/v1')
contains('assessed-detail', assessedDetail, 'Auto-accept band')
notContains('assessed-detail', assessedDetail, 'No extraction assessments')

// --- The draft detail (slice F3) ------------------------------------------

const draftHtml = renderToStaticMarkup(
  <DraftDetailView
    draft={draftDetail}
    transitions={draftTransitions}
    onAct={noop}
    onUpload={noop}
    onExport={async () => filingPackage}
    onRefresh={() => undefined}
  />,
)
// The header: form, version, status, target date and the ADR-0010 pin.
contains('draft/detail', draftHtml, 'NAR1 — annual return')
contains('draft/detail', draftHtml, 'badge draft-generated')
contains('draft/detail', draftHtml, 'version 2')
contains('draft/detail', draftHtml, 'Wed, 30 Sept 2026')
contains('draft/detail', draftHtml, 'specification 2/2025')
contains('draft/detail', draftHtml, 'Alpha Traders Limited')
contains('draft/detail', draftHtml, '1 linked (a1a1a1a1)')
contains('draft/detail', draftHtml, 'Not sent to the client')
// The boundary this screen states rather than hides.
contains('draft/detail', draftHtml, 'The values below are not edited')
contains('draft/detail', draftHtml, 'picked up by regenerating')
// The QC flags, each with the domain's own words.
contains('draft/detail', draftHtml, 'QC flags')
contains('draft/detail', draftHtml, 'Change against the prior filing no event explains')
contains('draft/detail', draftHtml, 'company.member_count')
contains('draft/detail', draftHtml, '1 -&gt; 2')
contains('draft/detail', draftHtml, 'Approved event that could not be applied')
contains('draft/detail', draftHtml, 'no in-office officer record matches')
contains('draft/detail', draftHtml, 'Critical field with no source')
contains('draft/detail', draftHtml, 'filing_metadata.return.made_up_date')
// The resolved values, structurally.
contains('draft/detail', draftHtml, 'Resolved values')
contains('draft/detail', draftHtml, 'FLAT 99')
contains('draft/detail', draftHtml, 'company.registered_office.building')
contains('draft/detail', draftHtml, 'Share capital')
contains('draft/detail', draftHtml, 'Ordinary')
contains('draft/detail', draftHtml, 'Total (derived, never stored)')
contains('draft/detail', draftHtml, 'Chan Tai Man')
contains('draft/detail', draftHtml, 'Change event a1a1a1a1')
contains('draft/detail', draftHtml, 'Wong Ka Ming')
contains('draft/detail', draftHtml, 'The subscriber’s holding.')
// The applied change, with its payload as the registry paths name it.
contains('draft/detail', draftHtml, 'Applied changes')
contains('draft/detail', draftHtml, 'Director appointment')
contains('draft/detail', draftHtml, 'Thu, 10 Sept 2026')
contains('draft/detail', draftHtml, 'payload.appointment.officer.name_en')
// The provenance, with the changed entry marked and its source trace shown.
contains('draft/detail', draftHtml, 'Provenance and change highlighting')
contains('draft/detail', draftHtml, '<code>12A.name</code>')
contains('draft/detail', draftHtml, '<code>HDR-BRN</code>')
contains('draft/detail', draftHtml, 'Master record aaaaaaaa')
contains('draft/detail', draftHtml, 'Change event a1a1a1a1')
contains('draft/detail', draftHtml, 'No source resolved')
const changedRowStart = draftHtml.indexOf('<tr class="changed">')
const changedRow = draftHtml.slice(changedRowStart, draftHtml.indexOf('</tr>', changedRowStart))
check(
  'draft/detail: the first changed row is the event-sourced one, marked',
  changedRow.includes('12A.name') &&
    changedRow.includes('Change event a1a1a1a1') &&
    changedRow.includes('Changed'),
)
check(
  'draft/detail: exactly the changed entries are marked, and the unchanged ones are not',
  (draftHtml.match(/<tr class="changed">/g) ?? []).length === 2 &&
    provenanceRows(draftDetail.field_provenance).filter((row) => row.changed).length === 2 &&
    draftHtml.includes('<tr class="changed">') &&
    !draftHtml.includes('<tr class="changed"><td><code>HDR-BRN</code>'),
)
contains('draft/detail', draftHtml, 'badge changed">Changed')
// The filing metadata, as the version holds it.
contains('draft/detail', draftHtml, 'Filing metadata')
contains('draft/detail', draftHtml, 'Financial period — start')
contains('draft/detail', draftHtml, '2025-04-01')
contains('draft/detail', draftHtml, 'Schedule 2')
contains('draft/detail', draftHtml, 'K2P Corporate Services Limited')
contains('draft/detail', draftHtml, 'NAR1-2026-ALPHA')
// The actions, strictly from the transitions read — and only them.
contains('draft/detail', draftHtml, 'What you may do now')
contains('draft/detail', draftHtml, 'Edit the filing metadata')
contains('draft/detail', draftHtml, 'Start review')
contains('draft/detail', draftHtml, 'the backend is the sole authority on allowed actions')
notContains('draft/detail', draftHtml, '>Send for signature</button>')
notContains('draft/detail', draftHtml, '>Approve</button>')
notContains('draft/detail', draftHtml, '>Export</button>')
notContains('draft/detail', draftHtml, '>Regenerate</button>')
notContains('draft/detail', draftHtml, '>Invalidate</button>')
// The history, the current version first.
contains('draft/detail', draftHtml, 'Version history')
contains('draft/detail', draftHtml, '(current)')
contains('draft/detail', draftHtml, 'v1')
contains('draft/detail', draftHtml, 'badge draft-invalidated">Invalidated')

// The transitions read is the authority: an offer list without `edit` renders
// no edit affordance at all, whatever the draft's status would allow.
const reviewOnly: DraftTransitions = {
  ...draftTransitions,
  actions: [{ action: 'mark_under_review', to_status: 'under_review', rationale_required: false }],
}
const reviewOnlyHtml = renderToStaticMarkup(
  <DraftDetailView
    draft={draftDetail}
    transitions={reviewOnly}
    onAct={noop}
    onUpload={noop}
    onExport={async () => filingPackage}
    onRefresh={() => undefined}
  />,
)
contains('draft/offers', reviewOnlyHtml, 'Start review')
notContains('draft/offers', reviewOnlyHtml, 'Edit the filing metadata')

// The empty states: a draft whose collections all resolved nothing, and whose
// status admits no action.
const minimalHtml = renderToStaticMarkup(
  <DraftDetailView
    draft={draftDetailMinimal}
    transitions={draftTransitionsEmpty}
    onAct={noop}
    onUpload={noop}
    onExport={async () => filingPackage}
    onRefresh={() => undefined}
  />,
)
contains('draft/minimal', minimalHtml, 'No officer resolved')
contains('draft/minimal', minimalHtml, 'No member resolved')
contains('draft/minimal', minimalHtml, 'No change event entered this draft')
contains('draft/minimal', minimalHtml, 'The generation raised no QC flags.')
contains('draft/minimal', minimalHtml, 'No provenance is on this version.')
contains('draft/minimal', minimalHtml, 'Nothing is stated on the document yet')
contains('draft/minimal', minimalHtml, 'No action is available to this account')
contains('draft/minimal', minimalHtml, 'Invalidated')
contains('draft/minimal', minimalHtml, 'ND2A — change of secretary/director')
// One version only: the history section is not rendered as an empty table.
notContains('draft/minimal', minimalHtml, 'Version history')
check(
  'draft/minimal: an invalidated version carries its moment and reason',
  minimalHtml.includes('change event b2b2b2b2 revert'),
)

// The release's package panel (slice F4b). SSR cannot press the button that
// reveals it, so the pure component is rendered directly with the fixture —
// the filter-rule precedent — and the release's own action note is read too.
const packageHtml = renderToStaticMarkup(
  <SignaturePackagePanel package={draftSignaturePackage} />,
)
contains('draft/package', packageHtml, 'The signature-ready package')
contains('draft/package', packageHtml, 'd4d4d4d4-d4d4-4d44-8d44-d4d4d4d4d4d4')
contains('draft/package', packageHtml, 'd4d4d4d')
contains('draft/package', packageHtml, 'A box the draft does not state')
contains('draft/package', packageHtml, 'missing_value')
contains('draft/package', packageHtml, 'filing_metadata.presentor: not stated')
contains('draft/package', packageHtml, 'Protected information, by hand')
contains('draft/package', packageHtml, 'protected_class_manual')
contains('draft/package', packageHtml, 'complete fill_5_P.7 by hand')
contains('draft/package', packageHtml, '/documents/')
check(
    'draft/package: an empty list says so rather than showing a table',
    renderToStaticMarkup(
      <SignaturePackagePanel
        package={{ document_id: draftSignaturePackage.document_id, escalations: [] }}
      />,
    ).includes('nothing is left for a human here'),
)
check(
    'draft/actions: every wired action carries its own note',
    draftActionNote('send_for_signature')?.includes('stores the PDF as a WORM document') === true &&
      (draftActionNote('upload_signed') ?? '').includes('signature received') &&
      (draftActionNote('approve') ?? '').includes('item-16') &&
      (draftActionNote('export') ?? '').includes('WORM documents') &&
      (draftActionNote('regenerate') ?? '').includes('generation seam') &&
      signaturePackageEscalationLabel('member_overflow') === 'A member beyond Schedule 1' &&
      signaturePackageEscalationLabel('something_new') === 'something_new',
)

// The export's package panel (P6-5): rendered directly with the fixture, the
// SignaturePackagePanel precedent — the export's reply is held by the screen
// that sent it, and SSR cannot press the chooser's button.
const exportPanelHtml = renderToStaticMarkup(<FilingPackagePanel package={filingPackage} />)
contains('draft/export-panel', exportPanelHtml, 'The filing package')
contains('draft/export-panel', exportPanelHtml, 'Paper lodgement')
contains('draft/export-panel', exportPanelHtml, filingPackage.document_id)
contains('draft/export-panel', exportPanelHtml, filingPackage.envelope.checklist_document_id as string)
contains('draft/export-panel', exportPanelHtml, '1 1, 2 2')
contains('draft/export-panel', exportPanelHtml, '2 copies')
contains('draft/export-panel', exportPanelHtml, 'Footer page numbers')
// The paper mode carries no e-Registry helper: its id has no row.
notContains('draft/export-panel', exportPanelHtml, 'e-Registry helper')
check(
  'draft/export-panel: an api-ready envelope carries no companion rows',
  (() => {
    const rows = filingPackageEnvelopeRows({
      sheet_counts: { main: 1 },
      page_pairs: [],
      helper_document_id: null,
      checklist_document_id: null,
    })
    return (
      rows.length === 2 &&
      rows[0].label === 'Sheets — main' &&
      rows[0].value === '1 copy' &&
      rows[1].label === 'Footer page numbers' &&
      rows[1].value === 'none printed'
    )
  })(),
)
check(
  'draft/export: the mode vocabulary is the contract’s three values, labelled and described',
  JSON.stringify(EXPORT_MODES) ===
    JSON.stringify(['e_registry_prep', 'paper', 'api_ready']) &&
    exportModeLabel('e_registry_prep') === 'e-Registry prep' &&
    (exportModeDescription('paper') ?? '').includes('fee schedule') &&
    (exportModeDescription('api_ready') ?? '').includes('deterministic JSON') &&
    exportModeDescription('something_new') === null,
)

// The three new input-carrying actions, verified to arrive from the
// transitions read alone (the reconfirmation precedent): the offered rows
// render buttons with the ellipsis that opens their form, and only them.
const signatureTransitions: DraftTransitions = {
  ...draftTransitions,
  status: 'sent_for_signature',
  actions: [
    { action: 'upload_signed', to_status: 'signature_received', rationale_required: false },
    { action: 'recall', to_status: 'under_review', rationale_required: false },
  ],
}
const signatureHtml = renderToStaticMarkup(
  <DraftDetailView
    draft={{ ...draftDetail, status: 'sent_for_signature', signature_status: 'sent_for_signature' }}
    transitions={signatureTransitions}
    onAct={noop}
    onUpload={noop}
    onExport={async () => filingPackage}
    onRefresh={() => undefined}
  />,
)
contains('draft/signature', signatureHtml, '>Upload the signed copy…</button>')
contains('draft/signature', signatureHtml, '>Recall</button>')
// The picker itself opens on the press — SSR renders the closed state only.
notContains('draft/signature', signatureHtml, 'The client-signed PDF')

const approvalTransitions: DraftTransitions = {
  ...draftTransitions,
  status: 'reviewed',
  actions: [
    { action: 'approve', to_status: 'approved', rationale_required: false },
    { action: 'reject', to_status: 'rejected', rationale_required: true },
  ],
}
// The NAR1 fixture makes the statement unconfirmed: approve collects the
// confirmation (the ellipsis), and the plain press would be wrong.
const approvalHtml = renderToStaticMarkup(
  <DraftDetailView
    draft={{ ...draftDetail, status: 'reviewed' }}
    transitions={approvalTransitions}
    onAct={noop}
    onUpload={noop}
    onExport={async () => filingPackage}
    onRefresh={() => undefined}
  />,
)
contains('draft/approval', approvalHtml, '>Approve…</button>')
contains('draft/approval', approvalHtml, '>Reject…</button>')
notContains('draft/approval', approvalHtml, '>Approve</button>')
check(
  'draft/approval: the confirmation gate reads the draft’s own two fields',
  statementConfirmationPending(draftDetail) === true &&
    statementConfirmationPending({ ...draftDetail, statement_confirmed_by: draftDetail.created_by ?? 'someone' }) ===
      false &&
    statementConfirmationPending({ ...draftDetail, private_company_statement: null }) === false &&
    statementConfirmationPending(draftDetailMinimal) === false,
)
// Confirmed already: approve is a plain press again.
const confirmedHtml = renderToStaticMarkup(
  <DraftDetailView
    draft={{
      ...draftDetail,
      status: 'reviewed',
      statement_confirmed_by: '99999999-9999-4999-8999-999999999999',
    }}
    transitions={approvalTransitions}
    onAct={noop}
    onUpload={noop}
    onExport={async () => filingPackage}
    onRefresh={() => undefined}
  />,
)
contains('draft/approval-confirmed', confirmedHtml, '>Approve</button>')
notContains('draft/approval-confirmed', confirmedHtml, '>Approve…</button>')

const exportTransitions: DraftTransitions = {
  ...draftTransitions,
  status: 'approved',
  actions: [{ action: 'export', to_status: 'exported', rationale_required: false }],
}
const exportHtml = renderToStaticMarkup(
  <DraftDetailView
    draft={{ ...draftDetail, status: 'approved' }}
    transitions={exportTransitions}
    onAct={noop}
    onUpload={noop}
    onExport={async () => filingPackage}
    onRefresh={() => undefined}
  />,
)
contains('draft/export', exportHtml, '>Export…</button>')
contains('draft/export', exportHtml, 'assembles the filing package in the chosen mode')

// The readers, called directly (the payload-rows precedent).
const provenance = provenanceRows(draftDetail.field_provenance)
const changedEntry = provenance.find((row) => row.item === '12A.name')
const noSourceEntry = provenance.find((row) => row.item === 'HDR-DATE')
check(
  'draft/readers: the provenance reads every item, marking change and no-source',
  provenance.length === 7 &&
    provenance[0].item === '1' &&
    changedEntry !== undefined &&
    changedEntry.changed &&
    changedEntry.sourceType === 'change_event_id' &&
    changedEntry.sourceId === 'a1a1a1a1-1111-4111-8111-aaaaaaaaaaaa' &&
    noSourceEntry !== undefined &&
    noSourceEntry.sourceType === 'none' &&
    noSourceEntry.sourceId === null,
)
check(
  'draft/readers: the source trace names the record kind and the id stub',
  sourceLabel('master_record_id', 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa') ===
    'Master record aaaaaaaa' &&
    sourceLabel('change_event_id', 'a1a1a1a1-1111-4111-8111-aaaaaaaaaaaa') ===
      'Change event a1a1a1a1' &&
    sourceLabel('backfill_document_id', '11111111-2222-4333-8444-555555555555') ===
      'Backfilled document 11111111' &&
    sourceLabel('none', null) === 'No source resolved',
)
const flags = qcFlagRows(draftDetail.qc_flags)
check(
  'draft/readers: each flag carries its kind, its coordinate and its detail',
  flags.length === 3 &&
    flags[0].kind === 'unexpected_change' &&
    flags[0].detail === 'company.member_count — 1 -> 2' &&
    flags[1].detail.includes('event b2b2b2b2') &&
    flags[2].detail === 'filing_metadata.return.made_up_date',
)
check(
  'draft/readers: the resolved entries are read without inventing anything',
  resolvedCompanyName(draftDetail.resolved_state) === 'Alpha Traders Limited' &&
    resolvedOfficers(draftDetail.resolved_state)[0].name === 'Chan Tai Man' &&
    resolvedOfficers(draftDetail.resolved_state)[0].correspondence ===
      'UNIT 3, GOLDEN GATE BUILDING, 20 DES VOEUX ROAD CENTRAL, CENTRAL' &&
    resolvedMembers(draftDetail.resolved_state)[0].amountPaid === '1000.00' &&
    resolvedChanges(draftDetail.resolved_state)[0].eventId === draftDetail.applied_event_ids[0],
)
check(
  'draft/readers: the company facts and the derived capital total read as rows',
  companyFactRows(draftDetail.resolved_state).some(
    (row) => row.path === 'company.name.name_en' && row.value === 'Alpha Traders Limited',
  ) &&
    !companyFactRows(draftDetail.resolved_state).some((row) =>
      row.path.startsWith('company.share_capital.classes'),
    ) &&
    shareCapital(draftDetail.resolved_state).classes.length === 1 &&
    shareCapital(draftDetail.resolved_state).total.some(
      (row) => row.label === 'Total shares' && row.value === '1500',
    ) &&
    shareCapital(draftDetailMinimal.resolved_state).classes.length === 0,
)
check(
  'draft/readers: the labels speak the lifecycle’s words',
  draftStatusLabel('sent_for_signature') === 'Sent for signature' &&
    draftStatusLabel('invalidated') === 'Invalidated' &&
    signatureStatusLabel('not_sent') === 'Not sent to the client' &&
    draftActionLabel('mark_under_review') === 'Start review' &&
    (draftActionNote('edit') ?? '').includes('filing metadata') &&
    (draftActionNote('approve') ?? '').includes('approval stamp') &&
    formVersionText(draftDetail.form_version) === 'specification 2/2025',
)
const metadataForm = filingMetadataFormFrom(draftDetail)
check(
  'draft/readers: the edit form reads the document back, statement and all',
  metadataForm.fsPeriodStart === '2025-04-01' &&
    metadataForm.fsPeriodEnd === '2026-03-31' &&
    metadataForm.delivery === 'schedule_2' &&
    metadataForm.presentorName === 'K2P Corporate Services Limited' &&
    metadataForm.statement === 'true' &&
    filingMetadataFormFrom({ filing_metadata: {}, private_company_statement: null }).statement === '',
)
const edited = filingMetadataFrom({ ...metadataForm, presentorFax: '  ', fsPeriodStart: '' })
check(
  'draft/readers: the edit posts the stated document, blank fields as explicit nulls, and never the made-up date',
  edited.return?.fs_period_start === null &&
    edited.return?.fs_period_end === '2026-03-31' &&
    edited.return?.made_up_date === undefined &&
    edited.presentor?.fax === null &&
    edited.member_particulars_delivery === 'schedule_2' &&
    statementFrom({ ...metadataForm, statement: 'clear' }) === null &&
    statementFrom({ ...metadataForm, statement: '' }) === undefined &&
    statementFrom({ ...metadataForm, statement: 'true' }) === true,
)
check(
  'draft/readers: the delivery vocabulary is the contract’s three values',
  JSON.stringify(DELIVERY_CHOICES) === JSON.stringify(['schedule_1', 'schedule_2', 'cd_rom']),
)
// The form itself, rendered directly (the draft screen opens the edit by click —
// an interaction SSR does not run). §3.3's vocabulary is offered in full, but
// the listed company's Schedule 2 is disabled with its reason: the fill plan
// refuses to tick `cb_2_P.8` (listed companies are outside Wave 1), so a
// staffer is never led to a channel this build cannot honour.
const metadataFormHtml = renderToStaticMarkup(
  <FilingMetadataForm
    initial={metadataForm}
    madeUpDate="2026-06-30"
    showStatement
    submitLabel="Save the filing metadata"
    onSubmit={noop}
    onCancel={() => undefined}
  />,
)
contains('draft/edit', metadataFormHtml, 'Schedule 2 — listed companies are outside Wave 1')
const schedule2Option = elementHTML(metadataFormHtml, 'listed companies are outside Wave 1')
const schedule1Option = elementHTML(metadataFormHtml, '>Schedule 1<')
const cdRomOption = elementHTML(metadataFormHtml, '>CD-ROM<')
check(
  'draft/edit: only the listed company’s channel is disabled',
  schedule2Option.startsWith('<option') &&
    schedule2Option.includes('disabled') &&
    schedule1Option.startsWith('<option') &&
    !schedule1Option.includes('disabled') &&
    cdRomOption.startsWith('<option') &&
    !cdRomOption.includes('disabled'),
)

// The edit body is diffed against the version's own document before it is
// posted: a save that would change nothing states **no fields at all**, so the
// API's own `edit: nothing to change` refusal fires instead of the merge
// silently moving the draft and writing an audit entry.
const noOpEdit = draftEditRequest(draftDetail, metadataForm)
check(
  'draft/edit: a no-op form states no fields, so the API refuses it rather than moving the draft',
  JSON.stringify(noOpEdit) === JSON.stringify({ action: 'edit' }),
)
const nullBlocksEdit = draftEditRequest(
  draftDetailMinimal,
  filingMetadataFormFrom(draftDetailMinimal),
)
check(
  'draft/edit: a document whose blocks are null is not "changed" by reading it back into the form',
  JSON.stringify(nullBlocksEdit) === JSON.stringify({ action: 'edit' }),
)
const changedEdit = draftEditRequest(draftDetail, { ...metadataForm, fsPeriodEnd: '2026-04-30' })
check(
  'draft/edit: a changed field posts the document — and only it — with the changed value in it',
  JSON.stringify(Object.keys(changedEdit)) === JSON.stringify(['action', 'filing_metadata']) &&
    changedEdit.filing_metadata?.return?.fs_period_end === '2026-04-30' &&
    changedEdit.filing_metadata?.return?.made_up_date === undefined &&
    changedEdit.private_company_statement === undefined,
)
const clearedStatement = draftEditRequest(draftDetail, { ...metadataForm, statement: 'clear' })
const restatedStatement = draftEditRequest(draftDetail, { ...metadataForm, statement: 'true' })
check(
  'draft/edit: only a changed statement travels, and it travels alone',
  JSON.stringify(clearedStatement) ===
    JSON.stringify({ action: 'edit', private_company_statement: null }) &&
    JSON.stringify(restatedStatement) === JSON.stringify({ action: 'edit' }),
)
const metadataOnly = { ...metadataForm, presentorReference: 'NAR1-2026-ALPHA-2' }
const referenceEdit = draftEditRequest(draftDetail, metadataOnly)
check(
  'draft/edit: a changed member of a nested block posts the whole block, the merge filling the rest',
  referenceEdit.filing_metadata?.presentor?.reference === 'NAR1-2026-ALPHA-2' &&
    referenceEdit.filing_metadata?.presentor?.name === 'K2P Corporate Services Limited' &&
    referenceEdit.private_company_statement === undefined,
)

// --- The calendar feed panel ---------------------------------------------

const coverageCovered = calendarSyncCoverage(
  dashboardPopulated.company_cards,
  dashboardPopulated.service_gaps,
)
const coverageTierMissing = calendarSyncCoverage(
  dashboardUnlicensed.company_cards,
  dashboardUnlicensed.service_gaps,
)
const deniedCard = dashboardPopulated.company_cards[1]
const coverageExcluded = calendarSyncCoverage(dashboardPopulated.company_cards, [
  ...dashboardPopulated.service_gaps,
  { company_id: deniedCard.company_id, automation: 'calendar_sync', reason: 'no_assignment' },
])
check('coverage: an untouched portfolio is covered', coverageCovered.kind === 'covered')
check('coverage: no calendar-sync denials means nothing is excluded', coverageCovered.excluded.length === 0)
check(
  'coverage: a deployment-wide tier denial is explained, not shown as an error',
  coverageTierMissing.kind === 'tier_missing',
)
check('coverage: a package-level denial excludes that company', coverageExcluded.excluded.length === 1)
check(
  'coverage: the excluded company is named with the gate reason',
  coverageExcluded.excluded[0].reason === 'no_assignment',
)

const feedEmpty = renderToStaticMarkup(
  <CalendarFeedView
    coverage={coverageCovered}
    credentials={feedCredentialsEmpty}
    issued={null}
    onCreate={noop}
    onRevoke={noop}
    onDismissIssued={() => undefined}
  />,
)
contains('feed/no-credential', feedEmpty, 'Create subscription URL')
contains('feed/no-credential', feedEmpty, 'Your calendar provider will see this data')
contains('feed/no-credential', feedEmpty, 'name, BRN and statutory due dates')
notContains('feed/no-credential', feedEmpty, 'Revoke…')

const feedActive = renderToStaticMarkup(
  <CalendarFeedView
    coverage={coverageCovered}
    credentials={feedCredentials}
    issued={null}
    onCreate={noop}
    onRevoke={noop}
    onDismissIssued={() => undefined}
  />,
)
contains('feed/active', feedActive, 'cannot be shown again')
contains('feed/active', feedActive, 'feed credential 66666666')
contains('feed/active', feedActive, 'created by staff 99999999')
contains('feed/active', feedActive, 'Revoked')
contains('feed/active', feedActive, 'Revoke…')
notContains('feed/active', feedActive, 'Create subscription URL')
notContains('feed/active', feedActive, 'Yes, revoke the feed')

const feedIssued = renderToStaticMarkup(
  <CalendarFeedView
    coverage={coverageCovered}
    credentials={feedCredentialsEmpty}
    issued={issuedFeedCredential}
    onCreate={noop}
    onRevoke={noop}
    onDismissIssued={() => undefined}
  />,
)
contains('feed/issued', feedIssued, 'Copy this URL now')
contains('feed/issued', feedIssued, 'shown once and cannot be shown again')
contains('feed/issued', feedIssued, 'EXAMPLESUBSCRIPTIONTOKEN')
contains('feed/issued', feedIssued, '>Copy<')
contains('feed/issued', feedIssued, 'Done — hide it')

const feedTierMissing = renderToStaticMarkup(
  <CalendarFeedView
    coverage={coverageTierMissing}
    credentials={feedCredentialsEmpty}
    issued={null}
    onCreate={noop}
    onRevoke={noop}
    onDismissIssued={() => undefined}
  />,
)
contains('feed/below-standard', feedTierMissing, 'Calendar sync is a Standard feature')
contains('feed/below-standard', feedTierMissing, 'tier_capability_missing')
contains('feed/below-standard', feedTierMissing, 'in-app compliance calendar is not gated')
notContains('feed/below-standard', feedTierMissing, 'Create subscription URL')

const feedExcluded = renderToStaticMarkup(
  <CalendarFeedView
    coverage={coverageExcluded}
    credentials={feedCredentialsEmpty}
    issued={null}
    onCreate={noop}
    onRevoke={noop}
    onDismissIssued={() => undefined}
  />,
)
contains('feed/excluded', feedExcluded, 'These companies are not in the feed')
contains('feed/excluded', feedExcluded, 'Beta Holdings Limited')
contains('feed/excluded', feedExcluded, 'no active service package is assigned')

// --- The calendar push wizard (slice M8) ------------------------------------

const pushProps = {
  coverage: coverageCovered,
  companies: dashboardPopulated.company_cards,
  onSelectProvider: noop,
  onSaveCredential: noop,
  onDeleteCredential: noop,
  onRunProbe: noop,
}

// No credential: the provider choice and the deployment-flag honesty line,
// and the per-company surface reads entirely as feed — nobody pushes.
const pushNoCredential = renderToStaticMarkup(
  <CalendarPushView
    {...pushProps}
    credential={providerCredentialNone}
    pushStatus={pushStatusFeed}
    guide={null}
    probeResult={null}
  />,
)
contains('push/no-credential', pushNoCredential, 'No provider credential is connected')
contains('push/no-credential', pushNoCredential, 'Microsoft 365 / Entra')
contains('push/no-credential', pushNoCredential, 'Google Workspace')
contains('push/no-credential', pushNoCredential, 'AICS_CALENDAR_PUSH_ENABLED')
contains('push/no-credential', pushNoCredential, 'The push loop is a deployment flag')
contains('push/no-credential', pushNoCredential, '>Feed</span>')
notContains('push/no-credential', pushNoCredential, '>Pushing</span>')
notContains('push/no-credential', pushNoCredential, '>Run probe</button>')
notContains('push/no-credential', pushNoCredential, 'name="client_id"')

// Mid-guide, Microsoft: the static walk renders as numbered steps from the
// payload — one step per registration action, incl. the multi-line ones —
// and the credential form carries the provider's own fields.
const pushGuideMicrosoft = renderToStaticMarkup(
  <CalendarPushView
    {...pushProps}
    credential={providerCredentialNone}
    pushStatus={pushStatusFeed}
    guide={onboardingGuideMicrosoft}
    probeResult={null}
  />,
)
contains(
  'push/guide-microsoft',
  pushGuideMicrosoft,
  'Register a Microsoft (Microsoft 365 / Entra) application',
)
contains('push/guide-microsoft', pushGuideMicrosoft, 'Microsoft Entra admin center')
contains('push/guide-microsoft', pushGuideMicrosoft, 'Calendars.ReadWrite')
contains('push/guide-microsoft', pushGuideMicrosoft, 'Grant admin consent')
// The multi-line scope step renders as one step, its continuation joined.
contains('push/guide-microsoft', pushGuideMicrosoft, 'a read-only scope cannot work')
// 7 provider steps + 4 app steps: one list item per numbered action.
check(
  'push/guide-microsoft: one step per numbered action',
  (pushGuideMicrosoft.match(/<li>/g) ?? []).length === 11,
)
contains('push/guide-microsoft', pushGuideMicrosoft, 'name="directory_tenant_id"')
contains('push/guide-microsoft', pushGuideMicrosoft, 'name="client_id"')
contains('push/guide-microsoft', pushGuideMicrosoft, 'name="client_secret"')
contains('push/guide-microsoft', pushGuideMicrosoft, 'name="refresh_token"')
contains('push/guide-microsoft', pushGuideMicrosoft, 'name="calendar_id"')
check(
  'push/guide-microsoft: the directory id is required for microsoft',
  elementHTML(pushGuideMicrosoft, 'name="directory_tenant_id"').includes('required'),
)

// Mid-guide, Google: the same wizard shape, the google field set — no
// directory tenant id at all (the API refuses one under google).
const pushGuideGoogle = renderToStaticMarkup(
  <CalendarPushView
    {...pushProps}
    credential={providerCredentialNone}
    pushStatus={pushStatusFeed}
    guide={onboardingGuideGoogle}
    probeResult={null}
  />,
)
contains(
  'push/guide-google',
  pushGuideGoogle,
  'Register a Google (Google Workspace) application',
)
contains('push/guide-google', pushGuideGoogle, 'Google Cloud Console')
contains('push/guide-google', pushGuideGoogle, 'Google Calendar API')
check(
  'push/guide-google: one step per numbered action',
  (pushGuideGoogle.match(/<li>/g) ?? []).length === 10,
)
notContains('push/guide-google', pushGuideGoogle, 'name="directory_tenant_id"')

// Credential saved, unprobed: connected and honest about it — the tenant
// stays on the feed, and the probe is the offered next action (R7: the
// completion is the probe flag, never a local checkbox).
const pushSaved = renderToStaticMarkup(
  <CalendarPushView
    {...pushProps}
    credential={providerCredentialSaved}
    pushStatus={pushStatusUnprobed}
    guide={null}
    probeResult={null}
  />,
)
contains('push/saved', pushSaved, 'The probe has not passed')
contains('push/saved', pushSaved, 'an unprobed credential never pushes')
contains('push/saved', pushSaved, 'deadlines@example-tcsp.hk')
contains('push/saved', pushSaved, '>Run probe</button>')
notContains('push/saved', pushSaved, 'Probe passed')
notContains('push/saved', pushSaved, '>Pushing</span>')

// Probe passed: the derived completion — credential plus the probe flag —
// and the per-company surface pushes, feeds, and refuses with the gate's
// own reason. Beta is the gate-denied company (coverageExcluded above).
const pushLive = renderToStaticMarkup(
  <CalendarPushView
    {...pushProps}
    coverage={coverageExcluded}
    credential={providerCredentialProbed}
    pushStatus={pushStatusPushing}
    guide={null}
    probeResult={probePassed}
  />,
)
contains('push/probe-passed', pushLive, 'Probe passed — every leg succeeded.')
contains('push/probe-passed', pushLive, 'deadlines are pushed to this calendar')
contains('push/probe-passed', pushLive, '>Pushing</span>')
contains('push/probe-passed', pushLive, '<code>pushing</code>')
contains('push/probe-passed', pushLive, 'Alpha Traders Limited (BRN 11112222)')
contains('push/probe-passed', pushLive, '<code>no_assignment</code>')
contains('push/probe-passed', pushLive, 'no active service package is assigned')
// A pushing tenant offers no probe button; the credential's metadata and the
// replace/disconnect actions stay.
notContains('push/probe-passed', pushLive, '>Run probe</button>')
contains('push/probe-passed', pushLive, 'Replace credential…')
contains('push/probe-passed', pushLive, 'Disconnect…')

// Probe failed: the leg that failed names its bounded detail, the legs that
// never ran read as skipped, the credential went back to the feed, and the
// flow stays retryable.
const pushFailed = renderToStaticMarkup(
  <CalendarPushView
    {...pushProps}
    credential={providerCredentialSaved}
    pushStatus={pushStatusUnprobed}
    guide={null}
    probeResult={probeFailed}
  />,
)
contains('push/probe-failed', pushFailed, 'Probe failed')
contains('push/probe-failed', pushFailed, 'the credential went back to the feed')
contains('push/probe-failed', pushFailed, '<code>token_refresh</code>')
contains('push/probe-failed', pushFailed, 'invalid_grant')
contains('push/probe-failed', pushFailed, '>Skipped</span>')
check(
  'push/probe-failed: a skipped leg names the skip, not a fake failure',
  pushFailed.indexOf('<code>token_refresh</code>') !== -1 &&
    pushFailed.indexOf('invalid_grant') !== -1 &&
    pushFailed.indexOf('>Skipped</span>') !== -1,
)
contains('push/probe-failed', pushFailed, '>Run probe</button>')

// Below Standard: the same derived refusal as the feed panel — explained
// from the dashboard's own gate flags, with no connect affordance at all.
const pushTierMissing = renderToStaticMarkup(
  <CalendarPushView
    {...pushProps}
    coverage={coverageTierMissing}
    credential={providerCredentialNone}
    pushStatus={pushStatusFeed}
    guide={null}
    probeResult={null}
  />,
)
contains('push/below-standard', pushTierMissing, 'Calendar push is a Standard feature')
contains('push/below-standard', pushTierMissing, '<code>tier_capability_missing</code>')
notContains('push/below-standard', pushTierMissing, 'Microsoft 365 / Entra')
notContains('push/below-standard', pushTierMissing, 'Run probe')


// --- The communications surface's parse fields (slice M5b) -------------------

// The list fixture obeys the parse fields' own contract: a parsed reply
// carries its parse's status and routing, and a row no cycle has parsed
// carries nulls — the two shapes a reader must be able to tell apart.
check(
  'communications fixture: the parse fields ride the parsed rows only',
  companyCommunications.every((item) =>
    item.parse_status === null
      ? item.parse_routing === null && item.language === null
      : item.parse_routing !== null,
  ),
)
check(
  'communications fixture: the empty list is the empty list',
  companyCommunicationsEmpty.length === 0,
)
// Custody on the parse read, at the fixture level: the confirmations carry
// the fact_id → answer join and nothing else — no evidence_span key exists at
// any depth. check-fixtures.py then validates this fixture against the
// backend's own response model (extra="forbid"), which is the wire agreeing.
check(
  'parse fixture: a confirmation is the fact_id → answer join, nothing else',
  replyParse.confirmations.every(
    (item) => JSON.stringify(Object.keys(item).sort()) === JSON.stringify(['answer', 'fact_id']),
  ),
)
check(
  'parse fixture: the payload holds no evidence span anywhere',
  !JSON.stringify(replyParse).includes('evidence_span'),
)

// --- The client-actions panel and the milestone configuration -------------

const panel = renderToStaticMarkup(
  <ClientActionsView
    data={clientActions}
    dueWithin={clientActions.due_within}
    viewerRole="administrator"
    onApplyWindow={() => undefined}
    onRefresh={() => undefined}
    onAct={noop}
  />,
)
// Which period, which company: both are on every block, with the anniversary
// the offsets were counted back from.
contains('panel/periods', panel, 'Alpha Traders Limited')
contains('panel/periods', panel, 'BRN 11112222 · period 2026 — anniversary (made-up) date Fri, 20 Nov 2026')
contains('panel/periods', panel, 'BRN 77778888 · period 2027 — anniversary (made-up) date Fri, 15 Jan 2027')
contains('panel/periods', panel, 'href="#/companies/aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"')
contains('panel/periods', panel, 'Look ahead (days)')
contains('panel/periods', panel, 'value="90"')
// The four milestones, in the order the client meets them.
contains('panel/periods', panel, 'Continuation ask')
contains('panel/periods', panel, 'Data confirmation')
contains('panel/periods', panel, 'Client reply by')
contains('panel/periods', panel, 'Documents to the client')
contains('panel/periods', panel, 'Mon, 21 Sept 2026')
contains('panel/periods', panel, 'Fri, 20 Nov 2026')
// Days remaining, in the calendar's own words.
contains('panel/periods', panel, '3 days overdue')
contains('panel/periods', panel, 'in 43 days')
contains('panel/periods', panel, '6 days overdue')
// The backend's five states, as badges and by name.
contains('panel/periods', panel, 'badge state-done')
contains('panel/periods', panel, 'badge state-pending')
contains('panel/periods', panel, 'badge state-overdue')
contains('panel/periods', panel, 'badge state-blocked')
contains('panel/periods', panel, '>Due today</span>')
// The period's record, in the backend's own vocabulary — including the
// distinction between "no row" and "the answer is not on it yet".
contains('panel/periods', panel, 'Client confirmed')
contains('panel/periods', panel, 'No record for this period')
contains('panel/periods', panel, 'Client declined')
contains('panel/periods', panel, 'Fee received')
// Blocked is explained as what it is — and the note names the leg the read
// returned, never a client-side guess: the reply's precondition, and the
// release gate's leg for the posting, each with its remedy.
contains('panel/periods', panel, '<code>no_confirmation_request</code>')
contains('panel/periods', panel, 'no confirmation request exists for this period yet')
contains('panel/periods', panel, 'a precondition, not a gate')
contains('panel/periods', panel, '<code>payment_not_received</code>')
contains('panel/periods', panel, '<code>continuation_not_recorded</code>')
contains('panel/periods', panel, 'record the fee, correcting the decision first')
contains('panel/periods', panel, 'ask the client, then record the answer')
// §5.1.12's exhaustion: the reply milestone's chain ran out with no reply,
// and the note names the remedy scope §2.5 prescribes — staff contact the
// client by hand.
contains('panel/periods', panel, '<code>reminders_exhausted</code>')
contains('panel/periods', panel, 'every reminder on this request has been sent and the reply is still missing')
// The reason is the backend's own word for the leg, shown beside the remedy.
contains('panel/periods', panel, 'Blocked — <code>')
// The posting's remaining limit is stated: the legs a period can see are
// judged here, the document's form at the download.
contains('panel/periods', panel, 'this read judges the legs a period can see')
contains('panel/periods', panel, 'refused at the download, where its form is known')
// Done says what completed the milestone, and the two honest limits are stated.
contains('panel/periods', panel, 'An answer is recorded for this period')
contains('panel/periods', panel, 'A confirmation request has been generated for this period')
contains(
  'panel/periods',
  panel,
  'A reply received on the request’s thread completes this milestone',
)
contains('panel/periods', panel, 'the product holds the answer')
// The three acts, offered only where the route admits them, with the reason
// each one is missing where it does not.
contains('panel/periods', panel, 'Record the client’s answer…')
contains('panel/periods', panel, 'Record the fee…')
contains('panel/periods', panel, 'Correct the decision…')
contains('panel/periods', panel, 'The client’s answer is recorded. A decision stands')
contains('panel/periods', panel, 'The fee is already recorded for this period')
contains('panel/periods', panel, 'A declined period admits no fee')
contains('panel/periods', panel, 'Not yet: the client’s answer comes first')
contains('panel/periods', panel, 'Nothing is recorded for this period to correct yet')
// The legend states the states and their precedence, and the panel says what
// the dates are (counted back) rather than leaving a reader to infer it.
contains('panel/periods', panel, 'States, as the API derives them')
contains('panel/periods', panel, 'Done outranks the date; blocked outranks it too')
contains('panel/periods', panel, 'names the leg that refused and')
contains('panel/periods', panel, 'A period drops off this panel 30 days after its last date has passed')
contains('panel/periods', panel, 'the milestone configuration these dates come from is on this page, below')

const generalPanel = renderToStaticMarkup(
  <ClientActionsView
    data={clientActions}
    dueWithin={clientActions.due_within}
    viewerRole="general"
    onApplyWindow={() => undefined}
    onAct={noop}
  />,
)
notContains('panel/general', generalPanel, 'the milestone configuration these dates come from is on this page')

const emptyPanel = renderToStaticMarkup(
  <ClientActionsView
    data={clientActionsEmpty}
    dueWithin={clientActionsEmpty.due_within}
    viewerRole="administrator"
    onApplyWindow={() => undefined}
    onAct={noop}
  />,
)
contains('panel/empty', emptyPanel, 'No period has a milestone date in this window')
notContains('panel/empty', emptyPanel, 'Record the client’s answer…')

// The reason travels per period, not as a coincidence: rendered for Alpha's
// block alone, the leg the fixture gives Alpha is the leg the note names.
const alphaPanel = renderToStaticMarkup(
  <ClientActionsView
    data={{ ...clientActions, periods: [clientActions.periods[0]] }}
    dueWithin={clientActions.due_within}
    onApplyWindow={() => undefined}
    onAct={noop}
  />,
)
contains('panel/alpha', alphaPanel, '<code>payment_not_received</code>')
contains('panel/alpha', alphaPanel, 'record the fee, correcting the decision first')
notContains('panel/alpha', alphaPanel, '<code>continuation_not_recorded</code>')
notContains('panel/alpha', alphaPanel, '<code>no_confirmation_request</code>')

// The fixture obeys the field's own contract — a blocked milestone carries a
// reason, an unblocked one carries null — so it cannot drift into a shape the
// API would never send while the shape checks still pass.
check(
  'panel fixture: blocked exactly when the read names a leg',
  clientActions.periods.every((period) =>
    period.milestones.every(
      (milestone) => (milestone.state === 'blocked') === (milestone.blocked_reason !== null),
    ),
  ),
)

// The reply milestone's done state (M5b): a reply on the request's thread
// completes it — the badge, and the note that names what completed it. The
// fixture's narrative keeps Alpha's reply unanswered, so the done branch is
// rendered the same way the unknown-reason panel below mutates its input.
const repliedPanel = renderToStaticMarkup(
  <ClientActionsView
    data={{
      ...clientActions,
      periods: clientActions.periods.map((period, index) =>
        index === 0
          ? {
              ...period,
              milestones: period.milestones.map((milestone) =>
                milestone.kind === 'client_reply'
                  ? { ...milestone, state: 'done' as const, blocked_reason: null }
                  : milestone,
              ),
            }
          : period,
      ),
    }}
    dueWithin={clientActions.due_within}
    onApplyWindow={() => undefined}
    onRefresh={() => undefined}
    onAct={noop}
  />,
)
contains('panel/replied', repliedPanel, 'badge state-done')
contains('panel/replied', repliedPanel, 'A reply from the client has arrived on the request’s thread')
// A done milestone never carries a block reason, and the exhausted-blocked
// note is gone once the reply landed.
notContains('panel/replied', repliedPanel, '<code>reminders_exhausted</code>')

// A status the screen does not know gets no acts, and no claim about a record
// it cannot describe.
const unknownStatusPanel = renderToStaticMarkup(
  <ClientActionsView
    data={{
      ...clientActions,
      periods: [{ ...clientActions.periods[0], renewal_status: 'escalated' }],
    }}
    dueWithin={clientActions.due_within}
    onApplyWindow={() => undefined}
    onAct={noop}
  />,
)
contains('panel/unknown-status', unknownStatusPanel, 'which is not a status this screen knows')
notContains('panel/unknown-status', unknownStatusPanel, 'Record the fee…')
notContains('panel/unknown-status', unknownStatusPanel, 'Correct the decision…')

// A blocked milestone the read gives a reason this screen does not know says
// exactly that — the panel never invents a remedy for a leg it cannot name.
// The cast stands in for a server that learned a new leg first.
const unknownReasonPanel = renderToStaticMarkup(
  <ClientActionsView
    data={{
      ...clientActions,
      periods: [
        {
          ...clientActions.periods[0],
          milestones: clientActions.periods[0].milestones.map((milestone) =>
            milestone.kind === 'signature_post'
              ? {
                  ...milestone,
                  state: 'blocked',
                  blocked_reason: 'at_risk' as unknown as MilestoneBlockReason,
                }
              : milestone,
          ),
        },
      ],
    }}
    dueWithin={clientActions.due_within}
    onApplyWindow={() => undefined}
    onAct={noop}
  />,
)
contains('panel/unknown-reason', unknownReasonPanel, 'the read names no reason this screen knows')
contains('panel/unknown-reason', unknownReasonPanel, 'check that this panel and the API are the same version')
notContains('panel/unknown-reason', unknownReasonPanel, 'at_risk')

// The configuration panel: the stored numbers (not the shipped defaults), the
// wire names beside them, the templates the PUT carries, and the semantics a
// reader must not get backwards.
const configPanel = renderToStaticMarkup(<MilestoneConfigView config={tenantConfig} onSave={noop} />)
contains('config/stored', configPanel, 'value="75"')
contains('config/stored', configPanel, 'value="35"')
contains('config/stored', configPanel, 'value="21"')
contains('config/stored', configPanel, 'value="15"')
contains('config/stored', configPanel, 'value="14"')
contains('config/stored', configPanel, 'value="5, 12"')
contains('config/stored', configPanel, 'value="anniversary"')
contains('config/stored', configPanel, 'continuation_request_days_before')
contains('config/stored', configPanel, 'minimum_reply_window_days')
contains('config/stored', configPanel, 'reminder_cadence_days')
// The cadence's copy names the slice that honours it (§5.1.12's sweep) and
// each send mode's behaviour — the stale "nothing sends reminders yet" line
// is gone.
contains('config/stored', configPanel, 'The reminder sweep')
contains('config/stored', configPanel, 'reminders send themselves')
contains('config/stored', configPanel, 'held for a staff release')
contains('config/stored', configPanel, 'the dashboard shows the due dates to act on by hand')
notContains('config/stored', configPanel, 'Nothing sends reminders yet')
contains('config/stored', configPanel, 'Last saved')
// The two things a reader gets wrong, said plainly.
contains('config/stored', configPanel, 'days counted <em>back</em>')
contains('config/stored', configPanel, 'a bigger number is an <em>earlier</em> date')
contains('config/stored', configPanel, 'emergency margin, not a deadline the product prints')
contains('config/stored', configPanel, '62 days before the return falls due')
contains('config/stored', configPanel, 'CR’s last day is never shown to a client')
contains('config/stored', configPanel, 'each of the first four numbers must be <em>bigger</em> than the one after it')
// What the save carries, and what the reply offset is bounded by.
contains('config/stored', configPanel, 'replaces the whole configuration')
contains('config/stored', configPanel, '{CompanyCode}/{Year}/{Form}_{DocType}_{CR}_{YYYYMMDD}_v{Version}.{Ext}')
contains('config/stored', configPanel, 'may not exceed 42 days minus that window')
contains('config/stored', configPanel, 'Save configuration')

const configDefaults = renderToStaticMarkup(
  <MilestoneConfigView config={tenantConfigDefaults} onSave={noop} />,
)
contains('config/defaults', configDefaults, 'never saved a configuration')
contains('config/defaults', configDefaults, 'value="60"')
contains('config/defaults', configDefaults, 'value="3, 7, 10"')
notContains('config/defaults', configDefaults, 'Last saved')
// A readable configuration carries no integrity notice: the field is what
// tells "the tenant stored this" from "this is the shipped default".
notContains('config/defaults', configDefaults, 'The stored configuration cannot be read')
notContains('config/stored', configPanel, 'The stored configuration cannot be read')

// The read that must not fail: a stored block nobody can read is stated as
// what it is, with the block, the violations a replacement has to satisfy, and
// the repair — never a silent fallback to numbers the tenant did not choose.
const configUnreadable = renderToStaticMarkup(
  <MilestoneConfigView config={tenantConfigUnreadable} onSave={noop} />,
)
contains('config/unreadable', configUnreadable, 'The stored configuration cannot be read')
contains('config/unreadable', configUnreadable, 'shipped defaults')
contains('config/unreadable', configUnreadable, '<code>milestones</code>')
// Every violation the API listed travels, not the first one.
contains('config/unreadable', configUnreadable, 'must be an integer (got str)')
contains('config/unreadable', configUnreadable, "must be &#x27;anniversary&#x27;")
contains('config/unreadable', configUnreadable, 'Saving replaces the two blocks this screen carries')
// The screen says which block it *cannot* repair: the one its save omits.
contains('config/unreadable', configUnreadable, 'the save is refused 422 with the same violations')
contains('config/unreadable', configUnreadable, 'a block of it cannot be read')
// The form is still a form — the repair is the ordinary save, so the numbers
// and the save button are present rather than replaced by an error screen.
contains('config/unreadable', configUnreadable, 'Save configuration')
contains('config/unreadable', configUnreadable, 'value="60"')
notContains('config/unreadable', configUnreadable, 'never saved a configuration')

// When the unreadable block is the template pair, the templates section says
// what it is showing — the shipped default — and that the same save repairs it.
const configUnreadableTemplates = renderToStaticMarkup(
  <MilestoneConfigView
    config={{
      ...tenantConfigUnreadable,
      unreadable_blocks: [{ block: 'naming_templates', violations: ['document: unknown tokens: Bogus'] }],
    }}
    onSave={noop}
  />,
)
contains('config/unreadable-templates', configUnreadableTemplates, '<code>naming_templates</code>')
contains('config/unreadable-templates', configUnreadableTemplates, 'unknown tokens: Bogus')
contains(
  'config/unreadable-templates',
  configUnreadableTemplates,
  'the stored pair could not be read, so saving this configuration writes them over it',
)

// --- The §5.1.9 translations the views make ------------------------------

check(
  'corrections: a confirmed period is corrected to declined',
  JSON.stringify(correctionTargets('confirmed')) === JSON.stringify(['declined']),
)
check(
  'corrections: a declined period is corrected to confirmed',
  JSON.stringify(correctionTargets('declined')) === JSON.stringify(['confirmed']),
)
check(
  'corrections: a paid period admits either answer',
  JSON.stringify(correctionTargets('payment_received')) === JSON.stringify(['confirmed', 'declined']),
)
check(
  'corrections: an unanswered period (or none) admits none',
  correctionTargets('pending').length === 0 && correctionTargets(null).length === 0,
)
check(
  'blocked: the gate’s legs are read through to their own remedies',
  (blockedReasonNote('no_assignment') ?? '').includes('no service package assigned') &&
    (blockedReasonNote('out_of_coverage') ?? '').includes('outside the assignment’s coverage period') &&
    (blockedReasonNote('continuation_not_recorded') ?? '').includes('record the answer') &&
    (blockedReasonNote('payment_not_received') ?? '').includes('record the fee'),
)
check(
  'blocked: the reply’s missing letter is named as a precondition',
  (blockedReasonNote('no_confirmation_request') ?? '').includes('precondition, not a gate') &&
    (blockedReasonNote('no_confirmation_request') ?? '').includes('nothing to reply to'),
)
check(
  'blocked: a leg the panel can never answer is still read (it shares the refusal notes)',
  blockedReasonNote('assignment_ended') !== null,
)
check(
  'blocked: an unknown reason is left unnamed rather than given an invented remedy',
  blockedReasonNote('at_risk') === null,
)

check(
  'statuses: the four the record defines (and no row) are known',
  knownRenewalStatus(null) &&
    knownRenewalStatus('pending') &&
    knownRenewalStatus('confirmed') &&
    knownRenewalStatus('declined') &&
    knownRenewalStatus('payment_received'),
)
check('statuses: anything else is not known', !knownRenewalStatus('escalated'))

const configured = describeRefusal(new ApiError(409, 'already_closed'))
check(
  'refusals: a known code is shown with its remedy',
  configured.message.includes('already_closed') && configured.message.includes('Correct the decision'),
)
const gateRefusal = describeRefusal(new ApiError(409, 'service_not_in_scope: out_of_coverage'))
check(
  'refusals: a scope leg is read through to its own reason',
  gateRefusal.message.includes('out_of_coverage') &&
    gateRefusal.message.includes('outside the assignment’s coverage period'),
)
const plainRefusal = describeRefusal(new ApiError(404, 'company not found'))
check(
  'refusals: an unknown detail keeps the plain status line',
  plainRefusal.message.startsWith('Request failed (HTTP 404)'),
)

// --- Calendar ------------------------------------------------------------

const calendar = renderToStaticMarkup(
  <CalendarView
    data={calendarPopulated}
    range={{ from: calendarPopulated.from, to: calendarPopulated.to }}
    onApplyRange={() => undefined}
  />,
)
contains('calendar', calendar, 'Fri, 18 Sept 2026')
contains('calendar', calendar, 'Mon, 05 Oct 2026')
contains('calendar', calendar, '22 Aug 2026')
contains('calendar', calendar, '22 Mar 2027')
contains('calendar', calendar, '3 days overdue')
contains('calendar', calendar, '30-day alert')
contains('calendar', calendar, '7-day alert')
// Slice L: the overdue deadline names its late fee; the on-time ones don't.
contains('calendar', calendar, 'HK$870')
notContains('calendar', calendar, 'HK$105')
contains('calendar', calendar, 'Zeta Logistics Limited')
contains('calendar', calendar, 'Showing 4 of 4 deadlines')
contains('calendar', calendar, 'Deadlines not generated')
contains('calendar', calendar, 'Company dddddddd')

// --- Login ---------------------------------------------------------------

const freshLogin = renderToStaticMarkup(<LoginView />)
contains('login', freshLogin, 'Sign in with your staff account')
notContains('login', freshLogin, 'Your session has ended')

expireSession()
const expiredLogin = renderToStaticMarkup(<LoginView />)
contains('login/expired', expiredLogin, 'Your session has ended')

// --- Staff MFA (phase 7, scope §8): the challenge and enrollment screens ---

// The challenge step: one code field, the authenticator named, and a refusal
// shown as the backend's own code and remedy — the session stays in the
// challenge state (the route keeps the marker; SSR renders the view only).
const challenge = renderToStaticMarkup(
  <MfaChallengeView displayName="A. Secretary" busy={false} error={null} onSubmit={noop} />,
)
contains('mfa/challenge', challenge, 'Two-step sign-in')
contains('mfa/challenge', challenge, 'authenticator app')
contains('mfa/challenge', challenge, 'name="mfa_code"')
notContains('mfa/challenge', challenge, 'totp_invalid')

const refusedChallenge = renderToStaticMarkup(
  <MfaChallengeView
    displayName="A. Secretary"
    busy={false}
    error={new ApiError(422, 'totp_invalid')}
    onSubmit={noop}
  />,
)
contains('mfa/challenge/refused', refusedChallenge, 'totp_invalid')
contains('mfa/challenge/refused', refusedChallenge, 'wrote nothing')

// The enrollment screen, before and after begin. The secret here is the
// RFC 4648 example key — synthetic, inline, never a fixture; the real secret
// exists only in the route's component state between begin and complete.
const enrollmentIdle = renderToStaticMarkup(
  <MfaEnrollmentView begin={null} busy={false} error={null} onBegin={noop} onSubmit={noop} />,
)
contains('mfa/enrollment/idle', enrollmentIdle, 'Set up two-step sign-in')
contains('mfa/enrollment/idle', enrollmentIdle, 'Start enrollment')
notContains('mfa/enrollment/idle', enrollmentIdle, 'mfa_code')

const SYNTHETIC_SECRET = 'JBSWY3DPEHPK3PXP'
const enrollmentHolding = renderToStaticMarkup(
  <MfaEnrollmentView
    begin={{
      secret: SYNTHETIC_SECRET,
      uri: `otpauth://totp/K2P:admin%40example-tcsp.hk?secret=${SYNTHETIC_SECRET}`,
      enrollment_state: 'pending',
    }}
    busy={false}
    error={null}
    onBegin={noop}
    onSubmit={noop}
  />,
)
contains('mfa/enrollment/holding', enrollmentHolding, 'shown only this once')
contains('mfa/enrollment/holding', enrollmentHolding, 'name="mfa_secret"')
contains('mfa/enrollment/holding', enrollmentHolding, SYNTHETIC_SECRET)
contains('mfa/enrollment/holding', enrollmentHolding, 'manual entry')
contains('mfa/enrollment/holding', enrollmentHolding, 'otpauth://totp/')
contains('mfa/enrollment/holding', enrollmentHolding, 'name="mfa_code"')
notContains('mfa/enrollment/holding', enrollmentHolding, 'Start enrollment')

// The session marker the app gate routes on: set from a login that owes MFA,
// moved enrollment → challenge, lifted by a passed challenge, re-pinnable
// from the guard's status read. No secret-shaped value is involved.
startSession({ ...loginFixture, mfa_required: true, enrollment_state: 'pending' })
check(
  'mfa: a login that owes enrollment marks the session pending',
  getSessionState().session?.mfa?.enrollment_state === 'pending',
)
mfaEnrollmentSealed()
check(
  'mfa: sealing the enrollment moves the marker to the challenge',
  getSessionState().session?.mfa?.enrollment_state === 'enrolled',
)
mfaRequiredFromServer('pending')
check(
  'mfa: the guard can pin the marker back to what the server named',
  getSessionState().session?.mfa?.enrollment_state === 'pending',
)
mfaChallengePassed()
check('mfa: a passed challenge lifts the marker', getSessionState().session?.mfa === null)
expireSession()

// --- The pre-use risk-acknowledgment interstitial (the 2026-10-04 rulings) ---

// The unacknowledged Administrator: the served statement renders verbatim
// (ruling R5 — the text the API sends, including the provider naming), with
// the explicit confirm action live.
const ackAdmin = renderToStaticMarkup(
  <RiskAcknowledgmentView
    data={riskAckUnacknowledged}
    viewerRole="administrator"
    busy={false}
    error={null}
    onConfirm={noop}
  />,
)
contains('risk-ack/admin', ackAdmin, 'Statement version v1')
contains('risk-ack/admin', ackAdmin, 'AI features risk disclosure')
contains('risk-ack/admin', ackAdmin, 'SiliconFlow')
contains('risk-ack/admin', ackAdmin, 'PDPA')
contains('risk-ack/admin', ackAdmin, 'I have read and understood this statement')
check(
  'risk-ack/admin: the confirm is a live button while no POST is in flight',
  !elementHTML(ackAdmin, 'I have read and understood this statement').includes('disabled'),
)

// The busy state: the confirm is disabled while the POST is in flight.
const ackAdminBusy = renderToStaticMarkup(
  <RiskAcknowledgmentView
    data={riskAckUnacknowledged}
    viewerRole="administrator"
    busy={true}
    error={null}
    onConfirm={noop}
  />,
)
contains('risk-ack/admin/busy', ackAdminBusy, 'Confirming…')
check(
  'risk-ack/admin/busy: the confirm is disabled in flight',
  elementHTML(ackAdminBusy, 'Confirming…').includes('disabled'),
)

// A refused POST renders through the house refusal pattern: the backend's
// non-Administrator write is a 403 `forbidden`, which reads as the standard
// not-permitted line.
const ackAdminRefused = renderToStaticMarkup(
  <RiskAcknowledgmentView
    data={riskAckUnacknowledged}
    viewerRole="administrator"
    busy={false}
    error={new ApiError(403, 'forbidden')}
    onConfirm={noop}
  />,
)
contains('risk-ack/admin/refused', ackAdminRefused, 'Not permitted')

// The unacknowledged staff account: the statement is readable, the notice
// names who must confirm, and no confirm affordance exists at all.
const ackStaff = renderToStaticMarkup(
  <RiskAcknowledgmentView
    data={riskAckUnacknowledged}
    viewerRole="general"
    busy={false}
    error={null}
    onConfirm={noop}
  />,
)
contains('risk-ack/staff', ackStaff, 'AI features risk disclosure')
contains(
  'risk-ack/staff',
  ackStaff,
  'An Administrator must confirm this statement before this deployment is used.',
)
notContains('risk-ack/staff', ackStaff, 'I have read and understood this statement')
notContains('risk-ack/staff', ackStaff, '<button')

// The acknowledged state renders nothing: the interstitial is exactly the
// not-acknowledged surface (the gate's acknowledged branch is the shell).
const ackDone = renderToStaticMarkup(
  <RiskAcknowledgmentView
    data={riskAckAcknowledged}
    viewerRole="administrator"
    busy={false}
    error={null}
    onConfirm={noop}
  />,
)
check('risk-ack/acknowledged: the interstitial is absent', ackDone === '')

// The post-bump re-required state: the revised text renders, the version
// moved, and even a staff account meets the interstitial again.
const ackReopened = renderToStaticMarkup(
  <RiskAcknowledgmentView
    data={riskAckReopened}
    viewerRole="general"
    busy={false}
    error={null}
    onConfirm={noop}
  />,
)
contains('risk-ack/reopened', ackReopened, 'Statement version v2')
contains('risk-ack/reopened', ackReopened, 'This statement is version v2.')
contains(
  'risk-ack/reopened',
  ackReopened,
  'An Administrator must confirm this statement before this deployment is used.',
)
notContains('risk-ack/reopened', ackReopened, 'I have read and understood this statement')

// The fixtures obey the field's own contract: the bump carries a newer
// version and fails closed to unacknowledged, so they cannot drift into a
// shape the API would never send while the render checks still pass.
check(
  'risk-ack fixtures: the bump re-opens by construction',
  riskAckAcknowledged.acknowledged === true &&
    riskAckAcknowledged.statement_version === riskAckUnacknowledged.statement_version &&
    riskAckReopened.statement_version !== riskAckUnacknowledged.statement_version &&
    riskAckReopened.acknowledged === false,
)

// --- The failure panel: a visible state with a retry affordance ----------

const offlinePanel = renderToStaticMarkup(
  <ErrorPanel error={new ApiError(0, 'network request failed')} onRetry={() => undefined} />,
)
contains('error-panel/offline', offlinePanel, 'Could not reach the server')
contains('error-panel/offline', offlinePanel, 'Retry')

const forbiddenPanel = renderToStaticMarkup(
  <ErrorPanel error={new ApiError(403, 'forbidden')} onRetry={() => undefined} />,
)
contains('error-panel/403', forbiddenPanel, 'Not permitted')

// --- The client: session handling, request shapes, failure modes ---------

async function checkTransport(): Promise<void> {
  startSession(loginFixture)
  check('session: token held after login', getSessionState().session?.token === loginFixture.access_token)

  stubFetch(200, dashboardPopulated)
  const data = await fetchDashboard()
  check('transport: dashboard JSON parsed', data.portfolio.total_companies === 4)

  stubFetch(401, { detail: 'not authenticated' })
  const unauthorised = await captureError(() => fetchDashboard())
  check(
    'transport: 401 throws ApiError(401)',
    unauthorised instanceof ApiError && unauthorised.status === 401,
  )
  check('transport: 401 cleared the session', getSessionState().session === null)
  check('transport: 401 set the expired notice', getSessionState().notice === 'expired')

  stubOffline()
  const offline = await captureError(() => fetchDashboard())
  check('transport: offline is ApiError(0)', offline instanceof ApiError && offline.status === 0)
  check('transport: offline copy', errorText(offline).includes('Could not reach the server'))

  stubFetch(403, { detail: 'forbidden' })
  const forbidden = await captureError(() => fetchDashboard())
  check('transport: 403 copy', errorText(forbidden).includes('Not permitted'))

  stubFetch(401, { detail: 'invalid credentials' })
  const badLogin = await captureError(() => login('someone@example-tcsp.hk', 'wrong'))
  check(
    'transport: a failed login is a credential error, not a session event',
    badLogin instanceof ApiError &&
      badLogin.status === 401 &&
      badLogin.detail === 'invalid credentials' &&
      getSessionState().session === null,
  )

  // --- The MFA surface's calls, against the real paths and bodies ---

  startSession({ ...loginFixture, mfa_required: true, enrollment_state: 'enrolled' })
  stubFetch(200, mfaStatus)
  const status = await fetchMfaStatus()
  check(
    'transport: the status read is the allowlisted route, no body',
    lastRequest?.url === '/api/v1/auth/mfa/status' &&
      lastRequest.method === 'GET' &&
      lastRequest.body === undefined &&
      status.enrollment_state === 'pending' &&
      status.mfa_verified === false,
  )

  stubFetch(200, { secret: SYNTHETIC_SECRET, uri: 'otpauth://totp/K2P:x', enrollment_state: 'pending' })
  const begun = await beginMfaEnrollment()
  check(
    'transport: enrollment begin posts the bare begin route',
    lastRequest?.url === '/api/v1/auth/mfa/enrollment' &&
      lastRequest.method === 'POST' &&
      lastRequest.body === undefined &&
      begun.secret === SYNTHETIC_SECRET,
  )

  stubFetch(200, { enrollment_state: 'enrolled' })
  const completed = await completeMfaEnrollment('123456', SYNTHETIC_SECRET)
  check(
    'transport: enrollment complete hands the caller-held secret back with the code',
    lastRequest?.url === '/api/v1/auth/mfa/enrollment/complete' &&
      lastRequest.method === 'POST' &&
      JSON.stringify(lastRequest.body) ===
        JSON.stringify({ code: '123456', secret: SYNTHETIC_SECRET }) &&
      completed.enrollment_state === 'enrolled',
  )

  stubFetch(200, { enrollment_state: 'enrolled', mfa_verified: true })
  const verified = await verifyMfaCode('123456')
  check(
    'transport: the challenge posts the code to the verify route',
    lastRequest?.url === '/api/v1/auth/mfa/verify' &&
      lastRequest.method === 'POST' &&
      JSON.stringify(lastRequest.body) === JSON.stringify({ code: '123456' }) &&
      verified.mfa_verified === true,
  )

  stubFetch(422, { detail: 'totp_invalid' })
  const refusedCode = await captureError(() => verifyMfaCode('000000'))
  const refusedCodeNotice = renderToStaticMarkup(<RefusalNotice error={refusedCode} />)
  contains('refusal/totp_invalid', refusedCodeNotice, 'totp_invalid')
  contains('refusal/totp_invalid', refusedCodeNotice, 'wrote nothing')

  // --- The risk-acknowledgment gate's calls ---

  stubFetch(200, riskAckUnacknowledged)
  const ackState = await fetchRiskAcknowledgment()
  check(
    'transport: the acknowledgment read is the risk-acknowledgment route',
    lastRequest?.url === '/api/v1/risk-acknowledgment' &&
      lastRequest.method === 'GET' &&
      ackState.acknowledged === false &&
      ackState.statement_version === 'v1' &&
      ackState.statement_text.startsWith('AI features risk disclosure'),
  )

  stubFetch(200, riskAckAcknowledged)
  const ackAccepted = await acceptRiskAcknowledgment()
  check(
    'transport: the confirmation posts the bare route and answers the resulting state',
    lastRequest?.url === '/api/v1/risk-acknowledgment' &&
      lastRequest.method === 'POST' &&
      lastRequest.body === undefined &&
      ackAccepted.acknowledged === true,
  )

  stubFetch(403, { detail: 'forbidden' })
  const ackForbidden = await captureError(() => acceptRiskAcknowledgment())
  check(
    'transport: a non-Administrator confirmation is refused 403 in the API’s own words',
    ackForbidden instanceof ApiError &&
      ackForbidden.status === 403 &&
      ackForbidden.detail === 'forbidden',
  )

  // --- The review screen's calls, against the real paths and bodies ---

  startSession(loginFixture)
  stubFetch(200, importBatches)
  const batchList = await fetchImportBatches('aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa')
  check(
    'transport: the batch list reads the company-scoped path',
    lastRequest?.url === '/api/v1/companies/aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa/import-batches' &&
      lastRequest.method === 'GET',
  )
  check('transport: the batch list parses', batchList[0].records_pending === 2)

  stubFetch(200, importBatch)
  const batch = await fetchImportBatch('aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa', importBatch.id)
  check(
    'transport: the batch read carries records and the assessment',
    batch.records.length === 7 && batch.assessment?.routing === 'staff_review',
  )

  stubFetch(200, importBatch.records[1])
  await confirmImportRecord(
    'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',
    importBatch.id,
    importBatch.records[1].id,
    '2025-01-15',
  )
  check(
    'transport: confirm posts the reviewer’s effective date',
    lastRequest?.url.endsWith(
      `/import-batches/${importBatch.id}/records/${importBatch.records[1].id}/confirm`,
    ) === true &&
      lastRequest.method === 'POST' &&
      JSON.stringify(lastRequest.body) === JSON.stringify({ effective_date: '2025-01-15' }),
  )

  stubFetch(200, importBatch.records[0])
  await confirmImportRecord(
    'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',
    importBatch.id,
    importBatch.records[0].id,
    null,
  )
  check(
    'transport: a confirmation with no date sends null, not a guess',
    JSON.stringify(lastRequest?.body) === JSON.stringify({ effective_date: null }),
  )

  stubFetch(422, {
    detail:
      'company.email has no effective date: the filing does not state one, so the reviewer must supply it',
  })
  const refused = await captureError(() =>
    confirmImportRecord(
      'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',
      importBatch.id,
      importBatch.records[1].id,
      null,
    ),
  )
  check(
    'transport: a missing effective date comes back as the API’s own 422 reason',
    refused instanceof ApiError &&
      refused.status === 422 &&
      refused.detail.includes('must supply it'),
  )

  stubFetch(200, { ...importBatch.records[6], status: 'rejected' })
  await rejectImportRecord(
    'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',
    importBatch.id,
    importBatch.records[6].id,
  )
  check(
    'transport: reject posts to the record’s own path',
    lastRequest?.url.endsWith(`/records/${importBatch.records[6].id}/reject`) === true &&
      lastRequest.method === 'POST',
  )

  // --- The feed panel's calls ---

  stubFetch(201, issuedFeedCredential)
  const issued = await createFeedCredential()
  check(
    'transport: creating a credential reads the subscription URL',
    lastRequest?.url === '/api/v1/calendar/feed-credentials' &&
      lastRequest.method === 'POST' &&
      issued.subscription_url.includes('/api/v1/calendar/feed.ics?token='),
  )

  stubFetch(409, { detail: 'an active feed credential already exists; revoke it first' })
  const conflict = await captureError(() => createFeedCredential())
  check(
    'transport: a second credential is refused with 409',
    conflict instanceof ApiError && conflict.status === 409,
  )

  stubFetch(200, { ...feedCredentials[1], revoked_at: '2026-09-21T10:00:00Z' })
  const revoked = await revokeFeedCredential(issued.id)
  check(
    'transport: revoking posts to the credential’s path',
    lastRequest?.url === `/api/v1/calendar/feed-credentials/${issued.id}/revoke` &&
      lastRequest.method === 'POST' &&
      revoked.revoked_at !== null,
  )

  // --- The push wizard's calls (slice M8) ---

  stubFetch(200, providerCredentialNone)
  const noProviderCredential = await fetchProviderCredential()
  check(
    'transport: the provider credential reads the push credential route',
    lastRequest?.url === '/api/v1/calendar/provider-credentials' &&
      lastRequest.method === 'GET' &&
      noProviderCredential.connected === false,
  )

  stubFetch(201, providerCredentialSaved)
  const savedProvider = await saveProviderCredential({
    provider: 'microsoft',
    client_id: '11111111-2222-4333-8444-555555555555',
    client_secret: 'app-secret',
    refresh_token: 'refresh-token',
    calendar_id: 'deadlines@example-tcsp.hk',
    directory_tenant_id: '99999999-8888-4777-8777-666666666666',
  })
  check(
    'transport: saving a microsoft credential posts the provider’s five values',
    lastRequest?.url === '/api/v1/calendar/provider-credentials' &&
      lastRequest.method === 'POST' &&
      JSON.stringify(lastRequest.body) ===
        JSON.stringify({
          provider: 'microsoft',
          client_id: '11111111-2222-4333-8444-555555555555',
          client_secret: 'app-secret',
          refresh_token: 'refresh-token',
          calendar_id: 'deadlines@example-tcsp.hk',
          directory_tenant_id: '99999999-8888-4777-8777-666666666666',
        }) &&
      savedProvider.probe_passed === false,
  )

  stubFetch(201, providerCredentialSaved)
  await saveProviderCredential({
    provider: 'google',
    client_id: 'google-client-id',
    client_secret: 'google-secret',
    refresh_token: 'google-refresh',
    calendar_id: 'primary',
  })
  check(
    'transport: a google credential carries no directory_tenant_id key at all',
    JSON.stringify(lastRequest?.body) ===
      JSON.stringify({
        provider: 'google',
        client_id: 'google-client-id',
        client_secret: 'google-secret',
        refresh_token: 'google-refresh',
        calendar_id: 'primary',
      }),
  )

  stubFetch(200, probePassed)
  const probe = await runCalendarProbe()
  check(
    'transport: the probe posts to the probe route with no body',
    lastRequest?.url === '/api/v1/calendar/probe' &&
      lastRequest.method === 'POST' &&
      lastRequest.body === undefined &&
      probe.passed === true,
  )

  stubFetch(409, { detail: 'not_connected' })
  const probeWithoutCredential = await captureError(() => runCalendarProbe())
  check(
    'transport: probing with no credential is the API’s own 409 not_connected',
    probeWithoutCredential instanceof ApiError &&
      probeWithoutCredential.status === 409 &&
      probeWithoutCredential.detail === 'not_connected',
  )

  stubFetch(200, pushStatusPushing)
  const pushStatus = await fetchPushStatus()
  check(
    'transport: the push status reads the pairing route',
    lastRequest?.url === '/api/v1/calendar/push-status' &&
      lastRequest.method === 'GET' &&
      pushStatus.mode === 'push' &&
      pushStatus.probe_passed === true,
  )

  stubFetch(200, providerCredentialNone)
  const disconnected = await deleteProviderCredential()
  check(
    'transport: disconnecting deletes the provider credential',
    lastRequest?.url === '/api/v1/calendar/provider-credentials' &&
      lastRequest.method === 'DELETE' &&
      disconnected.connected === false,
  )

  stubFetch(200, onboardingGuideMicrosoft)
  const guide = await fetchOnboardingGuide('microsoft')
  check(
    'transport: the guide read names the provider as a query parameter',
    lastRequest?.url === '/api/v1/calendar/onboarding-guide?provider=microsoft' &&
      lastRequest.method === 'GET' &&
      guide.provider === 'microsoft' &&
      guide.guide.startsWith('# Register a Microsoft'),
  )

  stubFetch(422, {
    detail: [
      {
        loc: ['body', 'directory_tenant_id'],
        msg: 'directory_tenant_id is microsoft-only',
        type: 'value_error',
      },
    ],
  })
  const malformedSave = await captureError(() =>
    saveProviderCredential({
      provider: 'google',
      client_id: 'google-client-id',
      client_secret: 'google-secret',
      refresh_token: 'google-refresh',
      calendar_id: 'primary',
      directory_tenant_id: 'a-microsoft-only-field',
    }),
  )
  check(
    'transport: a malformed credential surfaces the API’s own 422 violation',
    malformedSave instanceof ApiError &&
      malformedSave.status === 422 &&
      malformedSave.violations.length === 1 &&
      malformedSave.violations[0].includes('directory_tenant_id'),
  )

  // --- The panel's and the configuration's calls ---

  const ALPHA_ID = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'

  stubFetch(200, clientActions)
  const actions = await fetchClientActions(90)
  check(
    'transport: the panel read carries the window',
    lastRequest?.url === '/api/v1/client-actions?due_within=90' && lastRequest.method === 'GET',
  )
  check(
    'transport: the panel parses its periods and milestones',
    actions.periods.length === 4 && actions.periods[0].milestones.length === 4,
  )

  stubFetch(200, tenantConfig)
  const stored = await fetchTenantConfig()
  check(
    'transport: the configuration read is the tenant-config route',
    lastRequest?.url === '/api/v1/tenant-config' && lastRequest.method === 'GET',
  )
  check('transport: the configuration parses its milestone block', stored.milestones.reminder_cadence_days[1] === 12)

  stubFetch(200, tenantConfig)
  await saveTenantConfig({
    naming_templates: tenantConfig.naming_templates,
    milestones: tenantConfig.milestones,
  })
  check(
    'transport: the save PUTs both blocks together',
    lastRequest?.url === '/api/v1/tenant-config' &&
      lastRequest.method === 'PUT' &&
      JSON.stringify(lastRequest.body) ===
        JSON.stringify({
          naming_templates: tenantConfig.naming_templates,
          milestones: tenantConfig.milestones,
        }),
  )

  stubFetch(200, serviceRenewal)
  await openServiceRenewal(ALPHA_ID, 2026)
  check(
    'transport: opening a period posts its anniversary year',
    lastRequest?.url === `/api/v1/companies/${ALPHA_ID}/service-renewals` &&
      lastRequest.method === 'POST' &&
      JSON.stringify(lastRequest.body) === JSON.stringify({ anniversary_year: 2026 }),
  )

  stubFetch(200, serviceRenewal)
  await recordRenewalDecision(ALPHA_ID, 2026, 'declined', 'client rang on the 12th')
  check(
    'transport: a decision posts its answer and note',
    lastRequest?.url.endsWith('/service-renewals/2026/decision') === true &&
      JSON.stringify(lastRequest.body) ===
        JSON.stringify({ decision: 'declined', note: 'client rang on the 12th' }),
  )

  stubFetch(200, serviceRenewal)
  await recordRenewalDecision(ALPHA_ID, 2026, 'confirmed', null)
  check(
    'transport: a note-less decision sends null, not an empty string',
    JSON.stringify(lastRequest?.body) === JSON.stringify({ decision: 'confirmed', note: null }),
  )

  stubFetch(200, { ...serviceRenewal, status: 'payment_received' })
  await recordRenewalPayment(ALPHA_ID, 2026, null)
  check(
    'transport: the fee posts a note and nothing about money',
    lastRequest?.url.endsWith('/service-renewals/2026/payment') === true &&
      JSON.stringify(lastRequest.body) === JSON.stringify({ note: null }),
  )

  stubFetch(200, { ...serviceRenewal, status: 'declined' })
  await correctRenewalDecision(ALPHA_ID, 2026, 'declined', 'recorded against the wrong company')
  check(
    'transport: a correction posts its answer and its reason',
    lastRequest?.url.endsWith('/service-renewals/2026/correction') === true &&
      JSON.stringify(lastRequest.body) ===
        JSON.stringify({ decision: 'declined', note: 'recorded against the wrong company' }),
  )

  // --- The refusals the panel and the configuration must not flatten ---

  stubFetch(409, { detail: 'already_closed' })
  const closed = await captureError(() => recordRenewalDecision(ALPHA_ID, 2026, 'confirmed', null))
  const closedNotice = renderToStaticMarkup(<RefusalNotice error={closed} />)
  contains('refusal/already_closed', closedNotice, 'already_closed')
  contains('refusal/already_closed', closedNotice, 'Correct the decision')

  stubFetch(409, { detail: 'payment_not_received: 2026' })
  const gate = await captureError(() => recordRenewalPayment(ALPHA_ID, 2026, null))
  const gateNotice = renderToStaticMarkup(<RefusalNotice error={gate} />)
  contains('refusal/payment_not_received', gateNotice, 'payment_not_received: 2026')
  contains('refusal/payment_not_received', gateNotice, 'record the fee')

  stubFetch(422, {
    detail: [
      'milestones.continuation_request_days_before: must be greater than data_confirmation_days_before',
      'milestones.reminder_cadence_days: must be strictly increasing',
    ],
  })
  const invalidBlock = await captureError(() =>
    saveTenantConfig({
      naming_templates: tenantConfig.naming_templates,
      milestones: { ...tenantConfig.milestones, continuation_request_days_before: 10 },
    }),
  )
  check(
    'transport: a 422’s violation list is kept as a list, not one line',
    invalidBlock instanceof ApiError &&
      invalidBlock.violations.length === 2 &&
      invalidBlock.detail.includes('must be strictly increasing'),
  )
  const invalidBlockNotice = renderToStaticMarkup(<RefusalNotice error={invalidBlock} />)
  contains(
    'refusal/milestone_violations',
    invalidBlockNotice,
    'milestones.continuation_request_days_before: must be greater than data_confirmation_days_before',
  )
  contains(
    'refusal/milestone_violations',
    invalidBlockNotice,
    'milestones.reminder_cadence_days: must be strictly increasing',
  )

  stubFetch(409, {
    detail:
      'tenant_config_invalid: milestones.client_reply_days_before: must not exceed 28 (NAR1_FILING_WINDOW_DAYS - minimum_reply_window_days)',
  })
  const unreadable = await captureError(() => fetchTenantConfig())
  const unreadableNotice = renderToStaticMarkup(<RefusalNotice error={unreadable} />)
  contains('refusal/tenant_config_invalid', unreadableNotice, 'tenant_config_invalid')
  contains(
    'refusal/tenant_config_invalid',
    unreadableNotice,
    'milestones.client_reply_days_before: must not exceed 28 (NAR1_FILING_WINDOW_DAYS - minimum_reply_window_days)',
  )
  contains('refusal/tenant_config_invalid', unreadableNotice, 'with one exception, the optional send_modes block')

  stubFetch(422, {
    detail: [{ type: 'string_too_long', loc: ['body', 'note'], msg: 'String should have at most 500 characters' }],
  })
  const longNote = await captureError(() => recordRenewalDecision(ALPHA_ID, 2026, 'declined', 'x'.repeat(501)))
  check(
    'transport: a body-level 422 reads its field and message',
    longNote instanceof ApiError && longNote.detail === 'note: String should have at most 500 characters',
  )

  // --- The change-event reads and writes ---

  stubFetch(200, changeEventQueue)
  const queue = await fetchChangeEventQueue()
  check(
    'transport: the queue read is GET /api/v1/change-events',
    lastRequest?.url === '/api/v1/change-events' && lastRequest.method === 'GET',
  )
  check(
    'transport: the queue parses its company and window',
    queue.length === 3 &&
      queue[0].company_name === 'Alpha Traders Limited' &&
      queue[0].change_window.is_late === true &&
      queue[1].change_window.days_remaining === 0,
  )

  stubFetch(200, companyChangeEvents)
  const events = await fetchCompanyChangeEvents(ALPHA_ID)
  check(
    'transport: the company read is company-scoped',
    lastRequest?.url === `/api/v1/companies/${ALPHA_ID}/change-events` && lastRequest.method === 'GET',
  )
  check(
    'transport: the company read carries the version history and the window',
    events[0].history.length === 1 &&
      events[0].history[0].version_no === 1 &&
      events[0].change_window.deadline === '2026-09-25',
  )

  const appointmentBody = {
    type: 'director_appointment' as const,
    effective_date: '2026-09-10',
    effective_date_text: null,
    informed_date: null,
    payload: {
      appointment: {
        officer: { role: 'director' as const, name_en: 'Wong Ka Ming', appointment_date: '2026-09-10' },
      },
    },
  }
  stubFetch(201, companyChangeEvents[0])
  const created = await createChangeEvent(ALPHA_ID, appointmentBody)
  check(
    'transport: the manual entry posts the type, dates and payload to the company’s own path',
    lastRequest?.url === `/api/v1/companies/${ALPHA_ID}/change-events` &&
      lastRequest.method === 'POST' &&
      JSON.stringify(lastRequest.body) === JSON.stringify(appointmentBody),
  )
  check('transport: the entry echo parses', created.version_no === 2 && created.status === 'under_review')

  stubFetch(200, changeEventTransitions)
  const allowed = await fetchChangeEventTransitions(changeEventTransitions.event_id)
  check(
    'transport: the transitions read is the event’s id-scoped path',
    lastRequest?.url === `/api/v1/change-events/${changeEventTransitions.event_id}/transitions` &&
      lastRequest.method === 'GET',
  )
  check(
    'transport: the transitions read parses every offered action',
    allowed.actions.length === 6 &&
      allowed.actions[2].action === 'reject' &&
      allowed.actions[2].rationale_required === true,
  )

  stubFetch(200, companyChangeEvents[0])
  await postChangeEventTransition(changeEventTransitions.event_id, {
    action: 'reject',
    rationale: 'the client changed their mind',
  })
  check(
    'transport: a rationale-required action posts the action and the reason',
    lastRequest?.url === `/api/v1/change-events/${changeEventTransitions.event_id}/transitions` &&
      lastRequest.method === 'POST' &&
      JSON.stringify(lastRequest.body) ===
        JSON.stringify({ action: 'reject', rationale: 'the client changed their mind' }),
  )

  stubFetch(200, companyChangeEvents[0])
  await postChangeEventTransition(changeEventTransitions.event_id, {
    action: 'edit',
    type: 'director_appointment',
    payload: { appointment: { officer: { role: 'director', name_en: 'Wong Ka Ming', appointment_date: '2026-09-10' } } },
  })
  check(
    'transport: an edit posts only the fields it states',
    lastRequest?.body !== undefined &&
      !('informed_date' in (lastRequest.body as Record<string, unknown>)) &&
      !('effective_date' in (lastRequest.body as Record<string, unknown>)),
  )

  stubFetch(200, changeEventAssessments)
  const assessments = await fetchChangeEventAssessments(changeEventTransitions.event_id)
  check(
    'transport: the assessments read is the event’s id-scoped path',
    lastRequest?.url === `/api/v1/change-events/${changeEventTransitions.event_id}/assessments` &&
      lastRequest.method === 'GET' &&
      assessments.length === 0,
  )

  // --- The change-event refusals, shown as the API’s own words ---

  stubFetch(409, { detail: 'transition_not_allowed' })
  const staleAction = await captureError(() =>
    postChangeEventTransition(changeEventTransitions.event_id, { action: 'confirm' }),
  )
  check(
    'transport: a refused transition comes back as the API’s own 409 code',
    staleAction instanceof ApiError && staleAction.status === 409 && staleAction.detail === 'transition_not_allowed',
  )
  const staleNotice = renderToStaticMarkup(<RefusalNotice error={staleAction} />)
  contains('refusal/transition_not_allowed', staleNotice, 'transition_not_allowed')
  contains('refusal/transition_not_allowed', staleNotice, 'reload')

  stubFetch(422, { detail: 'rationale_required' })
  const noReason = await captureError(() =>
    postChangeEventTransition(changeEventTransitions.event_id, { action: 'client_withdraw' }),
  )
  const noReasonNotice = renderToStaticMarkup(<RefusalNotice error={noReason} />)
  contains('refusal/rationale_required', noReasonNotice, 'rationale_required')
  contains('refusal/rationale_required', noReasonNotice, 'reason')

  stubFetch(422, {
    detail:
      'change_event_invalid: payload.appointment.officer.role: not one of company_secretary, director, reserve_director; ' +
      'payload.appointment.officer: needs one of name_en, name_zh, surname_en, other_names_en',
  })
  const invalidEvent = await captureError(() => createChangeEvent(ALPHA_ID, appointmentBody))
  const invalidNotice = renderToStaticMarkup(<RefusalNotice error={invalidEvent} />)
  contains('refusal/change_event_invalid', invalidNotice, 'change_event_invalid')
  contains(
    'refusal/change_event_invalid',
    invalidNotice,
    'payload.appointment.officer.role: not one of company_secretary, director, reserve_director',
  )
  contains(
    'refusal/change_event_invalid',
    invalidNotice,
    'payload.appointment.officer: needs one of name_en, name_zh, surname_en, other_names_en',
  )

  stubFetch(422, {
    detail: 'protected_class_unavailable: payload.appointment.officer.hkid_full',
  })
  const protectedClass = await captureError(() => createChangeEvent(ALPHA_ID, appointmentBody))
  const protectedNotice = renderToStaticMarkup(<RefusalNotice error={protectedClass} />)
  contains('refusal/protected_class_unavailable', protectedNotice, 'protected_class_unavailable')
  contains('refusal/protected_class_unavailable', protectedNotice, 'payload.appointment.officer.hkid_full')

  stubFetch(409, { detail: 'change_event_conflict' })
  const conflicting = await captureError(() =>
    postChangeEventTransition(changeEventTransitions.event_id, {
      action: 'edit',
      payload: { appointment: { officer: { role: 'director', name_en: 'Wong Ka Ming', appointment_date: '2026-09-10' } } },
    }),
  )
  const conflictNotice = renderToStaticMarkup(<RefusalNotice error={conflicting} />)
  contains('refusal/change_event_conflict', conflictNotice, 'change_event_conflict')
  contains('refusal/change_event_conflict', conflictNotice, 'retry')

  stubFetch(409, { detail: 'statement_not_confirmed' })
  const unconfirmedExport = await captureError(() => exportDraft(draftDetail.id, 'paper'))
  const unconfirmedNotice = renderToStaticMarkup(<RefusalNotice error={unconfirmedExport} />)
  contains('refusal/statement_not_confirmed', unconfirmedNotice, 'statement_not_confirmed')
  contains('refusal/statement_not_confirmed', unconfirmedNotice, 'approve it with the confirmation recorded first')

  // --- The officer, member and draft endpoints -----------------------------

  stubFetch(200, officers)
  const officerList = await fetchOfficers(ALPHA_ID)
  check(
    'transport: the officer read is the company-scoped path and parses',
    lastRequest?.url === `/api/v1/companies/${ALPHA_ID}/officers` &&
      lastRequest.method === 'GET' &&
      officerList.length === 3 &&
      officerList[0].person.name_en === 'Wong Ka Ming',
  )
  stubFetch(200, officers)
  await fetchOfficers(ALPHA_ID, '2026-09-28')
  check(
    'transport: the officer as-of read carries the date',
    lastRequest?.url === `/api/v1/companies/${ALPHA_ID}/officers?as_of=2026-09-28`,
  )

  const officerCreateBody = {
    person: { person_type: 'natural_person' as const, name_en: 'Wong Ka Ming' },
    role: 'company_secretary' as const,
    capacity: ['company_secretary'],
    valid_from: '2026-09-28',
    correspondence_address: {
      flat: 'FLAT 12A',
      building: 'HARBOUR VIEW COURT',
      street: '88 CONNAUGHT ROAD WEST',
      district: 'SHEUNG WAN',
    },
  }
  stubFetch(201, officers[0])
  const createdOfficer = await createOfficer(ALPHA_ID, officerCreateBody)
  check(
    'transport: an appointment create posts the person inline and the office',
    lastRequest?.url === `/api/v1/companies/${ALPHA_ID}/officers` &&
      lastRequest.method === 'POST' &&
      JSON.stringify(lastRequest.body) === JSON.stringify(officerCreateBody) &&
      createdOfficer.role === 'company_secretary',
  )

  stubFetch(200, officers[1])
  await updateOfficer(officers[1].id, { valid_to: null, alternate_to: 'Wong Ka Ming' })
  check(
    'transport: the appointment patch is id-scoped and states only what changed',
    lastRequest?.url === `/api/v1/officers/${officers[1].id}` &&
      lastRequest.method === 'PATCH' &&
      JSON.stringify(lastRequest.body) ===
        JSON.stringify({ valid_to: null, alternate_to: 'Wong Ka Ming' }),
  )

  stubFetch(204, null)
  const removedOfficer = await deleteOfficer(officers[1].id)
  check(
    'transport: a removal answers 204 and the client does not parse a body',
    lastRequest?.url === `/api/v1/officers/${officers[1].id}` &&
      lastRequest.method === 'DELETE' &&
      removedOfficer === undefined,
  )

  stubFetch(200, shareholdings)
  const holdingList = await fetchShareholdings(ALPHA_ID)
  check(
    'transport: the shareholding read is the company-scoped path and keeps money as strings',
    lastRequest?.url === `/api/v1/companies/${ALPHA_ID}/shareholdings` &&
      lastRequest.method === 'GET' &&
      holdingList[0].amount_paid === '500.00' &&
      holdingList[0].percentage_held === '33.33',
  )
  stubFetch(201, shareholdings[0])
  await createShareholding(ALPHA_ID, {
    person: { person_type: 'natural_person', name_en: 'Chan Tai Man' },
    class_name: 'Ordinary',
    shares_held: 500,
    amount_paid: '500.00',
    valid_from: '2026-09-28',
  })
  check(
    'transport: a holding create posts the member position',
    lastRequest?.url === `/api/v1/companies/${ALPHA_ID}/shareholdings` &&
      lastRequest.method === 'POST' &&
      JSON.stringify(lastRequest.body) ===
        JSON.stringify({
          person: { person_type: 'natural_person', name_en: 'Chan Tai Man' },
          class_name: 'Ordinary',
          shares_held: 500,
          amount_paid: '500.00',
          valid_from: '2026-09-28',
        }),
  )
  stubFetch(200, shareholdings[0])
  await updateShareholding(shareholdings[0].id, { remarks: null })
  check(
    'transport: the holding patch is id-scoped, with an explicit null clearing',
    lastRequest?.url === `/api/v1/shareholdings/${shareholdings[0].id}` &&
      lastRequest.method === 'PATCH' &&
      JSON.stringify(lastRequest.body) === JSON.stringify({ remarks: null }),
  )
  stubFetch(204, null)
  const removedHolding = await deleteShareholding(shareholdings[0].id)
  check(
    'transport: a holding removal answers 204 as well',
    lastRequest?.url === `/api/v1/shareholdings/${shareholdings[0].id}` &&
      lastRequest.method === 'DELETE' &&
      removedHolding === undefined,
  )

  stubFetch(201, draftDetail)
  const generated = await generateDraft(ALPHA_ID, {
    form_type: 'nar1',
    target_date: '2026-09-30',
  })
  check(
    'transport: generation posts the form and the target date',
    lastRequest?.url === `/api/v1/companies/${ALPHA_ID}/drafts` &&
      lastRequest.method === 'POST' &&
      JSON.stringify(lastRequest.body) ===
        JSON.stringify({ form_type: 'nar1', target_date: '2026-09-30' }) &&
      generated.version_no === 2,
  )
  stubFetch(200, companyDrafts)
  const draftList = await fetchCompanyDrafts(ALPHA_ID)
  check(
    'transport: the draft list is the company-scoped read',
    lastRequest?.url === `/api/v1/companies/${ALPHA_ID}/drafts` &&
      lastRequest.method === 'GET' &&
      draftList.length === 2 &&
      draftList[0].status === 'generated',
  )
  stubFetch(200, draftDetail)
  const oneDraft = await fetchDraft(draftDetail.id)
  check(
    'transport: the draft read is id-scoped and carries the documents',
    lastRequest?.url === `/api/v1/drafts/${draftDetail.id}` &&
      lastRequest.method === 'GET' &&
      oneDraft.form_version.specification_no === '2/2025' &&
      oneDraft.field_provenance['12A.name'] !== undefined,
  )
  stubFetch(200, draftTransitions)
  const offers = await fetchDraftTransitions(draftDetail.id)
  check(
    'transport: the transitions read is the draft’s id-scoped allowed-actions authority',
    lastRequest?.url === `/api/v1/drafts/${draftDetail.id}/transitions` &&
      lastRequest.method === 'GET' &&
      offers.actions.length === 2 &&
      offers.actions[0].action === 'edit',
  )
  stubFetch(200, draftDetail)
  await postDraftTransition(draftDetail.id, {
    action: 'edit',
    filing_metadata: { return: { fs_period_end: '2026-03-31' } },
    private_company_statement: true,
  })
  check(
    'transport: an edit posts the stated metadata and the statement only',
    lastRequest?.url === `/api/v1/drafts/${draftDetail.id}/transitions` &&
      lastRequest.method === 'POST' &&
      JSON.stringify(lastRequest.body) ===
        JSON.stringify({
          action: 'edit',
          filing_metadata: { return: { fs_period_end: '2026-03-31' } },
          private_company_statement: true,
        }),
  )

  // The form's own body, built from the draft it edits: a no-op states no
  // fields, so the API's refusal is what the operator meets; a changed field
  // posts the document carrying it.
  stubFetch(422, { detail: 'edit: nothing to change' })
  const noOpPosted = await captureError(() =>
    postDraftTransition(draftDetail.id, draftEditRequest(draftDetail, metadataForm)),
  )
  check(
    'transport: a no-op edit states no fields, and the API refuses it in its own words',
    lastRequest?.url === `/api/v1/drafts/${draftDetail.id}/transitions` &&
      lastRequest.method === 'POST' &&
      JSON.stringify(lastRequest.body) === JSON.stringify({ action: 'edit' }) &&
      noOpPosted instanceof ApiError &&
      noOpPosted.status === 422 &&
      noOpPosted.detail === 'edit: nothing to change',
  )
  const noOpNotice = renderToStaticMarkup(<RefusalNotice error={noOpPosted} />)
  contains('refusal/edit_nothing_to_change', noOpNotice, 'edit: nothing to change')

  stubFetch(200, draftDetail)
  await postDraftTransition(
    draftDetail.id,
    draftEditRequest(draftDetail, { ...metadataForm, fsPeriodEnd: '2026-04-30' }),
  )
  const changedBody = (lastRequest?.body ?? {}) as {
    filing_metadata?: { return?: { fs_period_end?: string } }
  }
  check(
    'transport: a changed field posts the document with the changed value in it',
    JSON.stringify(Object.keys(changedBody)) === JSON.stringify(['action', 'filing_metadata']) &&
      changedBody.filing_metadata?.return?.fs_period_end === '2026-04-30',
  )

  // --- The draft refusals, shown as the API’s own words ---

  stubFetch(403, { detail: 'automation_denied: service_not_included' })
  const denied = await captureError(() =>
    generateDraft(ALPHA_ID, { form_type: 'nar1', target_date: '2026-09-30' }),
  )
  check(
    'transport: a denied generation is the gate’s own 403 code and reason',
    denied instanceof ApiError &&
      denied.status === 403 &&
      denied.detail === 'automation_denied: service_not_included',
  )
  const deniedNotice = renderToStaticMarkup(<RefusalNotice error={denied} />)
  contains('refusal/automation_denied', deniedNotice, 'automation_denied')
  contains('refusal/automation_denied', deniedNotice, 'service_not_included')
  contains('refusal/automation_denied', deniedNotice, 'does not include it')

  stubFetch(422, {
    detail:
      'filing_metadata_invalid: return.fs_period_end: not an ISO date; ' +
      'filing_metadata_invalid: presentor.vat',
  })
  const badMetadata = await captureError(() =>
    postDraftTransition(draftDetail.id, { action: 'edit', filing_metadata: {} }),
  )
  const badNotice = renderToStaticMarkup(<RefusalNotice error={badMetadata} />)
  contains('refusal/filing_metadata_invalid', badNotice, 'filing_metadata_invalid')
  contains('refusal/filing_metadata_invalid', badNotice, 'return.fs_period_end: not an ISO date')
  contains('refusal/filing_metadata_invalid', badNotice, 'presentor.vat')
  notContains('refusal/filing_metadata_invalid', badNotice, 'filing_metadata_invalid: presentor.vat')

  stubFetch(409, { detail: 'transition_not_allowed' })
  const staleDraft = await captureError(() =>
    postDraftTransition(draftDetail.id, { action: 'mark_under_review' }),
  )
  const staleDraftNotice = renderToStaticMarkup(<RefusalNotice error={staleDraft} />)
  contains('refusal/draft_transition_not_allowed', staleDraftNotice, 'transition_not_allowed')
  contains('refusal/draft_transition_not_allowed', staleDraftNotice, 'reload')

  // --- The signature upload and the export: the two non-transition writes ---

  stubFetch(200, { ...draftDetail, status: 'signature_received', signature_status: 'signature_received' })
  const signedFile = new File(['%PDF-1.4 signed'], 'nar1-signed.pdf', {
    type: 'application/pdf',
  })
  const uploaded = await uploadSignedDraft(draftDetail.id, signedFile)
  check(
    'transport: the signed upload posts the file itself, raw, to the signature route',
    lastRequest?.url === `/api/v1/drafts/${draftDetail.id}/signature` &&
      lastRequest.method === 'POST' &&
      lastRequest.contentType === 'application/pdf' &&
      lastRequest.body === signedFile,
  )
  check('transport: the signed upload answers with the moved draft', uploaded.status === 'signature_received')

  stubFetch(413, { detail: 'file too large' })
  const tooLarge = await captureError(() => uploadSignedDraft(draftDetail.id, signedFile))
  check(
    'transport: an over-cap upload is the route’s own refusal',
    tooLarge instanceof ApiError && tooLarge.status === 413,
  )

  stubFetch(201, filingPackage)
  const exported = await exportDraft(draftDetail.id, 'paper')
  check(
    'transport: the export posts the chosen mode to the exports route',
    lastRequest?.url === `/api/v1/drafts/${draftDetail.id}/exports` &&
      lastRequest.method === 'POST' &&
      JSON.stringify(lastRequest.body) === JSON.stringify({ mode: 'paper' }),
  )
  check(
    'transport: the export answers with the FilingPackage',
    exported.document_id === filingPackage.document_id &&
      exported.envelope.checklist_document_id === filingPackage.envelope.checklist_document_id,
  )

  stubFetch(422, { detail: 'statement_confirmation_required' })
  const unconfirmedApproval = await captureError(() =>
    postDraftTransition(draftDetail.id, { action: 'approve', confirm_statement: true }),
  )
  const unconfirmedApprovalNotice = renderToStaticMarkup(
    <RefusalNotice error={unconfirmedApproval} />,
  )
  contains(
    'refusal/statement_confirmation_required',
    unconfirmedApprovalNotice,
    'statement_confirmation_required',
  )
  contains(
    'refusal/statement_confirmation_required',
    unconfirmedApprovalNotice,
    'tick the confirmation and approve again',
  )
}

await checkTransport()

// --- The hash route ------------------------------------------------------

check('routes: #/client-actions is its own view', parseRoute('#/client-actions').view === 'client-actions')
check('routes: #/calendar is unchanged', parseRoute('#/calendar').view === 'calendar')
check('routes: an unknown hash falls back to the dashboard', parseRoute('#/nope').view === 'dashboard')
check('routes: #/change-events is the review queue', parseRoute('#/change-events').view === 'change-events')
check(
  'routes: the event detail carries the company and the event id',
  JSON.stringify(
    parseRoute(
      `#/companies/${companyChangeEvents[0].company_id}/change-events/${changeEventTransitions.event_id}`,
    ),
  ) ===
    JSON.stringify({
      view: 'change-event',
      companyId: companyChangeEvents[0].company_id,
      eventId: changeEventTransitions.event_id,
    }),
)
check(
  'routes: the plain company hash is not swallowed by the event route',
  parseRoute(`#/companies/${companyChangeEvents[0].company_id}`).view === 'company',
)
check(
  'routes: an empty event id is not a detail route',
  parseRoute(`#/companies/${companyChangeEvents[0].company_id}/change-events/`).view === 'company',
)
check(
  'routes: the draft detail carries the company and the draft id',
  JSON.stringify(parseRoute(`#/companies/${draftDetail.company_id}/drafts/${draftDetail.id}`)) ===
    JSON.stringify({
      view: 'draft',
      companyId: draftDetail.company_id,
      draftId: draftDetail.id,
    }),
)
check(
  'routes: the plain company hash is not swallowed by the draft route',
  parseRoute(`#/companies/${draftDetail.company_id}`).view === 'company',
)
check(
  'routes: an empty draft id is not a detail route',
  parseRoute(`#/companies/${draftDetail.company_id}/drafts/`).view === 'company',
)
check(
  'routes: the draft link is the company-scoped hash the parser reads back',
  hrefForDraft(draftDetail.company_id, draftDetail.id) ===
    `#/companies/${draftDetail.company_id}/drafts/${draftDetail.id}` &&
    JSON.stringify(
      parseRoute(hrefForDraft(draftDetail.company_id, draftDetail.id)),
    ) ===
      JSON.stringify({
        view: 'draft',
        companyId: draftDetail.company_id,
        draftId: draftDetail.id,
      }),
)

// --- Report --------------------------------------------------------------

console.log(results.join('\n'))
console.log(`fixtures: ${fileURLToPath(new URL('./fixtures.json', import.meta.url))}`)
if (failures > 0) {
  throw new Error(`${failures} of ${results.length} verification checks failed`)
}
console.log(`ALL ${results.length} CHECKS PASSED`)
