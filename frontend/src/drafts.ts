/**
 * The draft lifecycle's translations and the readers the draft screens render
 * `resolved_state`, `field_provenance`, `qc_flags` and `filing_metadata`
 * through — kept out of the views so the SSR harness can call the rules
 * directly (the `change_events.ts` / `renewals.ts` precedent for a rule that
 * outlives one render).
 *
 * It **translates and reads, it does not decide.** The status and action
 * vocabularies are `app.domain.drafts`' own; the readers below take the JSON
 * documents' shapes as the backend writes them and show what they state — an
 * unknown key or value is shown raw rather than given an invented meaning,
 * and an empty document renders as its own empty state. Nothing here holds a
 * §4.2 rule: which actions may be offered is the transitions read's answer
 * alone, and what a draft resolved is the resolution's.
 */

import type { DraftTransitionRequest, FilingMetadata, FormVersion, SignatureStatus } from './api/types'
import { targetLabel } from './format'

// --- The vocabularies, as words ---------------------------------------------

/** The nine statuses of §4.2, in the product's words. */
const STATUS_LABELS: Record<string, string> = {
  generated: 'Generated',
  under_review: 'Under review',
  sent_for_signature: 'Sent for signature',
  signature_received: 'Signature received',
  reviewed: 'Reviewed — awaiting approval',
  approved: 'Approved',
  exported: 'Exported',
  rejected: 'Rejected',
  invalidated: 'Invalidated',
}

export function draftStatusLabel(status: string): string {
  return STATUS_LABELS[status] ?? status
}

/** `signature_status` (§2.8), in the product's words. */
const SIGNATURE_LABELS: Record<SignatureStatus, string> = {
  not_sent: 'Not sent to the client',
  sent_for_signature: 'Sent for signature',
  signature_received: 'Signed copy received',
}

export function signatureStatusLabel(status: string): string {
  return SIGNATURE_LABELS[status as SignatureStatus] ?? status
}

/** Every action §4.2 names; the API offers only the wired, leg-admitted ones. */
const ACTION_LABELS: Record<string, string> = {
  edit: 'Edit the filing metadata',
  mark_under_review: 'Start review',
  send_for_signature: 'Send for signature',
  upload_signed: 'Upload the signed copy',
  recall: 'Recall',
  mark_reviewed: 'Mark reviewed',
  reject: 'Reject',
  approve: 'Approve',
  export: 'Export',
  regenerate: 'Regenerate',
  invalidate: 'Invalidate',
}

export function draftActionLabel(action: string): string {
  return ACTION_LABELS[action] ?? action
}

/**
 * One line about a wired action, for beside its button — or `null` for one
 * this build does not wire (the transitions read never offers those, so no
 * sentence is written for them). Every §4.2 row is wired now; the sentences
 * below say what the press does and what a human should know first.
 */
export function draftActionNote(action: string): string | null {
  if (action === 'edit') {
    return 'states the filing metadata — the returned financial period, the delivery channel, the presentor, the signature and the consent — on this same version'
  }
  if (action === 'mark_under_review') {
    return 'moves the draft from generated to under review'
  }
  if (action === 'send_for_signature') {
    return 'fills the pinned CR form from this version’s resolved values, stores the PDF as a WORM document, and moves the draft to sent for signature — the reply lists what the fill left for a human'
  }
  if (action === 'upload_signed') {
    return 'stores the client-signed PDF as a WORM document and moves the draft to signature received — the file picker below posts the file itself to the signature route'
  }
  if (action === 'recall') {
    return 'walks the draft back from sent for signature to under review — the released package is superseded, and the client is told by hand'
  }
  if (action === 'mark_reviewed') {
    return 'records the review stamp — the first layer of the two-layer gate; the draft then awaits approval'
  }
  if (action === 'reject') {
    return 'refuses the draft with a recorded reason — a rejection at either layer lands on the same status, and regenerating rebuilds the draft from current master data'
  }
  if (action === 'approve') {
    return 'records the approval stamp — the last human gate before export; an NAR1 making the item-16 declaration asks the approver to confirm it first'
  }
  if (action === 'export') {
    return 'assembles the filing package in the chosen mode — the flattened PDF or the JSON artifact with its helper document, all stored as WORM documents; no mode submits anything'
  }
  if (action === 'regenerate') {
    return 'appends the next version of this rejected lineage, resolved fresh from current master data — it rides the generation seam, so the tier’s automation gate answers first'
  }
  return null
}

