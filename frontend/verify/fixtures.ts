/**
 * Fixtures for the SSR verification entry: realistic payloads in the exact
 * wire shapes the API returns.
 *
 * They are typed against `src/api/types.ts`, and the verification run writes
 * them to `dist-ssr/fixtures.json` so the backend's own Pydantic response
 * models can validate them field for field (`verify/check-fixtures.py`).
 * Values are synthetic.
 */

import type { AuditReport } from '../src/auditReport'
import type {
  CalendarProbeResult,
  CalendarResponse,
  ChangeEvent,
  ChangeEventQueueItem,
  ChangeEventTransitions,
  ChangeEventVersion,
  ClientActionsResponse,
  Communication,
  CompanyMasterData,
  DashboardResponse,
  Draft,
  DraftTransitions,
  ExtractionAssessment,
  FeedCredential,
  FilingPackage,
  ImportBatch,
  ImportBatchSummary,
  IssuedFeedCredential,
  LoginResponse,
  MfaStatus,
  OfficerAppointment,
  OnboardingGuide,
  ProviderCredential,
  PushStatus,
  ReplyParse,
  RiskAcknowledgment,
  ServiceRenewal,
  Shareholding,
  SignaturePackage,
  TenantConfig,
} from '../src/api/types'

const ALPHA = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
const BETA = 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb'
const GAMMA = 'cccccccc-cccc-4ccc-8ccc-cccccccccccc'
const DELTA = 'dddddddd-dddd-4ddd-8ddd-dddddddddddd'
const EPSILON = 'eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee'
const USER = '99999999-9999-4999-8999-999999999999'
const TENANT = '77777777-7777-4777-8777-777777777777'

const ALPHA_DOCUMENT = '11111111-2222-4333-8444-555555555555'
const ALPHA_DATED_BATCH = '22222222-3333-4444-8555-666666666666'
const ALPHA_SCAN_BATCH = '33333333-4444-4555-8666-777777777777'
const ALPHA_UNDATED_BATCH = '44444444-5555-4666-8777-888888888888'
const ALPHA_DECIDED_BATCH = '55555555-6666-4777-8888-999999999999'
const FEED_ACTIVE = '66666666-7777-4888-8999-aaaaaaaaaaaa'
const FEED_REVOKED = '77777777-8888-4999-8aaa-bbbbbbbbbbbb'

// The change-event fixtures' ids, one per event (stable across versions).
const CHANGE_APPOINTMENT = 'a1a1a1a1-1111-4111-8111-aaaaaaaaaaaa'
const CHANGE_CESSATION = 'b2b2b2b2-2222-4222-8222-bbbbbbbbbbbb'
const CHANGE_OFFICE = 'c3c3c3c3-3333-4333-8333-cccccccccccc'
const CHANGE_OTHER = 'd4d4d4d4-4444-4444-8444-dddddddddddd'
const CHANGE_BETA = 'e5e5e5e5-5555-4555-8555-eeeeeeeeeeee'

// The pending-confirmations fixtures' ids, one per awaiting request.
const CONFIRM_ALPHA_MID = 'f6f6f6f6-6666-4666-8666-ffffffffffff'
const CONFIRM_ALPHA_EXHAUSTED = '07070707-7777-4777-8777-000000000000'
const CONFIRM_GAMMA_BLOCKED = '18181818-8888-4888-8888-111111111111'

export const loginFixture: LoginResponse = {
  access_token: 'session-token-for-verification',
  token_type: 'bearer',
  expires_at: '2026-09-28T09:30:00Z',
  mfa_required: false,
  enrollment_state: 'not_required',
  user: {
    id: USER,
    tenant_id: TENANT,
    email: 'admin@example-tcsp.hk',
    display_name: 'A. Secretary',
    role: 'administrator',
    review_events: true,
    approve_events: true,
    review_drafts: true,
    approve_drafts: true,
    is_active: true,
  },
}

// The status read an unverified session's guard and enrollment screen make.
// The enrollment-begin answer is deliberately NOT a fixture: it is the one
// response ever carrying the plaintext TOTP secret, and the secret never
// touches fixtures — the enrollment view is rendered in the verifier with a
// synthetic value instead.
export const mfaStatus: MfaStatus = {
  enrollment_state: 'pending',
  mfa_verified: false,
}

export const dashboardPopulated: DashboardResponse = {
  as_of: '2026-09-21',
  window_from: '2026-08-22',
  window_to: '2027-09-22',
  portfolio: {
    total_companies: 4,
    due_within_30_days: 1,
    due_within_15_days: 1,
    due_within_7_days: 0,
    overdue: 1,
    late_fee_exposure_hkd: 870,
  },
  upcoming_deadlines: [
    {
      company_id: ALPHA,
      br_number: '11112222',
      company_name: 'Alpha Traders Limited',
      kind: 'nar1_due',
      due_date: '2026-10-05',
      days_remaining: 14,
      risk: 'amber',
      alert: 'alert_15d',
      overdue: false,
      late_fee_exposure: { fee_hkd: 105, late: false },
    },
  ],
  risk_alerts: [
    {
      company_id: GAMMA,
      br_number: '55556666',
      company_name: 'Gamma Services Limited',
      kind: 'nar1_due',
      due_date: '2026-09-18',
      days_remaining: -3,
      risk: 'red',
      alert: 'alert_7d',
      overdue: true,
      late_fee_exposure: { fee_hkd: 870, late: true },
    },
    {
      company_id: ALPHA,
      br_number: '11112222',
      company_name: 'Alpha Traders Limited',
      kind: 'nar1_due',
      due_date: '2026-10-05',
      days_remaining: 14,
      risk: 'amber',
      alert: 'alert_15d',
      overdue: false,
      late_fee_exposure: { fee_hkd: 105, late: false },
    },
  ],
  pending_ingestion: {
    pending_records: 5,
    companies: [
      { company_id: ALPHA, company_name: 'Alpha Traders Limited', pending_records: 4 },
      { company_id: GAMMA, company_name: 'Gamma Services Limited', pending_records: 1 },
    ],
  },
  pending_event_review: {
    pending_events: 3,
    companies: [
      { company_id: GAMMA, company_name: 'Gamma Services Limited', pending_events: 2 },
      { company_id: ALPHA, company_name: 'Alpha Traders Limited', pending_events: 1 },
    ],
  },
  pending_draft_review: {
    pending_drafts: 2,
    companies: [
      { company_id: ALPHA, company_name: 'Alpha Traders Limited', pending_drafts: 1 },
      { company_id: GAMMA, company_name: 'Gamma Services Limited', pending_drafts: 1 },
    ],
  },
  pending_confirmations: {
    companies: [
      {
        company_id: ALPHA,
        company_name: 'Alpha Traders Limited',
        requests: [
          {
            confirmation_request_id: CONFIRM_ALPHA_MID,
            days_elapsed: 5,
            reminders_sent: 1,
            cadence_total: 3,
            next_reminder_at: '2026-09-24T09:00:00Z',
            exhausted: false,
            automation_blocked: null,
          },
          {
            confirmation_request_id: CONFIRM_ALPHA_EXHAUSTED,
            days_elapsed: 12,
            reminders_sent: 3,
            cadence_total: 3,
            next_reminder_at: null,
            exhausted: true,
            automation_blocked: null,
          },
        ],
      },
      {
        company_id: GAMMA,
        company_name: 'Gamma Services Limited',
        requests: [
          {
            confirmation_request_id: CONFIRM_GAMMA_BLOCKED,
            days_elapsed: 4,
            reminders_sent: 0,
            cadence_total: 3,
            next_reminder_at: '2026-09-26T09:00:00Z',
            exhausted: false,
            automation_blocked: 'no_assignment',
          },
        ],
      },
    ],
  },
  company_cards: [
    {
      company_id: ALPHA,
      br_number: '11112222',
      cr_number: '1234567',
      company_name: 'Alpha Traders Limited',
      status: 'active',
      next_deadline: {
        company_id: ALPHA,
        br_number: '11112222',
        company_name: 'Alpha Traders Limited',
        kind: 'nar1_due',
        due_date: '2026-10-05',
        days_remaining: 14,
        risk: 'amber',
        alert: 'alert_15d',
        overdue: false,
        late_fee_exposure: { fee_hkd: 105, late: false },
      },
      pending_ingestion_records: 4,
    },
    {
      company_id: BETA,
      br_number: '33334444',
      cr_number: null,
      company_name: 'Beta Holdings Limited',
      status: 'dormant',
      next_deadline: null,
      pending_ingestion_records: 0,
    },
    {
      company_id: GAMMA,
      br_number: '55556666',
      cr_number: '7654321',
      company_name: 'Gamma Services Limited',
      status: null,
      next_deadline: {
        company_id: GAMMA,
        br_number: '55556666',
        company_name: 'Gamma Services Limited',
        kind: 'nar1_due',
        due_date: '2026-09-18',
        days_remaining: -3,
        risk: 'red',
        alert: 'alert_7d',
        overdue: true,
        late_fee_exposure: { fee_hkd: 870, late: true },
      },
      pending_ingestion_records: 1,
    },
    {
      company_id: DELTA,
      br_number: '77778888',
      cr_number: null,
      company_name: 'Delta Public Company Limited',
      status: 'active',
      next_deadline: null,
      pending_ingestion_records: 0,
    },
  ],
  service_gaps: [
    {
      company_id: ALPHA,
      automation: 'email_ingestion',
      reason: 'service_not_included',
    },
    {
      company_id: ALPHA,
      automation: 'document_extraction',
      reason: 'tier_capability_missing',
    },
    {
      company_id: GAMMA,
      automation: 'document_extraction',
      reason: 'no_assignment',
    },
  ],
  not_generated: [{ company_id: DELTA, company_type: 'public', reason: 'nar1_rule_not_established' }],
}

export const dashboardEmpty: DashboardResponse = {
  as_of: '2026-09-21',
  window_from: '2026-08-22',
  window_to: '2027-09-22',
  portfolio: {
    total_companies: 0,
    due_within_30_days: 0,
    due_within_15_days: 0,
    due_within_7_days: 0,
    overdue: 0,
    late_fee_exposure_hkd: 0,
  },
  upcoming_deadlines: [],
  risk_alerts: [],
  pending_ingestion: { pending_records: 0, companies: [] },
  pending_event_review: { pending_events: 0, companies: [] },
  pending_draft_review: { pending_drafts: 0, companies: [] },
  pending_confirmations: { companies: [] },
  company_cards: [],
  service_gaps: [],
  not_generated: [],
}

