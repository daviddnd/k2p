/**
 * Hand-written mirrors of the execution plane's response shapes — no codegen.
 *
 * Sources, so a reader can check any field against the backend:
 *   - `backend/app/api/auth.py` + `backend/app/api/identity.py` (login/user)
 *   - `backend/app/api/compliance.py` (dashboard + calendar)
 *   - `backend/app/api/companies.py` (company statics + master data)
 *   - `backend/app/api/prior_filing.py` (import batches, records, assessments)
 *   - `backend/app/api/calendar_sync.py` (feed credentials)
 *   - `backend/app/api/tenant_config.py` (the naming templates + milestone block)
 *   - `backend/app/api/service_renewals.py` (the renewal record and the panel)
 *   - `backend/app/api/change_events.py` (the change events, the review queue,
 *     the allowed-actions read and the assessments)
 *   - `backend/app/api/officers.py` (the officer appointments and the
 *     shareholdings, each with its person snapshot)
 *   - `backend/app/api/drafts.py` (the drafts, their allowed actions and the
 *     filing-metadata transition body)
 *   - `backend/app/api/risk_acknowledgment.py` (the pre-use acknowledgment
 *     state: the served statement, its version, the acknowledged flag)
 *   - `backend/app/domain/compliance.py`, `app/domain/service_packages.py`,
 *     `app/domain/prior_filing.py`, `app/domain/milestones.py`,
 *     `app/domain/service_renewals.py`, `app/domain/change_events.py`,
 *     `app/domain/officers.py`, `app/domain/drafts.py` (the `risk`, `alert`,
 *     `automation`, `reason`, `status`, `shape`, milestone, confidence-band,
 *     officer/action/capacity, event-type, person-type, draft-status and
 *     draft-action vocabularies)
 *
 * Field names are the wire names: dates and timestamps arrive as ISO-8601
 * strings, ids as UUID strings. The calendar's `from` arrives under its
 * serialization alias (`CalendarResponse.from_` on the Python side).
 */

export type Role = 'administrator' | 'general'

export interface SessionUser {
  id: string
  tenant_id: string
  email: string
  display_name: string
  role: Role
  review_events: boolean
  approve_events: boolean
  review_drafts: boolean
  approve_drafts: boolean
  is_active: boolean
}

export interface LoginResponse {
  access_token: string
  token_type: string
  expires_at: string
  user: SessionUser
  /** False whenever the session owes a TOTP step (phase 7, scope §8; slice ①):
   * an Administrator still owing the challenge or forced enrollment, or an
   * enrolled staff member's fresh session awaiting the challenge; the
   * session store gates on it. */
  mfa_required: boolean
  /** Which of the two MFA steps the session owes, when `mfa_required`. */
  enrollment_state: MfaEnrollmentState
}

/** The MFA vocabulary the login response and the MFA routes share. */
export type MfaEnrollmentState = 'enrolled' | 'pending' | 'not_required'

/** `GET /auth/mfa/status` — the caller's own MFA facts. */
export interface MfaStatus {
  enrollment_state: MfaEnrollmentState
  mfa_verified: boolean
}

/** `POST /auth/mfa/enrollment` — the one response ever carrying the secret. */
export interface MfaEnrollmentBegin {
  secret: string
  uri: string
  enrollment_state: 'pending'
}

/** `POST /auth/mfa/enrollment/complete` — the confirming code sealed it; the
 * session owes the challenge next (the marker moves, the API is not asked to
 * verify anything here). */
export interface MfaEnrollmentComplete {
  enrollment_state: 'enrolled'
}

// --- The pre-use risk acknowledgment (the 2026-10-04 founder rulings) --------

/**
 * `GET`/`POST /api/v1/risk-acknowledgment` — the served statement and the
 * tenant's acknowledgment state. The statement text is the disclosure itself
 * (ruling R5: the UI renders it verbatim, never a paraphrase); the version
 * pins the wording, and an acceptance counts only while it carries the served
 * version — a wording bump answers `acknowledged: false` again until an
 * Administrator confirms the revised text. The write is Administrator-only
 * and idempotent.
 */
export interface RiskAcknowledgment {
  statement_version: string
  statement_text: string
  acknowledged: boolean
}

export type RiskLevel = 'green' | 'amber' | 'red'

export type AlertKind = 'alert_30d' | 'alert_15d' | 'alert_7d'

export type ComplianceEventKind = 'nar1_due' | 'change_window_15d' | AlertKind

export interface LateFeeExposure {
  /** The registration fee in force for a delivery today, in whole HKD. */
  fee_hkd: number
  /** False while the 42-day on-time window stands; true in a late band. */
  late: boolean
}

export interface CalendarEntry {
  company_id: string
  br_number: string
  company_name: string | null
  kind: ComplianceEventKind
  /** `YYYY-MM-DD` */
  due_date: string
  days_remaining: number
  risk: RiskLevel
  /** The deepest 30/15/7-day threshold crossed; null while > 30 days out. */
  alert: AlertKind | null
  overdue: boolean
  late_fee_exposure: LateFeeExposure
}