// --- The export’s mode vocabulary and readers (§2.12) -------------------------

/** §2.12's closed vocabulary, in the order the chooser offers it. */
export const EXPORT_MODES = ['e_registry_prep', 'paper', 'api_ready'] as const

const EXPORT_MODE_LABELS: Record<string, string> = {
  e_registry_prep: 'e-Registry prep',
  paper: 'Paper lodgement',
  api_ready: 'API-ready JSON',
}

/** The mode as the chooser names it; an unknown value prints as itself. */
export function exportModeLabel(mode: string): string {
  return EXPORT_MODE_LABELS[mode] ?? mode
}

const EXPORT_MODE_DESCRIPTIONS: Record<string, string> = {
  e_registry_prep:
    'the flattened PDF for a staff-run e-Registry lodgement, plus a step-by-step helper document — nothing is submitted',
  paper:
    'the flattened PDF plus a covering checklist with the fee schedule, for posting or a counter lodgement',
  api_ready:
    'the draft’s resolved state as deterministic JSON for a future electronic channel — no companion document',
}

/** One line about what the mode produces; an unknown value gets none. */
export function exportModeDescription(mode: string): string | null {
  return EXPORT_MODE_DESCRIPTIONS[mode] ?? null
}

/**
 * Whether `approve` needs the item-16 confirmation step first (ruling R2):
 * the draft makes the private-company statement and no approver has confirmed
 * it yet. The predicate reads the draft's own two fields and nothing else; a
 * draft without a statement (or one already confirmed) approves with a plain
 * press.
 */
export function statementConfirmationPending(draft: {
  private_company_statement: boolean | null
  statement_confirmed_by: string | null
}): boolean {
  return draft.private_company_statement === true && draft.statement_confirmed_by === null
}

export interface EnvelopeRow {
  label: string
  value: string
}

/**
 * The FilingPackage's envelope as rows — the assembly's own derived counts and
 * companion ids, never re-derived here. `sheet_counts` prints one row per
 * slug, `page_pairs` the footer numbers the numbered copies printed (an empty
 * pair list prints its own honest line), and the companion ids only when the
 * mode carried one. Unknown keys are ignored: the shape is the domain's.
 */
export function filingPackageEnvelopeRows(
  envelope: Record<string, unknown>,
): EnvelopeRow[] {
  const rows: EnvelopeRow[] = []
  const counts = asRecord(envelope.sheet_counts)
  if (counts !== null) {
    for (const [slug, count] of Object.entries(counts)) {
      rows.push({
        label: `Sheets — ${slug}`,
        value: `${String(count)} ${Number(count) === 1 ? 'copy' : 'copies'}`,
      })
    }
  }
  const pairs = Array.isArray(envelope.page_pairs) ? envelope.page_pairs : []
  const printed = pairs
    .filter((pair): pair is unknown[] => Array.isArray(pair))
    .map((pair) => pair.map((page) => String(page)).join(' '))
  rows.push({
    label: 'Footer page numbers',
    value: printed.length === 0 ? 'none printed' : printed.join(', '),
  })
  const helper = valueText(envelope.helper_document_id)
  if (helper !== null) rows.push({ label: 'e-Registry helper', value: helper })
  const checklist = valueText(envelope.checklist_document_id)
  if (checklist !== null) rows.push({ label: 'Covering checklist', value: checklist })
  return rows
}

/**
 * The fill's escalation vocabulary (`app.domain.draft_fill`), in the product's
 * words. An unknown kind prints as itself — the API's own code, never hidden.
 */