export const dashboardDormantOnly: DashboardResponse = {
  as_of: '2026-09-21',
  window_from: '2026-08-22',
  window_to: '2027-09-22',
  portfolio: {
    total_companies: 2,
    due_within_30_days: 0,
    due_within_15_days: 0,
    due_within_7_days: 0,
    overdue: 0,
    late_fee_exposure_hkd: 0,
  },
  upcoming_deadlines: [],
  risk_alerts: [],
  pending_ingestion: { pending_records: 0, companies: [] },
  pending_event_review: { pending_events: 0, companies: [] },
  pending_draft_review: { pending_drafts: 0, companies: [] },
  pending_confirmations: { companies: [] },
  company_cards: [
    {
      company_id: BETA,
      br_number: '33334444',
      cr_number: null,
      company_name: 'Beta Holdings Limited',
      status: 'dormant',
      next_deadline: null,
      pending_ingestion_records: 0,
    },
    {
      company_id: DELTA,
      br_number: '77778888',
      cr_number: null,
      company_name: 'Delta Public Company Limited',
      status: 'dormant',
      next_deadline: null,
      pending_ingestion_records: 0,
    },
  ],
  service_gaps: [],
  not_generated: [],
}

const ALL_AUTOMATIONS = [
  'calendar_sync',
  'automated_prefill',
  'email_ingestion',
  'document_extraction',
  'ai_reply_parsing',
  'confirmation_generation',
  'workflow_orchestration',
] as const

export const dashboardUnlicensed: DashboardResponse = {
  as_of: '2026-09-21',
  window_from: '2026-08-22',
  window_to: '2027-09-22',
  portfolio: {
    total_companies: 1,
    due_within_30_days: 0,
    due_within_15_days: 0,
    due_within_7_days: 0,
    overdue: 0,
    late_fee_exposure_hkd: 0,
  },
  upcoming_deadlines: [],
  risk_alerts: [],
  pending_ingestion: { pending_records: 0, companies: [] },
  pending_event_review: { pending_events: 0, companies: [] },
  pending_draft_review: { pending_drafts: 0, companies: [] },
  pending_confirmations: { companies: [] },
  company_cards: [
    {
      company_id: EPSILON,
      br_number: '99990000',
      cr_number: null,
      company_name: 'Epsilon Engineering Limited',
      status: 'active',
      next_deadline: {
        company_id: EPSILON,
        br_number: '99990000',
        company_name: 'Epsilon Engineering Limited',
        kind: 'nar1_due',
        due_date: '2027-03-01',
        days_remaining: 161,
        risk: 'green',
        alert: null,
        overdue: false,
        late_fee_exposure: { fee_hkd: 105, late: false },
      },
      pending_ingestion_records: 0,
    },
  ],
  service_gaps: ALL_AUTOMATIONS.map((automation) => ({
    company_id: EPSILON,
    automation,
    reason: 'tier_capability_missing' as const,
  })),
  not_generated: [],
}

export const calendarPopulated: CalendarResponse = {
  from: '2026-08-22',
  to: '2027-03-22',
  as_of: '2026-09-21',
  entries: [
    {
      company_id: GAMMA,
      br_number: '55556666',
      company_name: 'Gamma Services Limited',
      kind: 'nar1_due',
      due_date: '2026-09-18',
      days_remaining: -3,
      risk: 'red',
      alert: 'alert_7d',
      overdue: true,
      late_fee_exposure: { fee_hkd: 870, late: true },
    },
    {
      company_id: ALPHA,
      br_number: '11112222',
      company_name: 'Alpha Traders Limited',
      kind: 'nar1_due',
      due_date: '2026-10-05',
      days_remaining: 14,
      risk: 'amber',
      alert: 'alert_15d',
      overdue: false,
      late_fee_exposure: { fee_hkd: 105, late: false },
    },
    {
      company_id: EPSILON,
      br_number: '99990000',
      company_name: 'Epsilon Engineering Limited',
      kind: 'nar1_due',
      due_date: '2026-10-18',
      days_remaining: 27,
      risk: 'green',
      alert: 'alert_30d',
      overdue: false,
      late_fee_exposure: { fee_hkd: 105, late: false },
    },
    {
      company_id: 'ffffffff-ffff-4fff-8fff-ffffffffffff',
      br_number: '22223333',
      company_name: 'Zeta Logistics Limited',
      kind: 'nar1_due',
      due_date: '2027-02-20',
      days_remaining: 152,
      risk: 'green',
      alert: null,
      overdue: false,
      late_fee_exposure: { fee_hkd: 105, late: false },
    },
  ],
  not_generated: [{ company_id: DELTA, company_type: 'public', reason: 'nar1_rule_not_established' }],
}

// --- One company: master data and the ingestion review queue ----------------

export const companyMasterData: CompanyMasterData = {
  company: {
    id: ALPHA,
    tenant_id: TENANT,
    cr_number: '1234567',
    br_number: '11112222',
    date_of_incorporation: '2010-03-01',
    anniversary_date: '2026-03-01',
    type: 'private',
    ai_disabled: false,
  },
  as_of: '2026-09-21',
  name: {
    effective_date: '2025-03-31',
    name_en: 'Alpha Traders Limited',
    name_zh: '阿爾法貿易有限公司',
    business_name: 'ALPHA TRADERS',
  },
  registered_office: {
    effective_date: '2025-03-31',
    flat: 'FLAT 12',
    building: 'HARBOUR BUILDING',
    street: "1 QUEEN'S ROAD",
    district: 'CENTRAL',
    country: 'HONG KONG',
  },
  status: { effective_date: '2019-01-01', status: 'active' },
  business_nature: {
    effective_date: '2025-03-31',
    business_nature_code: '045',
    business_nature_description: 'IMPORT AND EXPORT TRADE',
  },
  contact: {
    effective_date: '2025-03-31',
    email: 'accounts@alpha-traders.hk',
    contact_phone_hk: '2345 6789',
  },
  filing: {
    effective_date: '2025-03-31',
    mortgages_and_charges_total: '1000.00',
    member_count: 2,
  },
  records_kept: null,
  share_capital_classes: null,
}

/** The company's runs, newest first, as the review screen lists them. */
export const importBatches: ImportBatchSummary[] = [
  {
    id: ALPHA_DATED_BATCH,
    company_id: ALPHA,
    document_id: ALPHA_DOCUMENT,
    form: 'nar1',
    shape: 'acroform',
    created_by: USER,
    created_at: '2026-09-20T09:05:00Z',
    records_total: 7,
    records_pending: 2,
    records_confirmed: 4,
    records_rejected: 1,
  },
  {
    id: ALPHA_UNDATED_BATCH,
    company_id: ALPHA,
    document_id: '12121212-3434-4565-8787-909090909090',
    form: 'nar1',
    shape: 'text_layer',
    created_by: USER,
    created_at: '2026-09-19T14:40:00Z',
    records_total: 2,
    records_pending: 2,
    records_confirmed: 0,
    records_rejected: 0,
  },
  {
    id: ALPHA_DECIDED_BATCH,
    company_id: ALPHA,
    document_id: '13131313-4545-4676-8989-919191919191',
    form: 'nar1',
    shape: 'acroform',
    created_by: USER,
    created_at: '2026-09-19T11:20:00Z',
    records_total: 3,
    records_pending: 0,
    records_confirmed: 2,
    records_rejected: 1,
  },
  {
    id: ALPHA_SCAN_BATCH,
    company_id: ALPHA,
    document_id: '14141414-5656-4787-8a8a-929292929292',
    form: 'nar1',
    shape: 'needs_ocr',
    created_by: USER,
    created_at: '2026-09-18T16:55:00Z',
    records_total: 0,
    records_pending: 0,
    records_confirmed: 0,
    records_rejected: 0,
  },
]

/**
 * One dated run with decisions in it: two proposals still pending (one an
 * anchor static, which only an Administrator may confirm), four confirmed and
 * one rejected.
 */
export const importBatch: ImportBatch = {
  id: ALPHA_DATED_BATCH,
  company_id: ALPHA,
  document_id: ALPHA_DOCUMENT,
  form: 'nar1',
  shape: 'acroform',
  created_by: USER,
  created_at: '2026-09-20T09:05:00Z',
  records: [
    {
      id: '31313131-4141-4252-8363-747474747474',
      target: 'company.br_number',
      value: '11112222',
      confidence: 0.99,
      status: 'pending',
      effective_date: null,
      decided_by: null,
      decided_at: null,
    },
    {
      id: '32323232-4242-4353-8464-757575757575',
      target: 'company.email',
      value: 'accounts@alpha-traders.hk',
      confidence: 0.99,
      status: 'confirmed',
      effective_date: '2025-03-31',
      decided_by: USER,
      decided_at: '2026-09-20T09:30:00Z',
    },
    {
      id: '33333333-4343-4454-8575-767676767676',
      target: 'company.member_count',
      value: '2',
      confidence: 0.99,
      status: 'confirmed',
      effective_date: '2025-03-31',
      decided_by: USER,
      decided_at: '2026-09-20T09:31:00Z',
    },
    {
      id: '34343434-4444-4555-8686-777777777777',
      target: 'company.name_en',
      value: 'Alpha Traders Limited',
      confidence: 0.8,
      status: 'confirmed',
      effective_date: '2025-03-31',
      decided_by: USER,
      decided_at: '2026-09-20T09:32:00Z',
    },
    {
      id: '35353535-4545-4656-8797-888888888888',
      target: 'company.name_zh',
      value: '阿爾法貿易有限公司',
      confidence: 0.8,
      status: 'confirmed',
      effective_date: '2025-03-31',
      decided_by: USER,
      decided_at: '2026-09-20T09:33:00Z',
    },
    {
      id: '36363636-4646-4757-8a8a-999999999999',
      target: 'company.contact_phone_hk',
      value: '2345 6789',
      confidence: 0.7,
      status: 'pending',
      effective_date: '2025-03-31',
      decided_by: null,
      decided_at: null,
    },
    {
      id: '37373737-4747-4858-8b9b-aaaaaaaaaaaa',
      target: 'company.registered_office.flat',
      value: 'FLAT 99',
      confidence: 0.7,
      status: 'rejected',
      effective_date: '2025-03-31',
      decided_by: USER,
      decided_at: '2026-09-20T09:40:00Z',
    },
  ],
  assessment: {
    id: '21212121-3232-4343-8545-656565656565',
    subject_type: 'document',
    subject_id: ALPHA_DOCUMENT,
    component: 'document_extractor',
    model: 'pdf-geometry/v1',
    field_scores: {
      'company.br_number': 0.99,
      'company.email': 0.99,
      'company.member_count': 0.99,
      'company.name_en': 0.8,
      'company.name_zh': 0.8,
      'company.contact_phone_hk': 0.7,
      'company.registered_office.flat': 0.7,
    },
    signal_log: {
      shape: 'acroform',
      fields: {
        'company.br_number': ['exact_field_value'],
        'company.email': ['exact_field_value'],
        'company.member_count': ['exact_field_value'],
        'company.name_en': ['exact_field_value', 'bilingual_box_split'],
        'company.name_zh': ['exact_field_value', 'bilingual_box_split'],
        'company.contact_phone_hk': ['text_in_field_rect'],
        'company.registered_office.flat': ['text_in_field_rect'],
        'return.made_up_date': ['exact_field_value'],
      },
      document: [],
    },
    overall_score: 0.7,
    routing: 'staff_review',
    created_at: '2026-09-20T09:05:00Z',
  },
}