export interface NotGenerated {
  company_id: string
  company_type: string
  reason: string
}

export interface CalendarResponse {
  /** The requested range, echoed. */
  from: string
  to: string
  as_of: string
  entries: CalendarEntry[]
  not_generated: NotGenerated[]
}

export interface PortfolioOverview {
  total_companies: number
  due_within_30_days: number
  due_within_15_days: number
  due_within_7_days: number
  overdue: number
  /** The late fees the overdue deadlines would pay if filed today, in HKD. */
  late_fee_exposure_hkd: number
}

export interface PendingIngestionCompany {
  company_id: string
  company_name: string | null
  pending_records: number
}

export interface PendingIngestion {
  pending_records: number
  companies: PendingIngestionCompany[]
}

/** One company with change events awaiting staff review (`pending`/`under_review`). */
export interface PendingEventReviewCompany {
  company_id: string
  company_name: string | null
  pending_events: number
}

/**
 * The pending-event-review widget, in counts: the same set the cross-company
 * queue (`GET /api/v1/change-events`) lists, so the total is that queue's
 * length and the two cannot disagree.
 */
export interface PendingEventReview {
  pending_events: number
  companies: PendingEventReviewCompany[]
}

/** One company with drafts awaiting staff review (`generated`/`under_review`). */
export interface PendingDraftReviewCompany {
  company_id: string
  company_name: string | null
  pending_drafts: number
}

/**
 * The pending-draft-review widget, in counts: a draft is counted once, on its
 * **current version** (the highest `version_no` per lineage), from the set
 * `app.domain.drafts.AWAITING_REVIEW_STATUSES` names — so a regenerated or
 * invalidated lineage is never counted off an earlier row.
 */
export interface PendingDraftReview {
  pending_drafts: number
  companies: PendingDraftReviewCompany[]
}

export interface CompanyCard {
  company_id: string
  br_number: string
  /** Nullable: most companies have none recorded (NAR1 has no CR box). */
  cr_number: string | null
  company_name: string | null
  /** `active | dormant | striking_off | dissolved`, or null when unrecorded. */
  status: string | null
  next_deadline: CalendarEntry | null
  pending_ingestion_records: number
}

/** The §2.2 capability vocabulary reused as automation types. */
export type AutomationType =
  | 'calendar_sync'
  | 'automated_prefill'
  | 'email_ingestion'
  | 'document_extraction'
  | 'ai_reply_parsing'
  | 'confirmation_generation'
  | 'workflow_orchestration'

/** The automation gate's own refusal vocabulary (contract §5.1/§5.1.3). */
export type GatingReason =
  | 'tier_capability_missing'
  | 'no_assignment'
  | 'assignment_ended'
  | 'out_of_coverage'
  | 'service_not_included'

export interface ServiceGap {
  company_id: string
  automation: AutomationType
  reason: GatingReason
}

/** One `sent` confirmation request awaiting reply (contract §5.1.12). */
export interface PendingConfirmationRequest {
  confirmation_request_id: string
  days_elapsed: number
  reminders_sent: number
  cadence_total: number
  next_reminder_at: string | null
  exhausted: boolean
  automation_blocked: GatingReason | null
}

export interface PendingConfirmationsCompany {
  company_id: string
  company_name: string | null
  requests: PendingConfirmationRequest[]
}

export interface PendingConfirmations {
  companies: PendingConfirmationsCompany[]
}

export interface DashboardResponse {
  as_of: string
  window_from: string
  window_to: string
  portfolio: PortfolioOverview
  upcoming_deadlines: CalendarEntry[]
  risk_alerts: CalendarEntry[]
  pending_ingestion: PendingIngestion
  pending_event_review: PendingEventReview
  pending_draft_review: PendingDraftReview
  pending_confirmations: PendingConfirmations
  company_cards: CompanyCard[]
  service_gaps: ServiceGap[]
  not_generated: NotGenerated[]
}

export type CompanyType = 'private' | 'public' | 'company_limited_by_guarantee'

/** `GET /api/v1/companies/{id}` — the anchor statics (no name; it is a fact). */
export interface Company {
  id: string
  tenant_id: string
  cr_number: string | null
  br_number: string
  date_of_incorporation: string
  anniversary_date: string
  type: CompanyType
  ai_disabled: boolean
}

/** One resolved fact group: its payload plus the effective date it resolved to. */
export interface NameFacts {
  effective_date: string
  name_en: string | null
  name_zh: string | null
  business_name: string | null
}

export interface StatusFacts {
  effective_date: string
  status: string
}

/** `GET /api/v1/companies/{id}/master-data` — statics plus facts as of a date. */
export interface CompanyMasterData {
  company: Company
  as_of: string
  name: NameFacts | null
  registered_office: Record<string, string | null> | null
  status: StatusFacts | null
  business_nature: Record<string, string | null> | null
  contact: Record<string, string | null> | null
  filing: Record<string, string | number | null> | null
  records_kept: Record<string, unknown> | null
  share_capital_classes: Record<string, unknown> | null
}