const ESCALATION_LABELS: Record<string, string> = {
  missing_value: 'A box the draft does not state',
  protected_class_manual: 'Protected information, by hand',
  officer_overflow: 'An officer beyond the pinned blocks',
  member_overflow: 'A member beyond Schedule 1',
  records_overflow: 'Records kept beyond the pinned row',
  share_classes_over_4: 'Share classes beyond the grid',
  multiple_changes: 'A second change of the same kind',
}

export function signaturePackageEscalationLabel(kind: string): string {
  return ESCALATION_LABELS[kind] ?? kind
}

/** The provenance vocabulary §2.8 carries (§3.5), as the source column reads. */
export function sourceLabel(sourceType: string, sourceId: string | null): string {
  const names: Record<string, string> = {
    master_record_id: 'Master record',
    change_event_id: 'Change event',
    backfill_document_id: 'Backfilled document',
  }
  const name = names[sourceType]
  if (name === undefined) return sourceType === 'none' ? 'No source resolved' : sourceType
  return sourceId === null ? name : `${name} ${sourceId.slice(0, 8)}`
}

/** The §3.6 QC vocabulary: what each flag kind is looking at. */
const QC_KIND_LABELS: Record<string, string> = {
  missing_source: 'Critical field with no source',
  unapplied_event: 'Approved event that could not be applied',
  unexpected_change: 'Change against the prior filing no event explains',
}

export function qcFlagKindLabel(kind: string): string {
  return QC_KIND_LABELS[kind] ?? kind
}

export interface QcFlagRow {
  kind: string
  label: string
  detail: string
}

/**
 * The QC flags as rows: each flag's kind, and whatever it carries — the
 * missing path, the event it could not apply, the target and the before →
 * after of an unexplained difference. The flag's own words are the domain's;
 * nothing is summarised away.
 */
export function qcFlagRows(flags: Array<Record<string, unknown>>): QcFlagRow[] {
  return flags.map((flag) => {
    const kind = valueText(flag.kind) ?? 'unknown'
    const parts: string[] = []
    const path = valueText(flag.path) ?? valueText(flag.target)
    if (path !== null) parts.push(path)
    const eventId = valueText(flag.event_id)
    if (eventId !== null) parts.push(`event ${eventId.slice(0, 8)}`)
    const detail = valueText(flag.detail)
    if (detail !== null) parts.push(detail)
    return { kind, label: qcFlagKindLabel(kind), detail: parts.join(' — ') }
  })
}

export interface ProvenanceRow {
  /** The registry item's own id (`HDR-BRN`, …). */
  item: string
  /** The appendix-A targets the item resolves from, ` + `-joined. */
  target: string
  sourceType: string
  sourceId: string | null
  changed: boolean
}

/**
 * The provenance as rows, in the document's own key order — note that
 * JavaScript orders integer-like keys (`'1'`, `'6'`) before the rest, so a
 * registry's numeric items come first. `changed` is §2.9 step 8's highlight:
 * the value differs from the prior filing — the domain's flag, rendered rather
 * than re-derived. It does not mean "an applied event is behind this": an item
 * an event placed reads `changed: false` when the prior filing stated the
 * same, so "unchanged" never means the event went unapplied.
 */
export function provenanceRows(provenance: Record<string, unknown>): ProvenanceRow[] {
  const rows: ProvenanceRow[] = []
  for (const [item, entry] of Object.entries(provenance)) {
    const record = asRecord(entry)
    if (record === null) continue
    rows.push({
      item,
      target: valueText(record.target) ?? '—',
      sourceType: valueText(record.source_type) ?? 'none',
      sourceId: valueText(record.source_id),
      changed: record.changed === true,
    })
  }
  return rows
}

// --- The resolved values -----------------------------------------------------

export interface FactRow {
  path: string
  label: string
  value: string
}

/**
 * The company block's leaves as rows — every scalar the resolution placed,
 * labelled by the registry's own target vocabulary where it has one. The
 * share-capital classes are skipped here: they are a table of their own
 * (`shareCapitalClasses`).
 */