/** A run whose filing stated no made-up date: only the reviewer can date it. */
export const importBatchNoEffectiveDate: ImportBatch = {
  id: ALPHA_UNDATED_BATCH,
  company_id: ALPHA,
  document_id: '12121212-3434-4565-8787-909090909090',
  form: 'nar1',
  shape: 'text_layer',
  created_by: USER,
  created_at: '2026-09-19T14:40:00Z',
  records: [
    {
      id: '41414141-5151-5262-8373-848484848484',
      target: 'company.email',
      value: 'accounts@alpha-traders.hk',
      confidence: 0.7,
      status: 'pending',
      effective_date: null,
      decided_by: null,
      decided_at: null,
    },
    {
      id: '42424242-5252-5363-8484-858585858585',
      target: 'company.registered_office.building',
      value: 'HARBOUR BUILDING',
      confidence: 0.7,
      status: 'pending',
      effective_date: null,
      decided_by: null,
      decided_at: null,
    },
  ],
  assessment: {
    id: '22222222-3232-4242-8343-535353535353',
    subject_type: 'document',
    subject_id: '12121212-3434-4565-8787-909090909090',
    component: 'document_extractor',
    model: 'pdf-geometry/v1',
    field_scores: { 'company.email': 0.7, 'company.registered_office.building': 0.7 },
    signal_log: {
      shape: 'text_layer',
      fields: {
        'company.email': ['text_in_field_rect'],
        'company.registered_office.building': ['text_in_field_rect'],
        'return.made_up_date': ['value_unparsable'],
      },
      document: [],
    },
    overall_score: 0.7,
    routing: 'staff_review',
    created_at: '2026-09-19T14:40:00Z',
  },
}

/** A run whose records have all been decided: what remains is the record. */
export const importBatchDecided: ImportBatch = {
  id: ALPHA_DECIDED_BATCH,
  company_id: ALPHA,
  document_id: '13131313-4545-4676-8989-919191919191',
  form: 'nar1',
  shape: 'acroform',
  created_by: USER,
  created_at: '2026-09-19T11:20:00Z',
  records: [
    {
      id: '51515151-6161-6272-8383-949494949494',
      target: 'company.business_name',
      value: 'ALPHA TRADERS',
      confidence: 0.99,
      status: 'confirmed',
      effective_date: '2025-03-31',
      decided_by: USER,
      decided_at: '2026-09-19T11:45:00Z',
    },
    {
      id: '52525252-6262-6373-8484-959595959595',
      target: 'company.name_en',
      value: 'Alpha Traders Ltd',
      confidence: 0.8,
      status: 'rejected',
      effective_date: '2025-03-31',
      decided_by: USER,
      decided_at: '2026-09-19T11:46:00Z',
    },
    {
      id: '53535353-6363-6474-8585-969696969696',
      target: 'company.name_zh',
      value: '阿爾法貿易有限公司',
      confidence: 0.99,
      status: 'confirmed',
      effective_date: '2025-03-31',
      decided_by: USER,
      decided_at: '2026-09-19T11:47:00Z',
    },
  ],
  assessment: {
    id: '23232323-3333-4343-8444-545454545454',
    subject_type: 'document',
    subject_id: '13131313-4545-4676-8989-919191919191',
    component: 'document_extractor',
    model: 'pdf-geometry/v1',
    field_scores: {
      'company.business_name': 0.99,
      'company.name_en': 0.8,
      'company.name_zh': 0.99,
    },
    signal_log: {
      shape: 'acroform',
      fields: {
        'company.business_name': ['exact_field_value'],
        'company.name_en': ['exact_field_value', 'bilingual_box_split'],
        'company.name_zh': ['exact_field_value', 'bilingual_box_split'],
      },
      document: [],
    },
    overall_score: 0.8,
    routing: 'staff_review',
    created_at: '2026-09-19T11:20:00Z',
  },
}

/** A scanned run that proposed nothing, and its own record of why. */
export const importBatchNeedsOcr: ImportBatch = {
  id: ALPHA_SCAN_BATCH,
  company_id: ALPHA,
  document_id: '14141414-5656-4787-8a8a-929292929292',
  form: 'nar1',
  shape: 'needs_ocr',
  created_by: USER,
  created_at: '2026-09-18T16:55:00Z',
  records: [],
  assessment: {
    id: '24242424-3434-4444-8545-565656565656',
    subject_type: 'document',
    subject_id: '14141414-5656-4787-8a8a-929292929292',
    component: 'document_extractor',
    model: 'tesseract/5.3.4+ocr-fields/v1',
    field_scores: {},
    signal_log: {
      shape: 'needs_ocr',
      fields: {},
      document: ['needs_ocr', 'form_frame_not_found'],
    },
    overall_score: null,
    routing: null,
    created_at: '2026-09-18T16:55:00Z',
  },
}

/** A company whose prior filing has never been read. */
export const importBatchesEmpty: ImportBatchSummary[] = []

// --- The calendar feed credentials -----------------------------------------

export const feedCredentials: FeedCredential[] = [
  {
    id: FEED_REVOKED,
    created_by: USER,
    created_at: '2026-08-01T10:00:00Z',
    revoked_at: '2026-09-10T08:30:00Z',
  },
  {
    id: FEED_ACTIVE,
    created_by: USER,
    created_at: '2026-09-10T08:31:00Z',
    revoked_at: null,
  },
]

export const feedCredentialsEmpty: FeedCredential[] = []

export const issuedFeedCredential: IssuedFeedCredential = {
  id: FEED_ACTIVE,
  created_by: USER,
  created_at: '2026-09-10T08:31:00Z',
  revoked_at: null,
  subscription_url:
    'https://company-secretary.example-tcsp.hk/api/v1/calendar/feed.ics?token=EXAMPLESUBSCRIPTIONTOKEN',
}

// --- The calendar push (slice M8): credential, probe, status, guide ---------

const PROVIDER_CREDENTIAL = '88888888-9999-4aaa-8aaa-cccccccccccc'

/** The provider-credential read with nothing stored. */
export const providerCredentialNone: ProviderCredential = {
  connected: false,
  credential_id: null,
  provider: null,
  calendar_id: null,
  probe_passed: false,
  created_at: null,
  updated_at: null,
}

/** A credential saved but not yet probed: connected, not pushing. */
export const providerCredentialSaved: ProviderCredential = {
  connected: true,
  credential_id: PROVIDER_CREDENTIAL,
  provider: 'microsoft',
  calendar_id: 'deadlines@example-tcsp.hk',
  probe_passed: false,
  created_at: '2026-10-03T09:00:00Z',
  updated_at: '2026-10-03T09:00:00Z',
}

/** The same credential after a passing probe moved the flag. */
export const providerCredentialProbed: ProviderCredential = {
  ...providerCredentialSaved,
  probe_passed: true,
  updated_at: '2026-10-03T09:05:00Z',
}

/** The probe's answer when every leg passed. */
export const probePassed: CalendarProbeResult = {
  credential_id: PROVIDER_CREDENTIAL,
  passed: true,
  legs: [
    { name: 'token_refresh', ok: true, detail: null },
    { name: 'calendar_list', ok: true, detail: null },
    { name: 'probe_write', ok: true, detail: null },
    { name: 'probe_delete', ok: true, detail: null },
  ],
}

/** A failed probe: the refresh grant is refused and the later legs never ran. */
export const probeFailed: CalendarProbeResult = {
  credential_id: PROVIDER_CREDENTIAL,
  passed: false,
  legs: [
    { name: 'token_refresh', ok: false, detail: 'invalid_grant' },
    { name: 'calendar_list', ok: false, detail: 'skipped' },
    { name: 'probe_write', ok: false, detail: 'skipped' },
    { name: 'probe_delete', ok: false, detail: 'skipped' },
  ],
}

/**
 * The pairing read with no credential: every covered company is on the feed.
 * Beta is gate-denied (`no_assignment`), matching the coverageExcluded
 * derivation above; the read lists every tenant company, dormant included.
 */
export const pushStatusFeed: PushStatus = {
  credential: providerCredentialNone,
  mode: 'feed',
  probe_passed: false,
  companies: [
    { company_id: ALPHA, state: 'feed', reason: null },
    { company_id: BETA, state: 'refused', reason: 'no_assignment' },
    { company_id: GAMMA, state: 'feed', reason: null },
    { company_id: DELTA, state: 'feed', reason: null },
  ],
}

/** The pairing read with a probed credential: covered companies push. */
export const pushStatusPushing: PushStatus = {
  credential: providerCredentialProbed,
  mode: 'push',
  probe_passed: true,
  companies: [
    { company_id: ALPHA, state: 'pushing', reason: null },
    { company_id: BETA, state: 'refused', reason: 'no_assignment' },
    { company_id: GAMMA, state: 'pushing', reason: null },
    { company_id: DELTA, state: 'pushing', reason: null },
  ],
}

/**
 * The pairing read with a credential saved but not yet probed: the mode is
 * `push` but `probe_passed` is false — an unprobed credential never reads
 * as pushing, so every covered company stays on the feed.
 */
export const pushStatusUnprobed: PushStatus = {
  credential: providerCredentialSaved,
  mode: 'push',
  probe_passed: false,
  companies: pushStatusFeed.companies,
}

/**
 * The static registration walks (ruling R6), verbatim from the backend's
 * one source (`backend/app/domain/calendar_onboarding.py`) — the wizard
 * renders these payloads as numbered steps, so the fixtures carry the
 * exact text the route serves.
 */