/** Which digital shape a prior filing arrived in (slice E1; `needs_ocr` is an
 * outcome, not a failure: the scan path reads it, or says why it could not). */
export type ImportBatchShape = 'acroform' | 'text_layer' | 'needs_ocr'

/** `pending` proposals are the work; only `confirmed` becomes master data. */
export type RecordStatus = 'pending' | 'confirmed' | 'rejected'

/** The §2.7 routing band a confidence score falls into. */
export type ConfidenceBand = 'auto_accept' | 'staff_review' | 'client_clarification'

export interface ImportBatchRecord {
  id: string
  /** A master-data dot path (`company.registered_office.flat`). */
  target: string
  value: string
  confidence: number
  status: RecordStatus
  effective_date: string | null
  decided_by: string | null
  decided_at: string | null
}

export interface ExtractionAssessment {
  id: string
  subject_type: string
  subject_id: string
  component: string
  model: string
  field_scores: Record<string, number>
  signal_log: {
    shape?: string
    /** Per target: how it was read, then the signals that cap or explain it. */
    fields?: Record<string, string[]>
    /** Batch-level signals: the shape, then why nothing (or something) was read. */
    document?: string[]
  }
  overall_score: number | null
  routing: ConfidenceBand | null
  created_at: string
}

export interface ImportBatch {
  id: string
  company_id: string
  document_id: string
  form: string
  shape: ImportBatchShape
  created_by: string | null
  created_at: string
  records: ImportBatchRecord[]
  assessment: ExtractionAssessment | null
}

/** A run as the company's batch list carries it — counts, never the records. */
export interface ImportBatchSummary {
  id: string
  company_id: string
  document_id: string
  form: string
  shape: ImportBatchShape
  created_by: string | null
  created_at: string
  records_total: number
  records_pending: number
  records_confirmed: number
  records_rejected: number
}

/** A calendar feed credential. The token is deliberately not a field. */
export interface FeedCredential {
  id: string
  created_by: string | null
  created_at: string
  revoked_at: string | null
}

/** The create response: the subscription URL is returned here and nowhere else. */
export interface IssuedFeedCredential extends FeedCredential {
  subscription_url: string
}

// --- The calendar push (slice M8): the provider credential, the probe,
// --- the push status, and the static onboarding guide ----------------------

/** The calendar providers the push supports — the TCSP's own registration. */
export type CalendarProviderName = 'google' | 'microsoft'

/**
 * The provider credential's metadata; the client secret and refresh token
 * are deliberately not fields (they live in the ADR-0014 file store only).
 */
export interface ProviderCredential {
  connected: boolean
  credential_id: string | null
  provider: CalendarProviderName | null
  calendar_id: string | null
  probe_passed: boolean
  created_at: string | null
  updated_at: string | null
}

/** The credential form's body, exactly the create route's shape. */
export interface ProviderCredentialRequest {
  provider: CalendarProviderName
  client_id: string
  client_secret: string
  refresh_token: string
  calendar_id: string
  /** Microsoft's directory (tenant) id — microsoft only; refused for google. */
  directory_tenant_id?: string
}

/** One probe leg's outcome: its name, whether it passed, a bounded detail. */
export interface ProbeLeg {
  name: string
  ok: boolean
  detail: string | null
}

/** The probe's answer: every leg in order, and the flag the run left behind. */
export interface CalendarProbeResult {
  credential_id: string
  passed: boolean
  legs: ProbeLeg[]
}

/** One company's place in the pairing: `pushing`, `feed`, or `refused`. */
export interface CompanyPushStatus {
  company_id: string
  state: 'pushing' | 'feed' | 'refused'
  /** The §5.1 gate's own reason when the state is `refused`. */
  reason: string | null
}

/**
 * The tenant's delivery path plus one state per company. `mode` and
 * `probe_passed` travel together so an unprobed credential never reads
 * as pushing — the feed stays the honest default.
 */
export interface PushStatus {
  credential: ProviderCredential
  mode: 'push' | 'feed'
  probe_passed: boolean
  companies: CompanyPushStatus[]
}

/** One provider's static registration walk (ruling R6), Markdown text. */
export interface OnboardingGuide {
  provider: CalendarProviderName
  guide: string
}

// --- The tenant configuration and the client-actions panel (§5.1.9) ---------

/** The §5.3 document naming templates. `{Version}` is the `generated` pair's. */
export interface NamingTemplates {
  document: string
  generated: string
}

/**
 * The §2.9 milestone block: every offset is a count of days **back** from the
 * company's anniversary date, so a larger number is an earlier date. The
 * statutory 42-day NAR1 window is not one of these numbers — it stays the
 * margin behind the last milestone, and the API refuses a configuration that
 * would print CR's last day to a client.
 */