export function companyFactRows(state: Record<string, unknown>): FactRow[] {
  const company = asRecord(state.company)
  if (company === null) return []
  const rows: FactRow[] = []
  for (const [key, value] of Object.entries(company)) {
    if (key === 'status') continue
    flatten(`company.${key}`, value, rows, new Set(['classes']))
  }
  return rows
}

function flatten(
  path: string,
  value: unknown,
  rows: FactRow[],
  skipKeys: Set<string>,
): void {
  if (value === null || value === undefined) return
  if (Array.isArray(value)) {
    value.forEach((item, index) => flatten(`${path}[${index}]`, item, rows, skipKeys))
    return
  }
  const record = asRecord(value)
  if (record !== null) {
    for (const [key, item] of Object.entries(record)) {
      if (skipKeys.has(key)) continue
      flatten(`${path}.${key}`, item, rows, skipKeys)
    }
    return
  }
  rows.push({ path, label: targetLabel(path), value: String(value) })
}

const CLASS_KEY_LABELS: Record<string, string> = {
  class_name: 'Class',
  currency: 'Currency',
  total_shares: 'Total shares',
  total_amount: 'Total amount',
  total_paid_up: 'Paid up',
}

export interface ShareClassRow {
  label: string
  value: string
}

/** One share class's own leaves, in the class object's own order. */
export function shareClassRows(entry: Record<string, unknown>): ShareClassRow[] {
  return Object.entries(entry).map(([key, value]) => ({
    label: CLASS_KEY_LABELS[key] ?? key,
    value: value === null || value === undefined ? '—' : String(value),
  }))
}

export interface ShareCapital {
  classes: Array<Record<string, unknown>>
  total: FactRow[]
}

/** The resolved share capital: the classes as tables, the derived total as rows. */
export function shareCapital(state: Record<string, unknown>): ShareCapital {
  const company = asRecord(state.company)
  const capital = company === null ? null : asRecord(company.share_capital)
  if (capital === null) return { classes: [], total: [] }
  const total = asRecord(capital.total)
  const rows: FactRow[] = []
  if (total !== null) {
    for (const [key, value] of Object.entries(total)) {
      if (key === 'derived' || value === null || value === undefined) continue
      rows.push({
        path: `company.share_capital.total.${key}`,
        label: CLASS_KEY_LABELS[key] ?? key,
        value: String(value),
      })
    }
  }
  return { classes: listOfRecords(capital.classes), total: rows }
}

export interface ResolvedOfficer {
  role: string
  name: string
  capacity: string[]
  alternateTo: string | null
  validFrom: string
  validTo: string | null
  correspondence: string
  registeredOffice: string
  source: string
}

/** The resolved officer entries, in the resolution's own order. */
export function resolvedOfficers(state: Record<string, unknown>): ResolvedOfficer[] {
  return listOfRecords(state.officers).map((entry) => ({
    role: valueText(entry.role) ?? '—',
    name: entryName(entry),
    capacity: stringList(entry.capacity),
    alternateTo: valueText(entry.alternate_to),
    validFrom: valueText(entry.valid_from) ?? '—',
    validTo: valueText(entry.valid_to),
    correspondence: addressText(entry.correspondence_address),
    registeredOffice: addressText(entry.registered_office_address),
    source: sourceOf(entry),
  }))
}

export interface ResolvedMember {
  name: string
  className: string
  sharesHeld: string
  jointlyHeld: string | null
  percentageHeld: string | null
  amountPaid: string | null
  amountUnpaid: string | null
  allotmentDate: string | null
  address: string
  validFrom: string
  validTo: string | null
  remarks: string | null
  source: string
}