export const onboardingGuideMicrosoft: OnboardingGuide = {
  provider: 'microsoft',
  guide: `# Register a Microsoft (Microsoft 365 / Entra) application

Performed once, by the TCSP's own Microsoft 365 administrator, against the
tenant's own Entra directory — the push authenticates as the TCSP's own app,
never a shared vendor registration.

1. Sign in to the **Microsoft Entra admin center** (entra.microsoft.com) with
   an account that can register applications in the TCSP's tenant.
2. **Identity → Applications → App registrations → New registration**: give
   it a name (for example "K2P calendar push"); supported account types:
   **Accounts in this organizational directory only** (single tenant).
3. Open the app's **Overview** page and copy two values: the **Application
   (client) ID** and the **Directory (tenant) ID**.
4. **Certificates & secrets → New client secret**: create a secret and copy
   its **Value** immediately — Entra shows it only once.
5. **API permissions → Add a permission → Microsoft Graph → Delegated
   permissions**: add **Calendars.ReadWrite** (the push writes and deletes
   events on the target calendar; a read-only scope cannot work), then press
   **Grant admin consent** for the tenant.
6. Obtain a **refresh token** for the mailbox/calendar account the deadlines
   should land in: one manual OAuth consent against the v2.0 authorize
   endpoint with scopes \`offline_access\` and \`Calendars.ReadWrite\`, and
   capture the refresh token from the code exchange. This is the one step a
   person performs in a browser; the app uses the refresh token from then on.
7. Note the target **calendar id**: in Outlook, right-click the calendar →
   Properties, or read it from Graph (\`GET /me/calendars\`).

You now have five values: Directory (tenant) ID, Application (client) ID,
client secret, refresh token, and calendar id.

## In this app

1. Open **Settings → Calendar sync → Connect provider** (Administrator only).
2. Paste the values above into the provider-credential form and save — the
   client secret and refresh token are stored in a mode-0600 file on this
   deployment (ADR-0014 custody), never in the database.
3. Press **Run probe**: it refreshes the token, lists the target calendar,
   writes a probe event and deletes it (ruling R5). Every leg must pass.
4. Only then does the push start: until the probe passes, this tenant stays
   on the \`.ics\` feed, which every tier keeps.`,
}

export const onboardingGuideGoogle: OnboardingGuide = {
  provider: 'google',
  guide: `# Register a Google (Google Workspace) application

Performed once, by the TCSP's own Google Cloud administrator, inside the
TCSP's own Google Cloud project — the push authenticates as the TCSP's own
app, never a shared vendor registration.

1. Open the **Google Cloud Console** (console.cloud.google.com) and create
   (or select) the project the registration belongs to.
2. **APIs & Services → Library**: enable the **Google Calendar API**.
3. **APIs & Services → OAuth consent screen**: configure it for the project
   and add the scope **.../auth/calendar** (the push writes and deletes
   events on the target calendar; a read-only scope cannot work).
4. **APIs & Services → Credentials → Create credentials → OAuth client ID**:
   create the client and copy the **client ID** and **client secret**.
5. Obtain a **refresh token** for the account whose calendar should hold the
   deadlines: one manual OAuth consent with scopes \`offline_access\` and
   \`.../auth/calendar\`, and capture the refresh token from the code exchange.
   This is the one step a person performs in a browser; the app uses the
   refresh token from then on.
6. Note the target **calendar ID**: \`primary\` for the account's own calendar,
   or the calendar's id from its settings page for any other calendar.

You now have four values: client ID, client secret, refresh token, and
calendar ID.

## In this app

1. Open **Settings → Calendar sync → Connect provider** (Administrator only).
2. Paste the values above into the provider-credential form and save — the
   client secret and refresh token are stored in a mode-0600 file on this
   deployment (ADR-0014 custody), never in the database.
3. Press **Run probe**: it refreshes the token, lists the target calendar,
   writes a probe event and deletes it (ruling R5). Every leg must pass.
4. Only then does the push start: until the probe passes, this tenant stays
   on the \`.ics\` feed, which every tier keeps.`,
}

// --- The tenant configuration and the client-actions panel ------------------

/**
 * A tenant that has tuned its block (the shipped defaults are 60/30/20/14,
 * window 7, cadence 3/7/10): the panel must show what is stored, not the
 * defaults, so this fixture is deliberately not the shipped set. It stays
 * coherent — 75 > 35 > 21 > 15, and the reply offset 21 is inside
 * `42 − 14 = 28`.
 */
export const tenantConfig: TenantConfig = {
  naming_templates: {
    document: '{CompanyCode}/{Year}/{Form}_{DocType}_{CR}_{YYYYMMDD}.{Ext}',
    generated: '{CompanyCode}/{Year}/{Form}_{DocType}_{CR}_{YYYYMMDD}_v{Version}.{Ext}',
  },
  milestones: {
    continuation_request_days_before: 75,
    data_confirmation_days_before: 35,
    client_reply_days_before: 21,
    signature_post_days_before: 15,
    minimum_reply_window_days: 14,
    reminder_cadence_days: [5, 12],
    anchor: 'anniversary',
  },
  send_modes: { client: 'automatic', government: 'review_required' },
  updated_at: '2026-09-24T06:30:00Z',
  unreadable_blocks: [],
}

/** A tenant that has never saved one: the shipped defaults, and `updated_at: null`. */
export const tenantConfigDefaults: TenantConfig = {
  naming_templates: {
    document: '{CompanyCode}/{Year}/{Form}_{DocType}_{CR}_{YYYYMMDD}.{Ext}',
    generated: '{CompanyCode}/{Year}/{Form}_{DocType}_{CR}_{YYYYMMDD}_v{Version}.{Ext}',
  },
  milestones: {
    continuation_request_days_before: 60,
    data_confirmation_days_before: 30,
    client_reply_days_before: 20,
    signature_post_days_before: 14,
    minimum_reply_window_days: 7,
    reminder_cadence_days: [3, 7, 10],
    anchor: 'anniversary',
  },
  send_modes: { client: 'automatic', government: 'review_required' },
  updated_at: null,
  unreadable_blocks: [],
}

/**
 * The read that must not fail: a tenant whose stored milestone block cannot be
 * read. The API answers the **shipped defaults** for the block and names it,
 * with the violations a replacement has to satisfy, because this read is what
 * the full-replace `PUT` is assembled from — the screen reports the problem
 * *and* offers the repair. `updated_at` is set (the row exists), which is what
 * distinguishes this from `tenantConfigDefaults`; the templates are the
 * defaults here too in fact, but the entry names only the milestone block, so
 * a client that read the entry rather than the numbers sees exactly which part
 * of the row is unreadable.
 */
export const tenantConfigUnreadable: TenantConfig = {
  naming_templates: {
    document: '{CompanyCode}/{Year}/{Form}_{DocType}_{CR}_{YYYYMMDD}.{Ext}',
    generated: '{CompanyCode}/{Year}/{Form}_{DocType}_{CR}_{YYYYMMDD}_v{Version}.{Ext}',
  },
  milestones: {
    continuation_request_days_before: 60,
    data_confirmation_days_before: 30,
    client_reply_days_before: 20,
    signature_post_days_before: 14,
    minimum_reply_window_days: 7,
    reminder_cadence_days: [3, 7, 10],
    anchor: 'anniversary',
  },
  send_modes: { client: 'automatic', government: 'review_required' },
  updated_at: '2026-09-24T09:05:00Z',
  unreadable_blocks: [
    {
      block: 'milestones',
      violations: [
        "milestones.continuation_request_days_before: must be an integer (got str)",
        "milestones.anchor: must be 'anniversary'",
      ],
    },
  ],
}

/**
 * The panel as of 24 Sept 2026 with a 90-day look-ahead — window 25 Aug to 23
 * Dec 2026 — and four periods that between them carry every renewal status and
 * every milestone state the backend's precedence can produce:
 *
 *   - Alpha (period 2026, `confirmed`): the answer is recorded and a request was
 *     generated, so the continuation ask and the data confirmation read `done`;
 *     the request's reminder chain has run out with the reply still missing, so
 *     the reply is `blocked` (`reminders_exhausted` — §5.1.12's exhaustion);
 *     the posting is `blocked` because the fee is not recorded
 *     (`payment_not_received`).
 *   - Beta (period 2026, no row): nothing is recorded — the answer is not on
 *     file, so the reply is blocked by the missing letter
 *     (`no_confirmation_request`) and the posting by the period leg
 *     (`continuation_not_recorded`, the company's coverage holding); its data
 *     confirmation falls on the read's own day, which is the one state a date
 *     alone produces (`due`).
 *   - Gamma (period 2026, `declined`): the ask is done, the data confirmation
 *     has passed its date with no request generated (`overdue`), and the
 *     posting is blocked on the fee — a declined and unpaid period reaches the
 *     period leg like any other.
 *   - Delta (period 2027, `payment_received`): the whole record is complete, so
 *     the posting reads by its date — `pending` — and not as blocked.
 *
 * The dates are the shipped offsets counted back from each anniversary, so a
 * reader can check every row against the block above. Every blocked milestone
 * carries the leg that refused (`blocked_reason`), and every unblocked one
 * carries `null` — the field's whole contract, which the SSR checks render.
 */