export interface MilestoneBlock {
  continuation_request_days_before: number
  data_confirmation_days_before: number
  client_reply_days_before: number
  signature_post_days_before: number
  minimum_reply_window_days: number
  /** Days *after* a request is sent — a different computation from the offsets. */
  reminder_cadence_days: number[]
  /** A closed one-value vocabulary; only `anniversary` is accepted. */
  anchor: string
}

/** The three blocks a stored configuration holds, as a block is named. */
export type ConfigBlock = 'naming_templates' | 'milestones' | 'send_modes'

/**
 * A stored configuration block this build could not read, and why. The
 * violations are the block's own rule's messages, field by field — never
 * client data — and they are what a replacement has to satisfy, not only a
 * diagnosis.
 */
export interface UnreadableBlock {
  block: ConfigBlock
  violations: string[]
}

/** `GET /api/v1/tenant-config` — the effective configuration, stored or shipped. */
export interface TenantConfig {
  naming_templates: NamingTemplates
  milestones: MilestoneBlock
  send_modes: SendModes
  /** Null when the tenant has never saved one: the shipped defaults stand. */
  updated_at: string | null
  /**
   * Empty when every stored block validated. A non-empty entry means the named
   * block's values **in this response** are the shipped defaults rather than
   * what the tenant stored, and the entry's violations are why — the read
   * that repairs the row is this one, so it does not fail for it.
   */
  unreadable_blocks: UnreadableBlock[]
}

/** The whole configuration as `PUT /api/v1/tenant-config` carries it. */
export interface TenantConfigRequest {
  naming_templates: NamingTemplates
  milestones: MilestoneBlock
  /**
   * Optional, and the one block a PUT may omit: absent means "keep what is
   * stored" (a client built before the block existed must not silently re-arm
   * automatic client sending). `government` admits `review_required` only.
   */
  send_modes?: SendModes
}

/**
 * Who releases a message, per destination (contract §5.3's send-mode block).
 * `review_required` drafts and holds; `automatic` releases without staff. The
 * shipped defaults are `automatic` for the client and `review_required` —
 * pinned — for a government filing destination, which admits nothing else.
 */
export interface SendModes {
  client: string
  government: string
}

/** The two answers a client can give (the request bodies' vocabulary). */
export type RenewalDecision = 'confirmed' | 'declined'

/**
 * One period's renewal record (§5.1.9): the continuation answer and the fee.
 * Nothing about money is on it — no amount, no currency, no invoice.
 */
export interface ServiceRenewal {
  id: string
  company_id: string
  /** The year the company's anniversary (made-up) date falls in. */
  anniversary_year: number
  /** `pending | confirmed | declined | payment_received` */
  status: string
  client_decided_at: string | null
  payment_received_at: string | null
  decided_by: string | null
  payment_by: string | null
  note: string | null
  created_at: string
}

/** The four client-facing milestones the configuration defines (§5.3). */
export type MilestoneKind =
  | 'continuation_request'
  | 'data_confirmation'
  | 'client_reply'
  | 'signature_post'

/** A milestone's state on the panel, as the backend derives it and no further. */
export type MilestoneState = 'pending' | 'due' | 'overdue' | 'done' | 'blocked'

/**
 * Which leg refused a `blocked` milestone, in the backend's own vocabulary:
 * the release gate's legs a period can be judged by,
 * `no_confirmation_request` for the reply milestone — a missing letter is a
 * precondition, not a gate — or `reminders_exhausted` when the request's
 * reminder chain ran out with the reply still missing (§5.1.12, slice R):
 * no automated reminder will follow, and staff contact the client by hand.
 * `form_not_covered` can never appear: a milestone names no form, and that leg
 * is judged at the download.
 */
export type MilestoneBlockReason =
  | 'no_assignment'
  | 'assignment_ended'
  | 'out_of_coverage'
  | 'continuation_not_recorded'
  | 'payment_not_received'
  | 'no_confirmation_request'
  | 'reminders_exhausted'

/** One milestone of one period, as of the panel read's day. */
export interface ClientActionMilestone {
  kind: MilestoneKind
  /** `YYYY-MM-DD` */
  date: string
  /** Negative once the day has passed (the compliance calendar's convention). */
  days_remaining: number
  state: MilestoneState
  /** The leg that refused; `null` whenever the milestone is not `blocked`. */
  blocked_reason: MilestoneBlockReason | null
}

/** One company's one anniversary period, with every configured milestone. */
export interface ClientActionPeriod {
  company_id: string
  br_number: string
  company_name: string | null
  anniversary_year: number
  /** `YYYY-MM-DD` — the made-up date, the anchor of every offset. */
  anniversary_date: string
  /** Null when no row was recorded for the period yet. */
  renewal_status: string | null
  milestones: ClientActionMilestone[]
}