/** The resolved member entries (§3.3): F1's holdings, with no event overlay. */
export function resolvedMembers(state: Record<string, unknown>): ResolvedMember[] {
  return listOfRecords(state.shareholders).map((entry) => ({
    name: entryName(entry),
    className: valueText(entry.class_name) ?? '—',
    sharesHeld: valueText(entry.shares_held) ?? '—',
    jointlyHeld: valueText(entry.jointly_held),
    percentageHeld: valueText(entry.percentage_held),
    amountPaid: valueText(entry.amount_paid),
    amountUnpaid: valueText(entry.amount_unpaid),
    allotmentDate: valueText(entry.allotment_date),
    address: addressText(entry.address),
    validFrom: valueText(entry.valid_from) ?? '—',
    validTo: valueText(entry.valid_to),
    remarks: valueText(entry.remarks),
    source: sourceOf(entry),
  }))
}

export interface ResolvedChange {
  eventId: string
  type: string
  effectiveDate: string
  payload: Record<string, unknown>
}

/** The applied events (§3.1): the `changes` block, as the draft linked them. */
export function resolvedChanges(state: Record<string, unknown>): ResolvedChange[] {
  return listOfRecords(state.changes).map((entry) => ({
    eventId: valueText(entry.event_id) ?? '—',
    type: valueText(entry.type) ?? '—',
    effectiveDate: valueText(entry.effective_date) ?? '—',
    payload: asRecord(entry.payload) ?? {},
  }))
}

// --- The filing-metadata document (§2.8) -------------------------------------

const METADATA_LABELS: Record<string, string> = {
  'return.made_up_date': 'Return — made-up date',
  'return.fs_period_start': 'Financial period — start',
  'return.fs_period_end': 'Financial period — end',
  member_particulars_delivery: 'Member particulars delivery',
  'presentor.name': 'Presentor — name',
  'presentor.address': 'Presentor — address',
  'presentor.tel': 'Presentor — telephone',
  'presentor.fax': 'Presentor — fax',
  'presentor.email': 'Presentor — email',
  'presentor.reference': 'Presentor — reference',
  'signature.name': 'Signature — name',
  'signature.date': 'Signature — date',
  'signature.capacity': 'Signature — capacity',
  'consent.signatory_name': 'Consent — signatory name',
  'consent.signatory_capacity': 'Consent — signatory capacity',
}

/**
 * The filing-metadata document's stated leaves as rows, in §2.8's own key
 * order; a key nothing stated is simply absent (the empty document renders as
 * its own empty state).
 */
export function filingMetadataRows(metadata: Record<string, unknown>): FactRow[] {
  const rows: FactRow[] = []
  const returned = asRecord(metadata.return)
  for (const key of ['made_up_date', 'fs_period_start', 'fs_period_end']) {
    const value = returned === null ? null : valueText(returned[key])
    if (value !== null) {
      rows.push({ path: `return.${key}`, label: labelFor(`return.${key}`), value })
    }
  }
  const delivery = valueText(metadata.member_particulars_delivery)
  if (delivery !== null) {
    rows.push({
      path: 'member_particulars_delivery',
      label: labelFor('member_particulars_delivery'),
      value: deliveryLabel(delivery),
    })
  }
  for (const [key, fields] of [
    ['presentor', ['name', 'address', 'tel', 'fax', 'email', 'reference']],
    ['signature', ['name', 'date', 'capacity']],
    ['consent', ['signatory_name', 'signatory_capacity']],
  ] as const) {
    const block = asRecord(metadata[key])
    if (block === null) continue
    for (const field of fields) {
      const value = valueText(block[field])
      if (value !== null) {
        rows.push({
          path: `${key}.${field}`,
          label: labelFor(`${key}.${field}`),
          value,
        })
      }
    }
  }
  return rows
}

function labelFor(path: string): string {
  return METADATA_LABELS[path] ?? path
}

/** §3.3's delivery vocabulary (`schedule_1 | schedule_2 | cd_rom`), as chosen. */
export const DELIVERY_CHOICES = ['schedule_1', 'schedule_2', 'cd_rom'] as const

const DELIVERY_LABELS: Record<string, string> = {
  schedule_1: 'Schedule 1',
  schedule_2: 'Schedule 2',
  cd_rom: 'CD-ROM',
}