export const clientActions: ClientActionsResponse = {
  as_of: '2026-09-24',
  due_within: 90,
  window_from: '2026-08-25',
  window_to: '2026-12-23',
  periods: [
    {
      company_id: ALPHA,
      br_number: '11112222',
      company_name: 'Alpha Traders Limited',
      anniversary_year: 2026,
      anniversary_date: '2026-11-20',
      renewal_status: 'confirmed',
      milestones: [
        {
          kind: 'continuation_request',
          date: '2026-09-21',
          days_remaining: -3,
          state: 'done',
          blocked_reason: null,
        },
        {
          kind: 'data_confirmation',
          date: '2026-10-21',
          days_remaining: 27,
          state: 'done',
          blocked_reason: null,
        },
        {
          kind: 'client_reply',
          date: '2026-10-31',
          days_remaining: 37,
          state: 'blocked',
          blocked_reason: 'reminders_exhausted',
        },
        {
          kind: 'signature_post',
          date: '2026-11-06',
          days_remaining: 43,
          state: 'blocked',
          blocked_reason: 'payment_not_received',
        },
      ],
    },
    {
      company_id: BETA,
      br_number: '33334444',
      company_name: 'Beta Holdings Limited',
      anniversary_year: 2026,
      anniversary_date: '2026-10-24',
      renewal_status: null,
      milestones: [
        {
          kind: 'continuation_request',
          date: '2026-08-25',
          days_remaining: -30,
          state: 'overdue',
          blocked_reason: null,
        },
        {
          kind: 'data_confirmation',
          date: '2026-09-24',
          days_remaining: 0,
          state: 'due',
          blocked_reason: null,
        },
        {
          kind: 'client_reply',
          date: '2026-10-04',
          days_remaining: 10,
          state: 'blocked',
          blocked_reason: 'no_confirmation_request',
        },
        {
          kind: 'signature_post',
          date: '2026-10-10',
          days_remaining: 16,
          state: 'blocked',
          blocked_reason: 'continuation_not_recorded',
        },
      ],
    },
    {
      company_id: GAMMA,
      br_number: '55556666',
      company_name: 'Gamma Services Limited',
      anniversary_year: 2026,
      anniversary_date: '2026-10-02',
      renewal_status: 'declined',
      milestones: [
        {
          kind: 'continuation_request',
          date: '2026-08-03',
          days_remaining: -52,
          state: 'done',
          blocked_reason: null,
        },
        {
          kind: 'data_confirmation',
          date: '2026-09-02',
          days_remaining: -22,
          state: 'overdue',
          blocked_reason: null,
        },
        {
          kind: 'client_reply',
          date: '2026-09-12',
          days_remaining: -12,
          state: 'blocked',
          blocked_reason: 'no_confirmation_request',
        },
        {
          kind: 'signature_post',
          date: '2026-09-18',
          days_remaining: -6,
          state: 'blocked',
          blocked_reason: 'payment_not_received',
        },
      ],
    },
    {
      company_id: DELTA,
      br_number: '77778888',
      company_name: 'Delta Public Company Limited',
      anniversary_year: 2027,
      anniversary_date: '2027-01-15',
      renewal_status: 'payment_received',
      milestones: [
        {
          kind: 'continuation_request',
          date: '2026-11-16',
          days_remaining: 53,
          state: 'done',
          blocked_reason: null,
        },
        {
          kind: 'data_confirmation',
          date: '2026-12-16',
          days_remaining: 83,
          state: 'done',
          blocked_reason: null,
        },
        {
          kind: 'client_reply',
          date: '2026-12-26',
          days_remaining: 93,
          state: 'pending',
          blocked_reason: null,
        },
        {
          kind: 'signature_post',
          date: '2027-01-01',
          days_remaining: 99,
          state: 'pending',
          blocked_reason: null,
        },
      ],
    },
  ],
}

/** The same panel with nothing in its window. */
export const clientActionsEmpty: ClientActionsResponse = {
  as_of: '2026-09-24',
  due_within: 90,
  window_from: '2026-08-25',
  window_to: '2026-12-23',
  periods: [],
}

/** Alpha's period 2026 row, as the three acts return it. */
export const serviceRenewal: ServiceRenewal = {
  id: '19191919-2929-4a3a-8b4b-5c5c5c5c5c5c',
  company_id: ALPHA,
  anniversary_year: 2026,
  status: 'confirmed',
  client_decided_at: '2026-09-22T02:15:00Z',
  payment_received_at: null,
  decided_by: USER,
  payment_by: null,
  note: 'client rang on the 12th',
  created_at: '2026-09-22T02:14:00Z',
}

// --- Change events: the company read, the queue, the transitions read -------

/**
 * Alpha's events, newest first (the company read's own order), one per
 * lifecycle position the screens must render: an under-review appointment at
 * version 2 with its v1 in `history` (the superseded row keeps its old status,
 * which is what makes "current version" a version-number rule), a pending
 * secretary cessation whose 15-day window has passed, a confirmed registered-
 * office change, and a deleted `other` event — the read lists soft-deleted
 * events too, visibly marked, because it is the only read they appear in.
 *
 * Every `change_window` is counted as of 21 Sept 2026, coherently with the
 * deadline: `deadline = effective_date + 15`, and the risk/alert pair is
 * `risk_band`'s (amber inside 15 days, red inside 7 or past).
 */

const appointmentCurrent: ChangeEventVersion = {
  id: CHANGE_APPOINTMENT,
  version_no: 2,
  company_id: ALPHA,
  type: 'director_appointment',
  status: 'under_review',
  effective_date: '2026-09-10',
  effective_date_text: '10 September 2026',
  informed_date: '2026-09-12',
  payload: {
    appointment: {
      officer: {
        role: 'director',
        capacity: ['director'],
        name_en: 'Wong Ka Ming',
        name_zh: '黃嘉明',
        appointment_date: '2026-09-10',
        hkid_partial: 'A123***',
        // A part of the grammar the entry/edit form does not offer; an edit
        // must carry it through rather than drop it.
        previous_names: [{ name_en: 'Wong Kar Ming' }],
      },
    },
  },
  source_type: 'manual',
  source_id: null,
  related_event_id: null,
  relation: null,
  reviewed_by: null,
  reviewed_at: null,
  approved_by: null,
  approved_at: null,
  rationale: null,
  created_by: USER,
  created_at: '2026-09-20T09:30:00Z',
}

/** The appointment's version 1, before the date correction the edit made. */
const appointmentVersion1: ChangeEventVersion = {
  id: CHANGE_APPOINTMENT,
  version_no: 1,
  company_id: ALPHA,
  type: 'director_appointment',
  status: 'pending',
  effective_date: '2026-09-08',
  effective_date_text: null,
  informed_date: null,
  payload: {
    appointment: {
      officer: {
        role: 'director',
        name_en: 'Wong Ka Ming',
        appointment_date: '2026-09-08',
      },
    },
  },
  source_type: 'manual',
  source_id: null,
  related_event_id: null,
  relation: null,
  reviewed_by: null,
  reviewed_at: null,
  approved_by: null,
  approved_at: null,
  rationale: null,
  created_by: USER,
  created_at: '2026-09-18T04:00:00Z',
}

const cessationCurrent: ChangeEventVersion = {
  id: CHANGE_CESSATION,
  version_no: 1,
  company_id: ALPHA,
  type: 'secretary_cessation',
  status: 'pending',
  effective_date: '2026-09-01',
  effective_date_text: null,
  informed_date: '2026-09-03',
  payload: {
    cessation: {
      officer: {
        role: 'company_secretary',
        name_en: 'Cheung Wai Man',
        name_zh: '張偉文',
        cessation_reason: 'resignation_others',
        cessation_date: '2026-09-01',
      },
    },
  },
  source_type: 'manual',
  source_id: null,
  related_event_id: null,
  relation: null,
  reviewed_by: null,
  reviewed_at: null,
  approved_by: null,
  approved_at: null,
  rationale: null,
  created_by: USER,
  created_at: '2026-09-19T06:00:00Z',
}

const officeChangeCurrent: ChangeEventVersion = {
  id: CHANGE_OFFICE,
  version_no: 1,
  company_id: ALPHA,
  type: 'registered_office_change',
  status: 'confirmed',
  effective_date: '2026-08-20',
  effective_date_text: null,
  informed_date: '2026-08-21',
  payload: {
    registered_office: {
      address: {
        flat: 'FLAT 12',
        building: 'HARBOUR BUILDING',
        street: "1 QUEEN'S ROAD",
        district: 'CENTRAL',
      },
    },
  },
  source_type: 'manual',
  source_id: null,
  related_event_id: null,
  relation: null,
  reviewed_by: USER,
  reviewed_at: '2026-08-21T08:00:00Z',
  approved_by: USER,
  approved_at: '2026-08-22T02:00:00Z',
  rationale: null,
  created_by: USER,
  created_at: '2026-09-18T10:00:00Z',
}

const deletedOtherCurrent: ChangeEventVersion = {
  id: CHANGE_OTHER,
  version_no: 1,
  company_id: ALPHA,
  type: 'other',
  status: 'deleted',
  effective_date: '2026-09-05',
  effective_date_text: null,
  informed_date: null,
  payload: {
    summary: 'Business registration address change, reported by the client by phone',
  },
  source_type: 'manual',
  source_id: null,
  related_event_id: null,
  relation: null,
  reviewed_by: null,
  reviewed_at: null,
  approved_by: null,
  approved_at: null,
  rationale: 'Client called on the 14th to withdraw the change.',
  created_by: USER,
  created_at: '2026-09-15T03:00:00Z',
}

const betaAppointmentCurrent: ChangeEventVersion = {
  id: CHANGE_BETA,
  version_no: 1,
  company_id: BETA,
  type: 'director_appointment',
  status: 'pending',
  effective_date: '2026-09-06',
  effective_date_text: null,
  informed_date: '2026-09-08',
  payload: {
    appointment: {
      officer: {
        role: 'director',
        capacity: ['director', 'alternate_director'],
        alternate_to: 'Chan Tai Man',
        name_en: 'Chan Siu Ming',
        surname_en: 'Chan',
        other_names_en: 'Siu Ming',
        appointment_date: '2026-09-06',
      },
    },
  },
  source_type: 'manual',
  source_id: null,
  related_event_id: null,
  relation: null,
  reviewed_by: null,
  reviewed_at: null,
  approved_by: null,
  approved_at: null,
  rationale: null,
  created_by: USER,
  created_at: '2026-09-20T02:00:00Z',
}

export const companyChangeEvents: ChangeEvent[] = [
  {
    ...appointmentCurrent,
    history: [appointmentVersion1],
    change_window: {
      deadline: '2026-09-25',
      days_remaining: 4,
      is_late: false,
      risk: 'red',
      alert: 'alert_7d',
    },
  },
  {
    ...cessationCurrent,
    history: [],
    change_window: {
      deadline: '2026-09-16',
      days_remaining: -5,
      is_late: true,
      risk: 'red',
      alert: 'alert_7d',
    },
  },
  {
    ...officeChangeCurrent,
    history: [],
    change_window: {
      deadline: '2026-09-04',
      days_remaining: -17,
      is_late: true,
      risk: 'red',
      alert: 'alert_7d',
    },
  },
  {
    ...deletedOtherCurrent,
    history: [],
    change_window: {
      deadline: '2026-09-20',
      days_remaining: -1,
      is_late: true,
      risk: 'red',
      alert: 'alert_7d',
    },
  },
]

/** A company with no change event yet. */
export const companyChangeEventsEmpty: ChangeEvent[] = []

// --- The company's communications and the newest parse (slice M5b) ------------

// Stable ids for the exchange rows and the parse they gained.
const COMM_REQUEST_OUT = 'e1000000-0000-4000-8000-000000000001'
const COMM_REPLY_IN = 'e1000000-0000-4000-8000-000000000002'
const COMM_REMINDER_OUT = 'e1000000-0000-4000-8000-000000000003'
const REPLY_PARSE = 'e2000000-0000-4000-8000-000000000001'
const PARSED_EVENT = 'e3000000-0000-4000-8000-000000000001'

/**
 * The company's exchange with its clients, newest receipt first: the
 * confirmation request the deployment sent, the client's reply to it, and the
 * reminder that followed another request. The reply row is the one the parse
 * read below belongs to: `language` and the parse fields are filled by the
 * parse cycle, and the unparsed reminder shows the null shape.
 */