/** `GET /api/v1/client-actions` — the panel, and the window it was computed over. */
export interface ClientActionsResponse {
  as_of: string
  due_within: number
  window_from: string
  window_to: string
  periods: ClientActionPeriod[]
}

// --- Communications (§2.9; slice M5b) ----------------------------------------

/**
 * One exchange as the company's communications list returns it — metadata
 * only, never a body (the text lives on the WORM document `raw_document_id`
 * points at, and is read through the document routes). An inbound row's
 * counterparty is `sender_address`; an outbound row's is `recipient_addresses`.
 */
export interface Communication {
  id: string
  company_id: string
  /** `inbound | outbound`. */
  direction: string
  /** `email` — v1.0's only channel (§2.5). */
  channel: string
  mailbox_message_id: string | null
  thread_id: string | null
  sender_address: string | null
  recipient_addresses: string | null
  /** The request an outbound row is the letter of; the request an inbound reply answers. */
  confirmation_request_id: string | null
  /** The detected reply language the parser fills; null until a parse has run. */
  language: string | null
  /** The newest parse's status (the grammar's six values); null until parsed. */
  parse_status: string | null
  /** The newest parse's routing band; null until parsed. */
  parse_routing: ConfidenceBand | null
  received_at: string
  created_at: string
  raw_document_id: string
}

/**
 * One parsed answer as `GET …/communications/{id}/parse` carries it: which
 * checklist fact, which answer. The client's supporting quote (the stored
 * `evidence_span`) never reaches the API — this shape has no field for it.
 */
export interface ParsedConfirmation {
  fact_id: string
  /** `confirmed | denied | not_addressed`. */
  answer: string
}

/**
 * The newest parse of one reply — status, language, routing, the checklist
 * answers and the proposed change-event ids. Metadata only: never an evidence
 * span, never the reply body. 404 `not_parsed` until a cycle has read the
 * reply (`POST /api/v1/mail/parse-replies` is the trigger).
 */
export interface ReplyParse {
  id: string
  communication_id: string
  confirmation_request_id: string | null
  /** The grammar's six: `confirmed_no_change | changes_reported | correction | withdrawal | unclear | off_topic`. */
  status: string
  /** `en | zh-hk | mixed`, or null when the parse carried none. */
  language: string | null
  routing: ConfidenceBand | null
  confirmations: ParsedConfirmation[]
  /** The change-event ids the parse proposed (empty for a withdrawal or a no-change answer). */
  event_ids: string[]
  created_at: string
}

// --- Change events (§2.6, §4.1; contract §5.1) ------------------------------

/** Where an event stands (§4.1). Nine values; every one is a table row. */
export type ChangeEventStatus =
  | 'pending'
  | 'client_reconfirming'
  | 'under_review'
  | 'reviewed'
  | 'confirmed'
  | 'applied_to_draft'
  | 'submitted'
  | 'withdrawn'
  | 'deleted'

/** What changed (§2.6). `company_name_change` is reserved for Wave 2/NNC2. */
export type ChangeEventType =
  | 'director_appointment'
  | 'director_cessation'
  | 'secretary_appointment'
  | 'secretary_cessation'
  | 'registered_office_change'
  | 'company_name_change'
  | 'other'

/** How the event got here (§2.6): a client communication, a document, staff. */
export type ChangeEventSourceType = 'communication' | 'document' | 'manual'

/** How a new event refers to an earlier one (§2.6). */
export type ChangeEventRelation = 'corrects' | 'withdraws' | 'supersedes'

/** Every action §4.1 names — the wire vocabulary, unwired members included. */
export type ChangeEventAction =
  | 'mark_under_review'
  | 'send_reconfirmation'
  | 'override'
  | 'client_reply'
  | 'mark_reviewed'
  | 'revert'
  | 'reject'
  | 'confirm'
  | 'apply'
  | 'submit'
  | 'supersede'
  | 'edit'
  | 'delete'
  | 'client_withdraw'

/** An officer's role (§2.4). `reserve_director` is NAR1's separate block. */
export type OfficerRole = 'company_secretary' | 'director' | 'reserve_director'

/** ND2A's capacity checkboxes, in the registry's own values. */
export type OfficerCapacity = 'company_secretary' | 'director' | 'alternate_director'

/** Why an officer left, as the ND2A cessation block asks. */
export type CessationReason = 'resignation_others' | 'deceased'

/**
 * One event's 15-day statutory window, counted as of the read's day:
 * `deadline` is `effective_date + 15`, `days_remaining` goes negative once
 * past, `is_late` is False on the deadline day itself, and `risk`/`alert` are
 * the compliance calendar's own 30/15/7 bands (amber inside 15 days, red
 * inside 7 or past). The history's version rows carry none.
 */
export interface ChangeWindow {
  deadline: string
  days_remaining: number
  is_late: boolean
  risk: RiskLevel
  alert: AlertKind | null
}