export function deliveryLabel(value: string): string {
  return DELIVERY_LABELS[value] ?? value
}

/**
 * The channels this build cannot honour, with the reason the dropdown shows:
 * `cb_2_P.8` is the **listed** company's Schedule 2 and listed companies are
 * outside Wave 1 — the fill plan refuses to tick it and names the refusal —
 * while `cd_rom` is the TCSP's own physical delivery, the form ticking only the
 * channel. A stored `schedule_2` still reads back (`DELIVERY_CHOICES` is the
 * API's grammar), so a draft that states it can be moved off it.
 */
export const DELIVERY_DISABLED_REASONS: Record<string, string> = {
  schedule_2: 'listed companies are outside Wave 1',
}

export interface FilingMetadataFormState {
  fsPeriodStart: string
  fsPeriodEnd: string
  /** '' clears the stored channel (the API's explicit null). */
  delivery: string
  presentorName: string
  presentorAddress: string
  presentorTel: string
  presentorFax: string
  presentorEmail: string
  presentorReference: string
  signatureName: string
  signatureDate: string
  signatureCapacity: string
  consentName: string
  consentCapacity: string
  /** `''` keeps the stored statement, `'true'` states it, `'clear'` nulls it. */
  statement: '' | 'true' | 'clear'
}

/** The edit form's initial state, read back from the version it edits. */
export function filingMetadataFormFrom(draft: {
  filing_metadata: Record<string, unknown>
  private_company_statement: boolean | null
}): FilingMetadataFormState {
  const metadata = draft.filing_metadata
  const returned = asRecord(metadata.return) ?? {}
  const presentor = asRecord(metadata.presentor) ?? {}
  const signature = asRecord(metadata.signature) ?? {}
  const consent = asRecord(metadata.consent) ?? {}
  const delivery = valueText(metadata.member_particulars_delivery) ?? ''
  const statement: FilingMetadataFormState['statement'] =
    draft.private_company_statement === null
      ? ''
      : draft.private_company_statement
        ? 'true'
        : 'clear'
  return {
    fsPeriodStart: valueText(returned.fs_period_start) ?? '',
    fsPeriodEnd: valueText(returned.fs_period_end) ?? '',
    delivery: DELIVERY_CHOICES.includes(delivery as (typeof DELIVERY_CHOICES)[number])
      ? delivery
      : '',
    presentorName: valueText(presentor.name) ?? '',
    presentorAddress: valueText(presentor.address) ?? '',
    presentorTel: valueText(presentor.tel) ?? '',
    presentorFax: valueText(presentor.fax) ?? '',
    presentorEmail: valueText(presentor.email) ?? '',
    presentorReference: valueText(presentor.reference) ?? '',
    signatureName: valueText(signature.name) ?? '',
    signatureDate: valueText(signature.date) ?? '',
    signatureCapacity: valueText(signature.capacity) ?? '',
    consentName: valueText(consent.signatory_name) ?? '',
    consentCapacity: valueText(consent.signatory_capacity) ?? '',
    statement,
  }
}

/**
 * The document the form states — the fields it holds, each blank one as an
 * explicit `null` (the merge clears it). Whether it travels is
 * `draftEditRequest`'s diff. The return's **made-up date is deliberately
 * absent**: it is derived at generation, the merge runs one level into
 * `return`, and a key the edit does not state keeps what the version holds.
 */
export function filingMetadataFrom(form: FilingMetadataFormState): FilingMetadata {
  return {
    return: {
      fs_period_start: text(form.fsPeriodStart),
      fs_period_end: text(form.fsPeriodEnd),
    },
    member_particulars_delivery: text(form.delivery),
    presentor: {
      name: text(form.presentorName),
      address: text(form.presentorAddress),
      tel: text(form.presentorTel),
      fax: text(form.presentorFax),
      email: text(form.presentorEmail),
      reference: text(form.presentorReference),
    },
    signature: {
      name: text(form.signatureName),
      date: text(form.signatureDate),
      capacity: text(form.signatureCapacity),
    },
    consent: {
      signatory_name: text(form.consentName),
      signatory_capacity: text(form.consentCapacity),
    },
  }
}