export const companyCommunications: Communication[] = [
  {
    id: COMM_REPLY_IN,
    company_id: ALPHA,
    direction: 'inbound',
    channel: 'email',
    mailbox_message_id: '<reply-9f@client.example>',
    thread_id: 'thread-req-9f',
    sender_address: 'accounts@alpha-traders.hk',
    recipient_addresses: null,
    confirmation_request_id: CONFIRM_ALPHA_MID,
    language: 'en',
    parse_status: 'changes_reported',
    parse_routing: 'auto_accept',
    received_at: '2026-09-23T01:31:00Z',
    created_at: '2026-09-23T01:31:00Z',
    raw_document_id: '11111111-2222-4333-8444-555555555555',
  },
  {
    id: COMM_REMINDER_OUT,
    company_id: ALPHA,
    direction: 'outbound',
    channel: 'email',
    mailbox_message_id: '<reminder-9e@tcsp.example>',
    thread_id: 'thread-req-9e',
    sender_address: 'ai-filings@example-tcsp.hk',
    recipient_addresses: 'accounts@alpha-traders.hk',
    confirmation_request_id: CONFIRM_ALPHA_EXHAUSTED,
    language: null,
    parse_status: null,
    parse_routing: null,
    received_at: '2026-09-22T09:00:00Z',
    created_at: '2026-09-22T09:00:00Z',
    raw_document_id: '12121212-3232-4343-8545-565656565656',
  },
  {
    id: COMM_REQUEST_OUT,
    company_id: ALPHA,
    direction: 'outbound',
    channel: 'email',
    mailbox_message_id: '<req-9f@tcsp.example>',
    thread_id: 'thread-req-9f',
    sender_address: 'ai-filings@example-tcsp.hk',
    recipient_addresses: 'accounts@alpha-traders.hk',
    confirmation_request_id: CONFIRM_ALPHA_MID,
    language: null,
    parse_status: null,
    parse_routing: null,
    received_at: '2026-09-18T09:00:00Z',
    created_at: '2026-09-18T09:00:00Z',
    raw_document_id: '13131313-3434-4545-8656-767676767676',
  },
]

/** A company that has exchanged nothing yet. */
export const companyCommunicationsEmpty: Communication[] = []

/**
 * The newest parse of the client's reply, as `…/parse` returns it: the
 * grammar's status, the detected language, the routing band, each checklist
 * fact's answer, and the one event the parse proposed. Coordinates only —
 * the evidence spans the stored row carries never appear here.
 */
export const replyParse: ReplyParse = {
  id: REPLY_PARSE,
  communication_id: COMM_REPLY_IN,
  confirmation_request_id: CONFIRM_ALPHA_MID,
  status: 'changes_reported',
  language: 'en',
  routing: 'auto_accept',
  confirmations: [
    { fact_id: 'company.registered_office.flat', answer: 'confirmed' },
    { fact_id: 'company.registered_office.building', answer: 'confirmed' },
    { fact_id: 'filing.member_count', answer: 'not_addressed' },
  ],
  event_ids: [PARSED_EVENT],
  created_at: '2026-09-23T01:31:05Z',
}

/**
 * The cross-company queue, in the API's own order (sooner effective date
 * first — the same order as the statutory deadline): Alpha's late cessation,
 * Beta's appointment on its deadline day, Alpha's approaching appointment.
 */
export const changeEventQueue: ChangeEventQueueItem[] = [
  {
    ...cessationCurrent,
    br_number: '11112222',
    company_name: 'Alpha Traders Limited',
    change_window: {
      deadline: '2026-09-16',
      days_remaining: -5,
      is_late: true,
      risk: 'red',
      alert: 'alert_7d',
    },
  },
  {
    ...betaAppointmentCurrent,
    br_number: '33334444',
    company_name: 'Beta Holdings Limited',
    change_window: {
      deadline: '2026-09-21',
      days_remaining: 0,
      is_late: false,
      risk: 'red',
      alert: 'alert_7d',
    },
  },
  {
    ...appointmentCurrent,
    br_number: '11112222',
    company_name: 'Alpha Traders Limited',
    change_window: {
      deadline: '2026-09-25',
      days_remaining: 4,
      is_late: false,
      risk: 'red',
      alert: 'alert_7d',
    },
  },
]

export const changeEventQueueEmpty: ChangeEventQueueItem[] = []

/**
 * The actions an Administrator may perform on the under-review appointment:
 * every wired row of §4.1's `under_review` block, in the table's own order,
 * with the two rationale-required actions marked. Unwired rows are absent —
 * the read never offers them.
 */
export const changeEventTransitions: ChangeEventTransitions = {
  event_id: CHANGE_APPOINTMENT,
  company_id: ALPHA,
  version_no: 2,
  status: 'under_review',
  actions: [
    { action: 'mark_reviewed', to_status: 'reviewed', rationale_required: false },
    { action: 'revert', to_status: 'pending', rationale_required: false },
    { action: 'reject', to_status: 'deleted', rationale_required: true },
    { action: 'edit', to_status: 'under_review', rationale_required: false },
    { action: 'delete', to_status: 'deleted', rationale_required: false },
    { action: 'client_withdraw', to_status: 'withdrawn', rationale_required: true },
  ],
}

/** A terminal event: no action is offered, to anyone. */
export const changeEventTransitionsEmpty: ChangeEventTransitions = {
  event_id: CHANGE_OTHER,
  company_id: ALPHA,
  version_no: 1,
  status: 'deleted',
  actions: [],
}

/** Truthfully empty until the reply parser exists: hand-entered events have none. */
export const changeEventAssessments: ExtractionAssessment[] = []


// --- The officer, member and draft fixtures (slices F1–F3) ------------------

// Stable ids for the records the panels and the draft detail render.
const PERSON_WONG = 'a1000000-0000-4000-8000-000000000001'
const PERSON_CHAN = 'a1000000-0000-4000-8000-000000000002'
const PERSON_FIRM = 'a1000000-0000-4000-8000-000000000003'
const APPOINTMENT_SECRETARY = 'b1000000-0000-4000-8000-000000000001'
const APPOINTMENT_DIRECTOR = 'b1000000-0000-4000-8000-000000000002'
const APPOINTMENT_FIRM = 'b1000000-0000-4000-8000-000000000003'
const HOLDING_WONG = 'c1000000-0000-4000-8000-000000000001'
const HOLDING_CHAN = 'c1000000-0000-4000-8000-000000000002'
const DRAFT_NAR1 = 'd1000000-0000-4000-8000-000000000001'
const DRAFT_ND2A = 'd1000000-0000-4000-8000-000000000002'

/**
 * The company's appointments, newest `valid_from` first (the API's order):
 * Alpha's company secretary (Wong) and director (Chan) as natural persons,
 * and a corporate director (a firm serving two companies is the common Hong
 * Kong shape the record exists for).
 */
export const officers: OfficerAppointment[] = [
  {
    id: APPOINTMENT_SECRETARY,
    company_id: ALPHA,
    person_id: PERSON_WONG,
    role: 'company_secretary',
    capacity: ['company_secretary'],
    alternate_to: null,
    valid_from: '2021-01-04',
    valid_to: null,
    correspondence_address: {
      flat: 'FLAT 12A',
      building: 'HARBOUR VIEW COURT',
      street: '88 CONNAUGHT ROAD WEST',
      district: 'SHEUNG WAN',
    },
    registered_office_address: null,
    created_by: USER,
    created_at: '2026-09-20T03:00:00Z',
    updated_at: '2026-09-20T03:00:00Z',
    person: {
      id: PERSON_WONG,
      person_type: 'natural_person',
      name_zh: '黃嘉明',
      name_en: 'Wong Ka Ming',
      surname_en: 'Wong',
      other_names_en: 'Ka Ming',
      previous_names: [{ name_en: 'Wong Kar Ming', name_zh: '黃家明' }],
      aliases: [],
      email: 'km.wong@example-tcsp.hk',
      br_number: null,
      hkid_partial: 'A123***',
      passport_partial: null,
      passport_issuing_country_region: null,
      tcsp_licence: { licence_no: 'TC-001234', not_required: false },
    },
  },
  {
    id: APPOINTMENT_DIRECTOR,
    company_id: ALPHA,
    person_id: PERSON_CHAN,
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
    created_by: USER,
    created_at: '2026-09-20T03:05:00Z',
    updated_at: '2026-09-20T03:05:00Z',
    person: {
      id: PERSON_CHAN,
      person_type: 'natural_person',
      name_zh: '陳大文',
      name_en: 'Chan Tai Man',
      surname_en: 'Chan',
      other_names_en: 'Tai Man',
      previous_names: [],
      aliases: [{ name_en: 'T.M. Chan' }],
      email: null,
      br_number: null,
      hkid_partial: 'B456***',
      passport_partial: null,
      passport_issuing_country_region: null,
      tcsp_licence: null,
    },
  },
  {
    id: APPOINTMENT_FIRM,
    company_id: ALPHA,
    person_id: PERSON_FIRM,
    role: 'reserve_director',
    capacity: [],
    alternate_to: null,
    valid_from: '2024-02-01',
    valid_to: null,
    correspondence_address: null,
    registered_office_address: {
      flat: '21/F',
      building: 'ALPHA TOWER',
      street: '1 QUEEN’S ROAD CENTRAL',
      district: 'CENTRAL',
    },
    created_by: USER,
    created_at: '2026-09-20T03:10:00Z',
    updated_at: '2026-09-20T03:10:00Z',
    person: {
      id: PERSON_FIRM,
      person_type: 'body_corporate',
      name_zh: null,
      name_en: 'PACIFIC CORPORATE SERVICES LIMITED',
      surname_en: null,
      other_names_en: null,
      previous_names: [],
      aliases: [],
      email: 'appointments@pacific.example',
      br_number: '76543210',
      hkid_partial: null,
      passport_partial: null,
      passport_issuing_country_region: null,
      tcsp_licence: { licence_no: null, not_required: true, not_required_reason: 'a TCSP licence is held by the person named at item 20, not by this body corporate' },
    },
  },
]

/** A company with no officer record yet. */
export const officersEmpty: OfficerAppointment[] = []