/** One version row of one event (§2.6). `history` entries are these. */
export interface ChangeEventVersion {
  id: string
  version_no: number
  company_id: string
  type: ChangeEventType
  status: ChangeEventStatus
  effective_date: string
  effective_date_text: string | null
  informed_date: string | null
  payload: Record<string, unknown>
  source_type: ChangeEventSourceType
  source_id: string | null
  related_event_id: string | null
  relation: ChangeEventRelation | null
  reviewed_by: string | null
  reviewed_at: string | null
  approved_by: string | null
  approved_at: string | null
  rationale: string | null
  created_by: string | null
  created_at: string
}

/**
 * One event as `GET /companies/{id}/change-events` returns it: the current
 * version flat, the versions before it (newest first), and the window.
 */
export interface ChangeEvent extends ChangeEventVersion {
  history: ChangeEventVersion[]
  change_window: ChangeWindow
}

/** One awaiting-review event as the cross-company queue lists it. */
export interface ChangeEventQueueItem extends ChangeEventVersion {
  br_number: string
  company_name: string | null
  change_window: ChangeWindow
}

/** One action the actor may perform now, as the transitions read offers it. */
export interface AllowedChangeEventAction {
  action: ChangeEventAction
  to_status: ChangeEventStatus
  rationale_required: boolean
}

/** `GET /change-events/{id}/transitions` — the §4.1 read for one actor. */
export interface ChangeEventTransitions {
  event_id: string
  company_id: string
  version_no: number
  status: ChangeEventStatus
  actions: AllowedChangeEventAction[]
}

/** The edit fields a revision may state; an absent one keeps, a null clears. */
export interface ChangeEventEditFields {
  type?: ChangeEventType
  effective_date?: string
  effective_date_text?: string | null
  informed_date?: string | null
  payload?: Record<string, unknown>
}

/** `POST /companies/{id}/change-events` — one hand-entered event. */
export interface CreateChangeEventRequest {
  type: ChangeEventType
  effective_date: string
  effective_date_text?: string | null
  informed_date?: string | null
  payload: Record<string, unknown>
}

/**
 * `POST /change-events/{id}/transitions` — `{action, rationale?, ...edit
 * fields}`. `rationale` is required by `reject` and `client_withdraw`; edit
 * fields may only accompany `edit`, and only the ones actually stated may
 * travel (each absent field keeps the current version's value).
 */
export interface TransitionRequest extends ChangeEventEditFields {
  action: ChangeEventAction
  rationale?: string | null
}

// --- Officers and shareholdings (§2.4; contract §5.1) -----------------------

/** §2.4's identity split: a natural person or a body corporate. */
export type PersonType = 'natural_person' | 'body_corporate'

/** One canonical name pair (§3.4): at least one language is stated. */
export interface NamePair {
  name_zh?: string | null
  name_en?: string | null
}

/**
 * Form note 20's either/or: a licence number, or not-required with a reason.
 * Both branches are stated; the domain refuses a body that says neither or
 * both (`tcsp_licence_invalid`).
 */
export interface TcspLicence {
  licence_no?: string | null
  not_required: boolean
  not_required_reason?: string | null
}

/** The §2.5 address as filed: flat, building, street, district; country optional. */
export interface Address {
  flat: string
  building: string
  street: string
  district: string
  country?: string | null
}

/** The person an appointment or a holding points at (§2.4 identity groups). */
export interface PersonSnapshot {
  id: string
  person_type: PersonType
  name_zh: string | null
  name_en: string | null
  surname_en: string | null
  other_names_en: string | null
  /** Name-history entries (`{name_zh, name_en}`); shape held by the domain. */
  previous_names: Array<Record<string, unknown>>
  aliases: Array<Record<string, unknown>>
  email: string | null
  br_number: string | null
  hkid_partial: string | null
  passport_partial: string | null
  passport_issuing_country_region: string | null
  /** `{licence_no, not_required, not_required_reason}` when one is stated. */
  tcsp_licence: Record<string, unknown> | null
}

/**
 * One appointment as filed, with its person snapshot.
 *
 * `valid_from`/`valid_to` are the service window: in office as of `as_of` ⟺
 * `valid_from <= as_of < valid_to`, `valid_to` being the cessation's
 * effective date (the first day out of office) and `null` open-ended.
 */
export interface OfficerAppointment {
  id: string
  company_id: string
  person_id: string
  role: OfficerRole
  capacity: OfficerCapacity[]
  alternate_to: string | null
  valid_from: string
  valid_to: string | null
  correspondence_address: Address | null
  registered_office_address: Address | null
  created_by: string | null
  created_at: string
  updated_at: string
  person: PersonSnapshot
}

/**
 * One member position, with its person snapshot. `percentage_held`,
 * `amount_paid` and `amount_unpaid` are strings — `Numeric` values cross as
 * strings in both directions (the house convention), so a cent never rounds
 * through a float.
 */
