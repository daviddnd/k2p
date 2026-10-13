/**
 * Display formatting and label vocabularies — one visual language for dates,
 * BRNs, risk colours, alert bands, company statuses, automation types and the
 * gate's refusal reasons. Every label is a translation of a backend value;
 * nothing here invents a rule the backend did not report.
 */

import type {
  AlertKind,
  AutomationType,
  CalendarEntry,
  GatingReason,
  RiskLevel,
} from './api/types'

const DATE_FORMAT = new Intl.DateTimeFormat('en-GB', {
  weekday: 'short',
  day: '2-digit',
  month: 'short',
  year: 'numeric',
})

/** `2026-09-21` → `Mon, 21 Sept 2026`. */
export function formatDate(isoDate: string): string {
  return DATE_FORMAT.format(parseIsoDate(isoDate))
}

function parseIsoDate(isoDate: string): Date {
  const [year, month, day] = isoDate.split('-').map(Number)
  return new Date(year, month - 1, day)
}

/** A `Date` as the `YYYY-MM-DD` the API expects, in local time. */
export function isoDate(date: Date): string {
  const month = String(date.getMonth() + 1).padStart(2, '0')
  const day = String(date.getDate()).padStart(2, '0')
  return `${date.getFullYear()}-${month}-${day}`
}

export function addDays(date: Date, days: number): Date {
  const next = new Date(date)
  next.setDate(next.getDate() + days)
  return next
}

/** `in 14 days` / `due today` / `3 days overdue`. */
export function formatDaysRemaining(days: number): string {
  if (days < 0) return `${-days} day${days === -1 ? '' : 's'} overdue`
  if (days === 0) return 'due today'
  return `in ${days} day${days === 1 ? '' : 's'}`
}

/** `870` → `HK$870` — CR fees are whole HKD. */
export function formatHkd(amount: number): string {
  return `HK$${amount.toLocaleString('en-GB')}`
}

const RISK_LABELS: Record<RiskLevel, string> = {
  green: 'Green',
  amber: 'Amber',
  red: 'Red',
}

export function riskLabel(risk: RiskLevel): string {
  return RISK_LABELS[risk]
}

const ALERT_LABELS: Record<AlertKind, string> = {
  alert_30d: '30-day alert',
  alert_15d: '15-day alert',
  alert_7d: '7-day alert',
}

export function alertLabel(alert: AlertKind | null): string {
  return alert === null ? '—' : ALERT_LABELS[alert]
}

const STATUS_LABELS: Record<string, string> = {
  active: 'Active',
  dormant: 'Dormant',
  striking_off: 'Striking off',
  dissolved: 'Dissolved',
}

export function companyStatusLabel(status: string | null): string {
  if (status === null) return 'No status recorded'
  return STATUS_LABELS[status] ?? status
}

const COMPANY_TYPE_LABELS: Record<string, string> = {
  private: 'Private company',
  public: 'Public company',
  company_limited_by_guarantee: 'Company limited by guarantee',
}

export function companyTypeLabel(type: string): string {
  return COMPANY_TYPE_LABELS[type] ?? type
}

const AUTOMATION_LABELS: Record<AutomationType, string> = {
  calendar_sync: 'Calendar sync',
  automated_prefill: 'Automated prefill',
  email_ingestion: 'Email ingestion',
  document_extraction: 'Document extraction',
  ai_reply_parsing: 'AI reply parsing',
  confirmation_generation: 'Confirmation generation',
  workflow_orchestration: 'Workflow orchestration',
}

export function automationLabel(automation: AutomationType): string {
  return AUTOMATION_LABELS[automation] ?? automation
}

/** The gate's own `GatingReason`, as a phrase a reviewer can act on. */
const GATING_REASON_LABELS: Record<GatingReason, string> = {
  tier_capability_missing: "this deployment's licence tier does not carry the capability",
  no_assignment: 'no active service package is assigned',
  assignment_ended: 'the service package has ended',
  out_of_coverage: 'the date falls outside the package coverage period',
  service_not_included: 'the assigned package does not include it',
}

export function gatingReasonLabel(reason: GatingReason): string {
  return GATING_REASON_LABELS[reason] ?? reason
}

export function companyLabel(entry: Pick<CalendarEntry, 'company_name' | 'br_number'>): string {
  return entry.company_name ?? `Company ${entry.br_number}`
}

/** `2026-09-21T14:30:00Z` → `21 Sept 2026, 22:30` (local time). */
const DATETIME_FORMAT = new Intl.DateTimeFormat('en-GB', {
  day: '2-digit',
  month: 'short',
  year: 'numeric',
  hour: '2-digit',
  minute: '2-digit',
})

export function formatDateTime(isoTimestamp: string): string {
  return DATETIME_FORMAT.format(new Date(isoTimestamp))
}

/** The first eight characters of an id — enough to match, never a UUID wall. */
export function shortId(id: string): string {
  return id.slice(0, 8)
}

const FORM_LABELS: Record<string, string> = {
  nar1: 'NAR1 — annual return',
  nd2a: 'ND2A — change of secretary/director',
}

export function formLabel(form: string): string {
  return FORM_LABELS[form] ?? form.toUpperCase()
}

/** How a prior filing arrived (scope §2.1). `needs_ocr` is an outcome too. */
const SHAPE_LABELS: Record<string, string> = {
  acroform: 'Filled form fields (AcroForm)',
  text_layer: 'Text layer',
  needs_ocr: 'Scanned image (local OCR)',
}