/** The member positions, newest `valid_from` first; money crosses as strings. */
export const shareholdings: Shareholding[] = [
  {
    id: HOLDING_CHAN,
    company_id: ALPHA,
    person_id: PERSON_CHAN,
    class_name: 'Ordinary',
    shares_held: 500,
    jointly_held: 'Wong Ka Ming',
    remarks: null,
    percentage_held: '33.33',
    allotment_date: '2019-06-01',
    amount_paid: '500.00',
    amount_unpaid: '0.00',
    address: {
      flat: 'UNIT 3',
      building: 'GOLDEN GATE BUILDING',
      street: '20 DES VOEUX ROAD CENTRAL',
      district: 'CENTRAL',
    },
    valid_from: '2019-06-01',
    valid_to: null,
    created_by: USER,
    created_at: '2026-09-20T03:15:00Z',
    updated_at: '2026-09-20T03:15:00Z',
    person: {
      id: PERSON_CHAN,
      person_type: 'natural_person',
      name_zh: '陳大文',
      name_en: 'Chan Tai Man',
      surname_en: 'Chan',
      other_names_en: 'Tai Man',
      previous_names: [],
      aliases: [{ name_en: 'T.M. Chan' }],
      email: null,
      br_number: null,
      hkid_partial: 'B456***',
      passport_partial: null,
      passport_issuing_country_region: null,
      tcsp_licence: null,
    },
  },
  {
    id: HOLDING_WONG,
    company_id: ALPHA,
    person_id: PERSON_WONG,
    class_name: 'Ordinary',
    shares_held: 1000,
    jointly_held: null,
    remarks: 'The subscriber’s holding.',
    percentage_held: '66.67',
    allotment_date: '2010-03-01',
    amount_paid: '1000.00',
    amount_unpaid: '0.00',
    address: null,
    valid_from: '2010-03-01',
    valid_to: null,
    created_by: USER,
    created_at: '2026-09-20T03:20:00Z',
    updated_at: '2026-09-20T03:20:00Z',
    person: {
      id: PERSON_WONG,
      person_type: 'natural_person',
      name_zh: '黃嘉明',
      name_en: 'Wong Ka Ming',
      surname_en: 'Wong',
      other_names_en: 'Ka Ming',
      previous_names: [{ name_en: 'Wong Kar Ming', name_zh: '黃家明' }],
      aliases: [],
      email: 'km.wong@example-tcsp.hk',
      br_number: null,
      hkid_partial: 'A123***',
      passport_partial: null,
      passport_issuing_country_region: null,
      tcsp_licence: { licence_no: 'TC-001234', not_required: false },
    },
  },
]

/** A company with no member record yet. */
export const shareholdingsEmpty: Shareholding[] = []

/**
 * One draft as the list and the detail both return it: the NAR1 lineage at
 * version 2 (version 1 was invalidated by a reverted event), in `generated`,
 * with one changed provenance entry and the generation's QC flags.
 */
export const draftDetail: Draft = {
  id: DRAFT_NAR1,
  company_id: ALPHA,
  form_type: 'nar1',
  version_no: 2,
  target_date: '2026-09-30',
  status: 'generated',
  resolved_state: {
    company: {
      br_number: '11112222',
      cr_number: '1234567',
      type: 'private',
      date_of_incorporation: '2010-03-01',
      anniversary_date: '2010-03-01',
      name: { name_en: 'Alpha Traders Limited', name_zh: '阿爾發貿易有限公司', business_name: null },
      registered_office: {
        flat: 'FLAT 99',
        building: 'ALPHA TOWER',
        street: '1 QUEEN’S ROAD CENTRAL',
        district: 'CENTRAL',
      },
      business_nature: { business_nature_code: '46900', business_nature_description: 'Trading' },
      contact: { email: 'ops@alpha.example', contact_phone_hk: '2123 4567' },
      filing: { mortgages_and_charges_total: '0.00', member_count: 2 },
      records_kept: [],
      share_capital: {
        classes: [
          {
            class_name: 'Ordinary',
            currency: 'HKD',
            total_shares: 1500,
            total_amount: '1500.00',
            total_paid_up: '1500.00',
          },
        ],
        total: {
          derived: true,
          currency: 'HKD',
          total_shares: 1500,
          total_amount: '1500.00',
          total_paid_up: '1500.00',
        },
      },
      status: 'active',
    },
    officers: [
      {
        person_id: PERSON_CHAN,
        person_type: 'natural_person',
        name_zh: '陳大文',
        name_en: 'Chan Tai Man',
        surname_en: 'Chan',
        other_names_en: 'Tai Man',
        previous_names: [],
        aliases: [{ name_en: 'T.M. Chan' }],
        email: null,
        br_number: null,
        hkid_partial: 'B456***',
        passport_partial: null,
        passport_issuing_country_region: null,
        tcsp_licence: null,
        role: 'director',
        capacity: ['director'],
        alternate_to: null,
        correspondence_address: {
          flat: 'UNIT 3',
          building: 'GOLDEN GATE BUILDING',
          street: '20 DES VOEUX ROAD CENTRAL',
          district: 'CENTRAL',
        },
        registered_office_address: null,
        valid_from: '2026-09-10',
        valid_to: null,
        source: { type: 'change_event_id', id: CHANGE_APPOINTMENT },
      },
    ],
    shareholders: [
      {
        person_id: PERSON_WONG,
        person_type: 'natural_person',
        name_zh: '黃嘉明',
        name_en: 'Wong Ka Ming',
        surname_en: 'Wong',
        other_names_en: 'Ka Ming',
        previous_names: [],
        aliases: [],
        email: 'km.wong@example-tcsp.hk',
        br_number: null,
        hkid_partial: 'A123***',
        passport_partial: null,
        passport_issuing_country_region: null,
        tcsp_licence: { licence_no: 'TC-001234', not_required: false },
        class_name: 'Ordinary',
        shares_held: 1000,
        jointly_held: null,
        remarks: 'The subscriber’s holding.',
        percentage_held: '66.67',
        allotment_date: '2010-03-01',
        amount_paid: '1000.00',
        amount_unpaid: '0.00',
        address: null,
        valid_from: '2010-03-01',
        valid_to: null,
        source: { type: 'master_record_id', id: HOLDING_WONG },
      },
    ],
    changes: [
      {
        event_id: CHANGE_APPOINTMENT,
        type: 'director_appointment',
        effective_date: '2026-09-10',
        payload: {
          appointment: {
            officer: {
              role: 'director',
              capacity: ['director'],
              name_zh: '陳大文',
              name_en: 'Chan Tai Man',
              hkid_partial: 'B456***',
              appointment_date: '2026-09-10',
            },
          },
        },
      },
    ],
  },
  field_provenance: {
    'HDR-BRN': {
      target: 'company.br_number',
      source_type: 'master_record_id',
      source_id: ALPHA,
      changed: false,
    },
    '1': {
      target: 'company.name_en + company.name_zh',
      source_type: 'master_record_id',
      source_id: ALPHA,
      changed: false,
    },
    '6': {
      target:
        'company.registered_office.flat + company.registered_office.building + company.registered_office.street + company.registered_office.district',
      source_type: 'master_record_id',
      source_id: ALPHA,
      changed: false,
    },
    '12A.name': {
      target: 'officers[].name_zh + officers[].surname_en + officers[].other_names_en',
      source_type: 'change_event_id',
      source_id: CHANGE_APPOINTMENT,
      changed: true,
    },
    '14.shares': {
      target: 'shareholders[].shares[].shares_held',
      source_type: 'master_record_id',
      source_id: HOLDING_WONG,
      changed: true,
    },
    '9': {
      target: 'company.mortgages_and_charges_total',
      source_type: 'backfill_document_id',
      source_id: ALPHA_DOCUMENT,
      changed: false,
    },
    'HDR-DATE': {
      target: 'return.made_up_date',
      source_type: 'none',
      source_id: null,
      changed: false,
    },
  },
  qc_flags: [
    {
      kind: 'unexpected_change',
      target: 'company.member_count',
      detail: '1 -> 2',
    },
    {
      kind: 'unapplied_event',
      event_id: CHANGE_CESSATION,
      detail: 'no in-office officer record matches the event’s identity and role',
    },
    {
      kind: 'missing_source',
      path: 'filing_metadata.return.made_up_date',
    },
  ],
  filing_metadata: {
    return: {
      made_up_date: '2026-03-01',
      fs_period_start: '2025-04-01',
      fs_period_end: '2026-03-31',
    },
    member_particulars_delivery: 'schedule_2',
    presentor: {
      name: 'K2P Corporate Services Limited',
      address: '21/F, 1 Queen’s Road Central, Central, Hong Kong',
      tel: '+852 2123 4567',
      fax: null,
      email: 'filings@example-tcsp.hk',
      reference: 'NAR1-2026-ALPHA',
    },
    signature: null,
    consent: null,
  },
  applied_event_ids: [CHANGE_APPOINTMENT],
  form_version: {
    specification_no: '2/2025',
    template_sha256: 'a5a5a5a5a5a5a5a5a5a5a5a5a5a5a5a5a5a5a5a5a5a5a5a5a5a5a5a5a5a5a5a5',
  },
  private_company_statement: true,
  statement_confirmed_by: null,
  statement_confirmed_at: null,
  signature_status: 'not_sent',
  reviewed_by: null,
  reviewed_at: null,
  approved_by: null,
  approved_at: null,
  rationale: null,
  comments: null,
  invalidated_at: null,
  invalidation_reason: null,
  created_by: null,
  created_at: '2026-09-28T04:00:00Z',
  updated_at: '2026-09-28T04:00:00Z',
  history: [
    {
      version_no: 1,
      status: 'invalidated',
      created_at: '2026-09-27T02:00:00Z',
      created_by: null,
    },
  ],
  signature_package: null,
}

/**
 * The release's own reply block (F4b): the document `send_for_signature`
 * stored and the fill's escalations. It rides the transition's POST response
 * alone — a re-read of the draft carries `null`.
 */
export const draftSignaturePackage: SignaturePackage = {
  document_id: 'd4d4d4d4-d4d4-4d44-8d44-d4d4d4d4d4d4',
  escalations: [
    {
      kind: 'missing_value',
      detail:
        'filing_metadata.presentor: not stated — the presenting firm’s own name, address, tel, fax, email and reference are completed through the draft’s edit',
    },
    {
      kind: 'protected_class_manual',
      detail:
        'appointment a1a1a1a1-1111-4111-8111-aaaaaaaaaaaa (陳大文): change_event.payload.appointment.officer.hkid_full: §1 protected information, which no field of this build holds and the planner never fills — complete fill_5_P.7 by hand on the PI-ND2A sheet',
    },
  ],
}

/**
 * A draft whose documents resolved nothing in the collections: no officer, no
 * member, no applied change, no flag, no earlier version — the empty states,
 * all at once. An ND2A lineage that a reverted event invalidated, so it is
 * also the terminal case the actions read offers nothing for.
 */