export interface Shareholding {
  id: string
  company_id: string
  person_id: string
  class_name: string
  shares_held: number
  jointly_held: string | null
  remarks: string | null
  percentage_held: string | null
  allotment_date: string | null
  amount_paid: string | null
  amount_unpaid: string | null
  address: Address | null
  valid_from: string
  valid_to: string | null
  created_by: string | null
  created_at: string
  updated_at: string
  person: PersonSnapshot
}

/** A new person's officer-facing fields; the §1 protected class has no field here. */
export interface PersonWrite {
  person_type?: PersonType
  name_zh?: string | null
  name_en?: string | null
  surname_en?: string | null
  other_names_en?: string | null
  previous_names?: NamePair[]
  aliases?: NamePair[]
  email?: string | null
  br_number?: string | null
  hkid_partial?: string | null
  passport_partial?: string | null
  passport_issuing_country_region?: string | null
  tcsp_licence?: TcspLicence | null
}

/**
 * One new appointment: `person_id` XOR `person`, exactly one of the two.
 * `capacity` crosses as a list of strings — the domain owns which values are
 * the registry's boxes and refuses an out-of-vocabulary tick as
 * `capacity_invalid`.
 */
export interface OfficerAppointmentCreate {
  person_id?: string | null
  person?: PersonWrite | null
  role: OfficerRole
  capacity: string[]
  alternate_to?: string | null
  valid_from: string
  valid_to?: string | null
  correspondence_address?: Address | null
  registered_office_address?: Address | null
}

/**
 * Partial update of one appointment: omitted fields are unchanged, a field
 * present with value `null` clears it where clearing is legal. The person's
 * identity is deliberately absent — that is Administrator-only
 * `PATCH /persons/{id}`'s.
 */
export interface OfficerAppointmentUpdate {
  role?: OfficerRole
  capacity?: string[]
  alternate_to?: string | null
  valid_from?: string
  valid_to?: string | null
  correspondence_address?: Address | null
  registered_office_address?: Address | null
}

/** One new member position; a new person may ride along (`person_id` XOR `person`). */
export interface ShareholdingCreate {
  person_id?: string | null
  person?: PersonWrite | null
  class_name: string
  shares_held: number
  jointly_held?: string | null
  remarks?: string | null
  percentage_held?: string | null
  allotment_date?: string | null
  amount_paid?: string | null
  amount_unpaid?: string | null
  address?: Address | null
  valid_from: string
  valid_to?: string | null
}

/** Partial update of one shareholding; the create body's rules apply. */
export interface ShareholdingUpdate {
  class_name?: string
  shares_held?: number
  jointly_held?: string | null
  remarks?: string | null
  percentage_held?: string | null
  allotment_date?: string | null
  amount_paid?: string | null
  amount_unpaid?: string | null
  address?: Address | null
  valid_from?: string
  valid_to?: string | null
}

// --- Drafts (§2.8, §4.2; contract §5.1) -------------------------------------

/** The Wave-1 forms (the contract's `nar1 | nd2a`). */
export type FormCode = 'nar1' | 'nd2a'

/** Where a draft stands (§4.2). Nine values, every one a table row. */
export type DraftStatus =
  | 'generated'
  | 'under_review'
  | 'sent_for_signature'
  | 'signature_received'
  | 'reviewed'
  | 'approved'
  | 'exported'
  | 'rejected'
  | 'invalidated'

/** `signature_status` (§2.8): the client-signature lifecycle. */
export type SignatureStatus = 'not_sent' | 'sent_for_signature' | 'signature_received'

/** Every action §4.2 names — the wire vocabulary, unwired members included. */
export type DraftAction =
  | 'edit'
  | 'mark_under_review'
  | 'send_for_signature'
  | 'upload_signed'
  | 'recall'
  | 'mark_reviewed'
  | 'reject'
  | 'approve'
  | 'export'
  | 'regenerate'
  | 'invalidate'

/** ADR-0010's pin: the registry revision the draft was built against. */
export interface FormVersion {
  specification_no: string
  template_sha256: string
}

/** One earlier version of the draft, as metadata only. */
export interface DraftHistoryEntry {
  version_no: number
  status: DraftStatus
  created_at: string
  created_by: string | null
}

/** One escalation a fill raised: its kind and the sentence a human reads. */
export interface SignaturePackageEscalation {
  /** The closed vocabulary `app.domain.draft_fill` names. */
  kind: string
  detail: string
}

/**
 * The release's own block on `POST /drafts/{id}/transitions` — the
 * signature-ready package `send_for_signature` just stored (scope §2.10).
 *
 * `document_id` is the WORM `generated_draft` document the filled CR PDF was
 * registered as; `escalations` are the boxes the fill could not complete from
 * the draft, each named so staff know what to do by hand. It is `null` on
 * every other read and every other action: the block is the release's reply.
 */
export interface SignaturePackage {
  document_id: string
  escalations: SignaturePackageEscalation[]
}