/** The statement the edit states, or `undefined` when the form keeps it. */
export function statementFrom(form: FilingMetadataFormState): boolean | null | undefined {
  if (form.statement === '') return undefined
  return form.statement === 'true' ? true : null
}

/**
 * The `edit` body the form posts: **only what the save would change travels**.
 *
 * The diff is against the version's own document, and it is taken in the
 * form's own terms: `filingMetadataFormFrom` reads the stored document back
 * into a form state, `filingMetadataFrom` builds the document that state
 * would state, and the two documents — both from the same builder, so
 * comparing them is comparing the form with itself — decide whether
 * `filing_metadata` travels at all. A save that would leave the document as it
 * stands therefore states **no fields**, and the API refuses it with
 * `edit: nothing to change` instead of accepting a revision that says nothing
 * and moving the draft from `generated` to `under_review` with an audit entry
 * for a no-op. The statement rides only when the form states one the version
 * does not already hold (an explicit `null` is a change when the version holds
 * a value, nothing when it already holds none).
 */
export function draftEditRequest(
  draft: { filing_metadata: Record<string, unknown>; private_company_statement: boolean | null },
  form: FilingMetadataFormState,
): DraftTransitionRequest {
  const request: DraftTransitionRequest = { action: 'edit' }
  const stated = filingMetadataFrom(form)
  const current = filingMetadataFrom(filingMetadataFormFrom(draft))
  if (JSON.stringify(stated) !== JSON.stringify(current)) {
    request.filing_metadata = stated
  }
  const statement = statementFrom(form)
  if (statement !== undefined && statement !== draft.private_company_statement) {
    request.private_company_statement = statement
  }
  return request
}

function text(value: string): string | null {
  const trimmed = value.trim()
  return trimmed === '' ? null : trimmed
}

// --- Small readers -----------------------------------------------------------

/** The company's name as the resolved block carries it (English first). */
export function resolvedCompanyName(state: Record<string, unknown>): string | null {
  const company = asRecord(state.company)
  const name = company === null ? null : asRecord(company.name)
  if (name === null) return null
  return valueText(name.name_en) ?? valueText(name.name_zh) ?? valueText(name.business_name)
}

/** The ADR-0010 pin as one line. */
export function formVersionText(version: FormVersion): string {
  return `specification ${version.specification_no}`
}

function entryName(entry: Record<string, unknown>): string {
  const english = valueText(entry.name_en)
  if (english !== null) return english
  const parts = [valueText(entry.surname_en), valueText(entry.other_names_en)].filter(
    (part): part is string => part !== null,
  )
  if (parts.length > 0) return parts.join(' ')
  return valueText(entry.name_zh) ?? 'No name on record'
}

function addressText(value: unknown): string {
  const address = asRecord(value)
  if (address === null) return '—'
  const parts = ['flat', 'building', 'street', 'district', 'country']
    .map((key) => valueText(address[key]))
    .filter((part): part is string => part !== null)
  return parts.length === 0 ? '—' : parts.join(', ')
}

function sourceOf(entry: Record<string, unknown>): string {
  const source = asRecord(entry.source)
  if (source === null) return 'No source resolved'
  return sourceLabel(valueText(source.type) ?? 'none', valueText(source.id))
}

export function asRecord(value: unknown): Record<string, unknown> | null {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) return null
  return value as Record<string, unknown>
}

export function listOfRecords(value: unknown): Array<Record<string, unknown>> {
  if (!Array.isArray(value)) return []
  return value
    .map((item) => asRecord(item))
    .filter((item): item is Record<string, unknown> => item !== null)
}

export function valueText(value: unknown): string | null {
  if (value === null || value === undefined) return null
  const text = String(value).trim()
  return text === '' ? null : text
}

function stringList(value: unknown): string[] {
  if (!Array.isArray(value)) return []
  return value.filter((item): item is string => typeof item === 'string')
}