export const draftDetailMinimal: Draft = {
  id: DRAFT_ND2A,
  company_id: ALPHA,
  form_type: 'nd2a',
  version_no: 1,
  target_date: '2026-09-30',
  status: 'invalidated',
  resolved_state: {
    company: {
      br_number: '11112222',
      cr_number: '1234567',
      type: 'private',
      date_of_incorporation: '2010-03-01',
      anniversary_date: '2010-03-01',
      name: { name_en: 'Alpha Traders Limited', name_zh: null, business_name: null },
      registered_office: {
        flat: 'FLAT 99',
        building: 'ALPHA TOWER',
        street: '1 QUEEN’S ROAD CENTRAL',
        district: 'CENTRAL',
      },
      business_nature: null,
      contact: null,
      filing: null,
      records_kept: [],
      share_capital: { classes: [], total: { derived: true, currency: null, total_shares: null, total_amount: null, total_paid_up: null } },
      status: 'active',
    },
    officers: [],
    shareholders: [],
    changes: [],
  },
  field_provenance: {},
  qc_flags: [],
  filing_metadata: {
    return: {
      made_up_date: null,
      fs_period_start: null,
      fs_period_end: null,
    },
    member_particulars_delivery: null,
    presentor: null,
    signature: null,
    consent: null,
  },
  applied_event_ids: [],
  form_version: {
    specification_no: '4/2024',
    template_sha256: 'b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6',
  },
  private_company_statement: null,
  statement_confirmed_by: null,
  statement_confirmed_at: null,
  signature_status: 'not_sent',
  reviewed_by: null,
  reviewed_at: null,
  approved_by: null,
  approved_at: null,
  rationale: null,
  comments: null,
  invalidated_at: '2026-09-28T05:30:00Z',
  invalidation_reason: 'change event b2b2b2b2 revert',
  created_by: null,
  created_at: '2026-09-28T05:00:00Z',
  updated_at: '2026-09-28T05:30:00Z',
  history: [],
  signature_package: null,
}

/** The company's drafts: each lineage's current version, newest first. */
export const companyDrafts: Draft[] = [draftDetail, draftDetailMinimal]

/** A company with no draft yet. */
export const companyDraftsEmpty: Draft[] = []

/**
 * The actions an Administrator may perform on the `generated` NAR1 draft:
 * the wired, leg-admitted rows only — `edit` and `mark_under_review`. The
 * unwired rows (send for signature, approve, export, regenerate) are absent
 * because the read never offers them.
 */
export const draftTransitions: DraftTransitions = {
  draft_id: DRAFT_NAR1,
  company_id: ALPHA,
  version_no: 2,
  status: 'generated',
  actions: [
    { action: 'edit', to_status: 'under_review', rationale_required: false },
    { action: 'mark_under_review', to_status: 'under_review', rationale_required: false },
  ],
}

/** A terminal draft: an invalidated lineage offers nothing, to anyone. */
export const draftTransitionsEmpty: DraftTransitions = {
  draft_id: DRAFT_ND2A,
  company_id: ALPHA,
  version_no: 1,
  status: 'invalidated',
  actions: [],
}

/**
 * The FilingPackage the exports route answers with: Alpha's NAR1 v2 exported
 * in `paper` mode — the flattened PDF artifact, the covering checklist as the
 * companion, and the envelope the assembly derived (two Schedule 1 copies
 * numbered 1 and 2, one continuation sheet, no e-Registry helper).
 */
export const filingPackage: FilingPackage = {
  id: 'e6000000-0000-4000-8000-000000000001',
  draft_id: DRAFT_NAR1,
  draft_version_no: 2,
  mode: 'paper',
  document_id: 'e6000000-0000-4000-8000-000000000002',
  envelope: {
    sheet_counts: { s1: 2, 'cs-a': 1 },
    page_pairs: [
      [1, 1],
      [2, 2],
    ],
    helper_document_id: null,
    checklist_document_id: 'e6000000-0000-4000-8000-000000000003',
  },
  exported_by: USER,
  exported_at: '2026-10-02T06:00:00Z',
}

// --- The pre-use risk acknowledgment (the 2026-10-04 founder rulings) --------

/**
 * The v1 statement text exactly as the backend's static source serves it
 * (`app/domain/risk_acknowledgment.py`'s `STATEMENT_TEXT`): the fixture is the
 * disclosure itself, so the SSR check that the interstitial renders the text
 * verbatim is a check against the real copy, not a lookalike. The interstitial
 * renders it inside a `pre-wrap` block — no client-held paraphrase exists.
 */
const STATEMENT_V1 = `AI features risk disclosure

This deployment includes AI features that process client information on the
operator's behalf. Before those features may be used, an Administrator of
this deployment must read this statement and confirm that they understand it.
Use of the AI features requires that confirmation. The confirmation is
recorded, and if this statement is ever revised, the revised statement must
be confirmed again before the AI features are used.

1. The vendor control plane retains no client information. Operating the
   service — metering and protocol coordination — uses metadata only. Client
   names, client data and document content are never sent to the vendor
   control plane.

2. AI features forward minimized, redacted snippets to the LLM platform for
   further processing. The platform is operated by SiliconFlow, the Singapore
   contracting entity, whose handling of personal data is governed by the
   PDPA. This processing occurs under the provider's standard terms.

3. This acknowledgment does not replace the per-company AI opt-in. The
   affirmative opt-in recorded for each company remains required before the
   AI features process that company's information, and a company's objection
   still disables the features for that company.
`

/**
 * The post-bump statement: a revision added the fourth paragraph, so the
 * version moved to v2 and every prior acceptance stopped counting — the GET
 * answers `acknowledged: false` on a previously-acknowledged deployment until
 * an Administrator confirms the revised text. Synthetic revision text, the
 * same wire shape.
 */
const STATEMENT_V2 = `AI features risk disclosure

This deployment includes AI features that process client information on the
operator's behalf. Before those features may be used, an Administrator of
this deployment must read this statement and confirm that they understand it.
Use of the AI features requires that confirmation. The confirmation is
recorded, and if this statement is ever revised, the revised statement must
be confirmed again before the AI features are used.

1. The vendor control plane retains no client information. Operating the
   service — metering and protocol coordination — uses metadata only. Client
   names, client data and document content are never sent to the vendor
   control plane.

2. AI features forward minimized, redacted snippets to the LLM platform for
   further processing. The platform is operated by SiliconFlow, the Singapore
   contracting entity, whose handling of personal data is governed by the
   PDPA. This processing occurs under the provider's standard terms.

3. This acknowledgment does not replace the per-company AI opt-in. The
   affirmative opt-in recorded for each company remains required before the
   AI features process that company's information, and a company's objection
   still disables the features for that company.

4. This statement is version v2. It replaces the v1 wording, and
   confirmations of the v1 text no longer satisfy this requirement: the
   revised statement must be read and confirmed before the AI features are
   used.
`

/** A deployment that has never been acknowledged: the first-run read. */
export const riskAckUnacknowledged: RiskAcknowledgment = {
  statement_version: 'v1',
  statement_text: STATEMENT_V1,
  acknowledged: false,
}

/** The same tenant after its Administrator confirmed v1. */
export const riskAckAcknowledged: RiskAcknowledgment = {
  ...riskAckUnacknowledged,
  acknowledged: true,
}

/** The same tenant after the statement was revised: the bump re-opened it. */
export const riskAckReopened: RiskAcknowledgment = {
  statement_version: 'v2',
  statement_text: STATEMENT_V2,
  acknowledged: false,
}

// --- The per-company audit compliance report (§5.1 Audit row; A1/A2) --------

const CHAIN_SCOPE =
  "verified over the tenant's full audit chain; a company-filtered subset " +
  'cannot be re-verified on its own because prev_hash links span companies'

/** A verified chain with entries across all five areas, deliberately not in
 * occurred_at order — the mapper owns newest-first. */
export const auditReport: AuditReport = {
  report_version: 1,
  generated_at: '2026-10-02T08:00:00Z',
  company: {
    company_id: ALPHA,
    name: 'Alpha Traders Limited',
    br_number: '11112222',
    cr_number: '1234567',
  },
  chain: {
    verified: true,
    checked_entries: 42,
    problems: [],
    scope: CHAIN_SCOPE,
  },
  entries: [
    {
      seq: 7,
      occurred_at: '2026-10-01T09:15:00Z',
      actor: USER,
      action: 'DRAFT_GENERATED',
      area: 'approvals',
      entity_type: 'draft',
      entity_id: 'd1000000-0000-4000-8000-000000000001',
      details: { form_type: 'nar1' },
    },
    {
      seq: 6,
      occurred_at: '2026-09-30T08:00:00Z',
      actor: USER,
      action: 'MAIL_MESSAGE_SENT',
      area: 'client_communications',
      entity_type: 'mail_message',
      entity_id: 'ee000000-0000-4000-8000-000000000002',
      details: {},
    },
    {
      seq: 3,
      occurred_at: '2026-10-02T02:05:00Z',
      actor: null,
      action: 'REMINDER_ENQUEUED',
      area: 'client_communications',
      entity_type: null,
      entity_id: null,
      details: {},
    },
    {
      seq: 5,
      occurred_at: '2026-09-30T16:40:00Z',
      actor: USER,
      action: 'CHANGE_EVENT_RECORDED',
      area: 'event_lifecycle',
      entity_type: 'change_event',
      entity_id: 'ce000000-0000-4000-8000-000000000001',
      details: { type: 'officer_appointment' },
    },
    {
      seq: 4,
      occurred_at: '2026-09-29T11:20:00Z',
      actor: USER,
      action: 'REPLY_PARSED',
      area: 'ai_suggestions',
      entity_type: 'mail_message',
      entity_id: 'ee000000-0000-4000-8000-000000000001',
      details: { outcome: 'events_extracted' },
    },
    {
      seq: 2,
      occurred_at: '2026-09-28T10:00:00Z',
      actor: USER,
      action: 'OFFICER_APPOINTMENT_CREATED',
      area: 'data_changes',
      entity_type: 'officer_appointment',
      entity_id: '0a000000-0000-4000-8000-000000000001',
      details: {},
    },
  ],
}

/** The broken-chain verdict: the problems list renders, and the scope note
 * travels with the verdict so the reader never mistakes the company filter
 * for an independently verifiable subset (ruling R6). */
export const auditReportChainProblems: AuditReport = {
  report_version: 1,
  generated_at: '2026-10-02T08:05:00Z',
  company: {
    company_id: ALPHA,
    name: 'Alpha Traders Limited',
    br_number: '11112222',
    cr_number: '1234567',
  },
  chain: {
    verified: false,
    checked_entries: 42,
    problems: ['entry 17: prev_hash mismatch'],
    scope: CHAIN_SCOPE,
  },
  entries: [],
}