export function shapeLabel(shape: string): string {
  return SHAPE_LABELS[shape] ?? shape
}

const RECORD_STATUS_LABELS: Record<string, string> = {
  pending: 'Pending review',
  confirmed: 'Confirmed',
  rejected: 'Rejected',
}

export function recordStatusLabel(status: string): string {
  return RECORD_STATUS_LABELS[status] ?? status
}

/**
 * The §5.3 milestones, as the four steps a client meets — labelled for what
 * happens at each, not for the field name that configures it.
 */
const MILESTONE_KIND_LABELS: Record<string, string> = {
  continuation_request: 'Continuation ask',
  data_confirmation: 'Data confirmation',
  client_reply: 'Client reply by',
  signature_post: 'Documents to the client',
}

export function milestoneKindLabel(kind: string): string {
  return MILESTONE_KIND_LABELS[kind] ?? kind
}

/**
 * The panel's five states, as `app.domain.service_renewals.milestone_state`
 * derives them. The backend's own words, and nothing invented: `pending` is a
 * day still to come, `due` the day itself, `overdue` a day that has passed,
 * `done` the record that completes the milestone existing, `blocked` something
 * refusing the work.
 */
const MILESTONE_STATE_LABELS: Record<string, string> = {
  pending: 'Pending',
  due: 'Due today',
  overdue: 'Overdue',
  done: 'Done',
  blocked: 'Blocked',
}

export function milestoneStateLabel(state: string): string {
  return MILESTONE_STATE_LABELS[state] ?? state
}

/** Where a period's continuation stands (§5.1.9). Null: no row was recorded. */
const RENEWAL_STATUS_LABELS: Record<string, string> = {
  pending: 'Awaiting the client’s answer',
  confirmed: 'Client confirmed',
  declined: 'Client declined',
  payment_received: 'Fee received',
}

export function renewalStatusLabel(status: string | null): string {
  if (status === null) return 'No record for this period'
  return RENEWAL_STATUS_LABELS[status] ?? status
}

/** The §2.7 routing bands an assessment reports. */
const ROUTING_LABELS: Record<string, string> = {
  auto_accept: 'Auto-accept band',
  staff_review: 'Staff review',
  client_clarification: 'Client clarification',
}

export function routingLabel(routing: string | null): string {
  if (routing === null) return 'Nothing assessed'
  return ROUTING_LABELS[routing] ?? routing
}

/**
 * The master-data targets the extraction slice proposes, as field names a
 * reviewer reads. Unknown targets fall back to the raw registry path, so a
 * backend vocabulary addition shows up plainly instead of disappearing.
 */
const TARGET_LABELS: Record<string, string> = {
  'company.br_number': 'BRN (anchor static)',
  'company.type': 'Company type (anchor static)',
  'company.name_en': 'Registered name (English)',
  'company.name_zh': 'Registered name (Chinese)',
  'company.business_name': 'Business name',
  'company.email': 'Email',
  'company.contact_phone_hk': 'Contact phone (HK)',
  'company.mortgages_and_charges_total': 'Mortgages and charges total',
  'company.member_count': 'Member count',
  'company.registered_office.flat': 'Registered office — flat',
  'company.registered_office.building': 'Registered office — building',
  'company.registered_office.street': 'Registered office — street',
  'company.registered_office.district': 'Registered office — district',
  'company.business_nature_code': 'Business nature — code',
  'company.business_nature_description': 'Business nature — description',
  'return.made_up_date': 'Filing made-up date (context)',
}

export function targetLabel(target: string): string {
  return TARGET_LABELS[target] ?? target
}

/**
 * The two anchor statics the extraction slice proposes (`company.br_number`,
 * `company.type`; contract §5.1.2). Deliberately duplicated here: the API's
 * record shape does not carry the write path, and a reviewer confirming a
 * static has to be told that master data is corrected in place — no effective
 * date, Administrator only — rather than shown a date field that would do
 * nothing.
 */
const STATIC_TARGETS = new Set(['company.br_number', 'company.type'])

export function isStaticTarget(target: string): boolean {
  return STATIC_TARGETS.has(target)
}

/**
 * How a value was read and what capped or explained it — the provenance the
 * assessment's `signal_log` carries per target. Unknown signals show raw.
 */
const SIGNAL_LABELS: Record<string, string> = {
  exact_field_value: 'read from the form field itself',
  text_in_field_rect: 'text found inside the field box',
  ocr_text_in_field_rect: 'OCR read inside the field box',
  ocr_checkbox_mark: 'checkbox read by ink coverage',
  bilingual_box_split: 'bilingual box split by script',
  repeated_field_disagreement: 'repeated header fields disagree',
  checkbox_group_not_single: 'more than one box in a group was marked',
  value_unparsable: 'value could not be normalised',
  needs_ocr: 'no text layer — the scan path read it',
  form_frame_not_found: 'the page frame does not match the pinned form',
  unsupported_spec: 'the printed specification is not the pinned one',
  spec_region_unreadable: 'the specification number could not be read',
  ocr_field_unreadable: 'the box yielded no text',
  ocr_engine_unavailable: 'the OCR engine is not installed',
  ocr_value_unvalidated: 'no machine check for this kind of value',
}

export function signalLabel(signal: string): string {
  return SIGNAL_LABELS[signal] ?? signal
}