/**
 * One draft version, in full — §2.8's fields plus the two the phase added
 * (`applied_event_ids`, `form_version`). `resolved_state` and
 * `field_provenance` are JSON documents whose shapes are
 * `app.domain.drafts`' — the readers for their entries live in
 * `src/drafts.ts`, the `src/change_events.ts` payload precedent.
 */
export interface Draft {
  id: string
  company_id: string
  form_type: FormCode
  version_no: number
  /** `YYYY-MM-DD` — the NAR1 made-up anchor / the ND2A filing date. */
  target_date: string
  status: DraftStatus
  resolved_state: Record<string, unknown>
  /** item → `{target, source_type, source_id, changed}` (§3.5). */
  field_provenance: Record<string, unknown>
  /** `missing_source` / `unapplied_event` / `unexpected_change` entries (§3.6). */
  qc_flags: Array<Record<string, unknown>>
  filing_metadata: Record<string, unknown>
  applied_event_ids: string[]
  form_version: FormVersion
  private_company_statement: boolean | null
  statement_confirmed_by: string | null
  statement_confirmed_at: string | null
  signature_status: SignatureStatus
  reviewed_by: string | null
  reviewed_at: string | null
  approved_by: string | null
  approved_at: string | null
  rationale: string | null
  comments: string | null
  invalidated_at: string | null
  invalidation_reason: string | null
  created_by: string | null
  created_at: string
  updated_at: string
  /** The versions before the current one, newest first. */
  history: DraftHistoryEntry[]
  /** The release's own block: only `send_for_signature`'s reply carries one. */
  signature_package: SignaturePackage | null
}

/** One action the actor may perform now, as the transitions read offers it. */
export interface AllowedDraftAction {
  action: DraftAction
  to_status: DraftStatus
  rationale_required: boolean
}

/**
 * `GET /drafts/{id}/transitions` — the §4.2 read for one actor. Only wired,
 * leg-admitted rows appear: the sole authority on what a screen may offer.
 */
export interface DraftTransitions {
  draft_id: string
  company_id: string
  version_no: number
  status: DraftStatus
  actions: AllowedDraftAction[]
}

/** `POST /companies/{id}/drafts` — one generation (the gated automation). */
export interface DraftGenerateRequest {
  form_type: FormCode
  /** The as-of date every effective date is resolved from. */
  target_date: string
}

/**
 * §2.8's filing-metadata document, as the edit form states it. The merge runs
 * one level into `return`, `presentor`, `signature` and `consent`; an absent
 * key keeps the stored value and an explicit `null` clears one.
 */
export interface FilingMetadata {
  return?: {
    made_up_date?: string | null
    fs_period_start?: string | null
    fs_period_end?: string | null
  } | null
  member_particulars_delivery?: string | null
  presentor?: {
    name?: string | null
    address?: string | null
    tel?: string | null
    fax?: string | null
    email?: string | null
    reference?: string | null
  } | null
  signature?: { name?: string | null; date?: string | null; capacity?: string | null } | null
  consent?: { signatory_name?: string | null; signatory_capacity?: string | null } | null
}

/**
 * `POST /drafts/{id}/transitions` — `{action, filing_metadata?,
 * private_company_statement?, rationale?, confirm_statement?, mode?}`. The edit
 * fields may only accompany `edit`, and only the keys actually stated may
 * travel (each absent key keeps the version's value). `confirm_statement` may
 * only accompany `approve` — required (`true`) when the draft makes NAR1 item
 * 16's declaration and it stands unconfirmed (ruling R2); a draft that makes
 * no statement posts a plain `approve`. `mode` may only accompany `export` —
 * the export UI calls `POST /drafts/{id}/exports` directly instead (ruling
 * R4's canonical door).
 */
export interface DraftTransitionRequest {
  action: DraftAction
  filing_metadata?: FilingMetadata
  private_company_statement?: boolean | null
  rationale?: string | null
  confirm_statement?: boolean | null
  mode?: ExportMode | null
}

// --- The export (§2.12; contract §5.1's Export row) --------------------------

/** §2.12's closed vocabulary — what the filing package is made as. */
export type ExportMode = 'e_registry_prep' | 'paper' | 'api_ready'

/** `POST /drafts/{id}/exports` — the mode alone; unknown values refuse 422. */
export interface ExportRequest {
  mode: ExportMode
}

/**
 * One export as stored (§2.8's FilingPackage): the artifact and the record.
 *
 * `document_id` is the WORM `export_package` document the primary artifact was
 * stored as — the flattened PDF for `e_registry_prep`/`paper`, the
 * deterministic JSON for `api_ready` — and `envelope` carries what the
 * assembly derived: `sheet_counts` per sheet slug, the footer `page_pairs`
 * the numbered sheets printed, and the companion documents' ids (the
 * e-Registry helper, the paper checklist; `null` where the mode carries none).
 */
export interface FilingPackage {
  id: string
  draft_id: string
  draft_version_no: number
  mode: ExportMode
  document_id: string
  envelope: Record<string, unknown>
  exported_by: string
  exported_at: string
}
