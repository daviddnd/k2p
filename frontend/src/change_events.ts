/**
 * The change-event lifecycle's translations and the typed-payload grammar the
 * entry and edit forms are built from — kept out of the views so the SSR
 * harness can call the rules directly (the `renewals.ts` / `coverage.ts`
 * precedent for a rule that outlives one render).
 *
 * It **translates and assembles, it does not decide**. The vocabularies are the
 * domain's (`backend/app/domain/change_events.py`), the field sets mirror the
 * grammar's closed key sets per event type, and every rule stays the API's: a
 * payload built here is what the form sends, and the API's own refusal —
 * verbatim — is what a caller who leaves the grammar sees.
 *
 * The §1 protected class is deliberately not in what the form can build:
 * `hkid_full`, `hkid_check_digit`, `passport_full` and
 * `usual_residential_address` are refused by name at the API, and
 * `OFFERED_OFFICER_KEYS` leaves them out, so the form cannot send one by
 * accident. The public-page partial identifiers are ordinary fields and are
 * offered.
 */

import type {
  CessationReason,
  ChangeEvent,
  ChangeEventAction,
  ChangeEventEditFields,
  ChangeEventSourceType,
  ChangeEventType,
  ChangeWindow,
  CreateChangeEventRequest,
  OfficerCapacity,
  OfficerRole,
} from './api/types'
import { formatDate, formatDaysRemaining } from './format'

// --- The vocabularies, as words ---------------------------------------------

/**
 * The six event types a v1.0 entry may carry. `company_name_change` is
 * deliberately out: §2.6 reserves it for Wave 2/NNC2, and the API refuses it
 * by name (`reserved_event_type`) — a form that offered it would fail on every
 * submit.
 */
export const ENTERABLE_CHANGE_EVENT_TYPES: ChangeEventType[] = [
  'director_appointment',
  'secretary_appointment',
  'director_cessation',
  'secretary_cessation',
  'registered_office_change',
  'other',
]

const TYPE_LABELS: Record<string, string> = {
  director_appointment: 'Director appointment',
  director_cessation: 'Director cessation',
  secretary_appointment: 'Secretary appointment',
  secretary_cessation: 'Secretary cessation',
  registered_office_change: 'Registered office change',
  company_name_change: 'Company name change',
  other: 'Other change',
}

export function changeEventTypeLabel(type: string): string {
  return TYPE_LABELS[type] ?? type
}

/** The nine statuses of §4.1, in the product's words. */
const STATUS_LABELS: Record<string, string> = {
  pending: 'Pending review',
  client_reconfirming: 'With the client for reconfirmation',
  under_review: 'Under review',
  reviewed: 'Reviewed — awaiting approval',
  confirmed: 'Confirmed',
  applied_to_draft: 'Applied to a draft',
  submitted: 'Submitted',
  withdrawn: 'Withdrawn',
  deleted: 'Deleted',
}

export function changeEventStatusLabel(status: string): string {
  return STATUS_LABELS[status] ?? status
}

/** How the event got here (§2.6). */
const SOURCE_LABELS: Record<ChangeEventSourceType, string> = {
  communication: 'A client communication',
  document: 'A document',
  manual: 'Entered by staff',
}

export function changeEventSourceLabel(source: string): string {
  return SOURCE_LABELS[source as ChangeEventSourceType] ?? source
}

/** Every action §4.1 names; the API offers only the wired, leg-admitted ones. */
const ACTION_LABELS: Record<string, string> = {
  mark_under_review: 'Start review',
  send_reconfirmation: 'Send for reconfirmation',
  override: 'Override',
  client_reply: 'Record the client’s reply',
  mark_reviewed: 'Mark reviewed',
  revert: 'Revert',
  reject: 'Reject',
  confirm: 'Confirm',
  apply: 'Apply to the draft',
  submit: 'Submit',
  supersede: 'Supersede',
  edit: 'Edit',
  delete: 'Delete',
  client_withdraw: 'Record client withdrawal',
}

export function changeEventActionLabel(action: string): string {
  return ACTION_LABELS[action] ?? action
}

/** Whether pressing this action opens a form or a confirmation first. */
export function actionOpensForm(action: ChangeEventAction): boolean {
  return (
    action === 'reject' || action === 'client_withdraw' || action === 'edit' || action === 'delete'
  )
}

/**
 * Whether pressing the action needs the caller's input before it can post: the
 * read's own `rationale_required` flag, or one of the forms (edit's fields,
 * delete's confirmation). Driven by the read, so a rationale-required action
 * this build has not seen before still collects its reason.
 */
export function actionNeedsInput(action: ChangeEventAction, rationaleRequired: boolean): boolean {
  return rationaleRequired || actionOpensForm(action)
}

/**
 * What the action does, in one line, for the wired actions — `null` for one
 * this build does not offer (the API never offers an unwired action, so a
 * button for one would be a state that cannot happen).
 */
const ACTION_NOTES: Record<string, string> = {
  mark_under_review: 'take the event up: it moves to under review',
  mark_reviewed: 'record that the review is done — approval (confirm) is the next step',
  revert: 'move the event back one step in the lifecycle',
  reject: 'refuse the event: it lands deleted (nothing is removed), and the reason is required',
  confirm: 'approve the event — it becomes confirmed and ready for a draft',
  edit: 'revise what the event says: the edit becomes a new version and the earlier ones stay',
  delete: 'set the event deleted (nothing is removed); it stays visible on the company’s list',
  client_withdraw:
    'record a withdrawal the client gave by phone or in person — staff act for the client, and the reason is required',
}

export function changeEventActionNote(action: string): string | null {
  return ACTION_NOTES[action] ?? null
}

const ROLE_LABELS: Record<string, string> = {
  company_secretary: 'Company secretary',
  director: 'Director',
  reserve_director: 'Reserve director',
}

export function officerRoleLabel(role: string): string {
  return ROLE_LABELS[role] ?? role
}

/** ND2A's capacity checkboxes, in the registry's own values. */
const CAPACITY_LABELS: Record<string, string> = {
  company_secretary: 'Company secretary (公司秘書)',
  director: 'Director (董事)',
  alternate_director: 'Alternate director (候補董事)',
}

export function capacityLabel(capacity: string): string {
  return CAPACITY_LABELS[capacity] ?? capacity
}

const CESSATION_REASON_LABELS: Record<string, string> = {
  resignation_others: 'Resignation or other (non-death)',
  deceased: 'Deceased',
}

export function cessationReasonLabel(reason: string): string {
  return CESSATION_REASON_LABELS[reason] ?? reason
}

/**
 * The 15-day window as one sentence. The countdown's own facts are the
 * backend's (`is_late` is False on the deadline day, `days_remaining` goes
 * negative past it); this only says them.
 */
export function describeChangeWindow(window: ChangeWindow): string {
  const deadline = formatDate(window.deadline)
  if (window.is_late) {
    return `The 15-day window closed on ${deadline} — ${-window.days_remaining} day${
      window.days_remaining === -1 ? '' : 's'
    } past it.`
  }
  if (window.days_remaining === 0) return `The 15-day window ends today (${deadline}).`
  return `The 15-day window ends ${deadline} — ${formatDaysRemaining(window.days_remaining)}.`
}

// --- The typed-payload grammar ----------------------------------------------

/** The four §1 protected-class fields, refused by name at the API. */
export const PROTECTED_CLASS_FIELDS = [
  'hkid_full',
  'hkid_check_digit',
  'passport_full',
  'usual_residential_address',
] as const

const ROLES_BY_TYPE: Partial<Record<ChangeEventType, OfficerRole[]>> = {
  director_appointment: ['director', 'reserve_director'],
  director_cessation: ['director', 'reserve_director'],
  secretary_appointment: ['company_secretary'],
  secretary_cessation: ['company_secretary'],
}

/** The roles an event type can carry (the grammar's type↔role coherence). */
export function rolesForType(type: ChangeEventType): OfficerRole[] {
  return ROLES_BY_TYPE[type] ?? []
}

/** Whether the type's payload is one of the two officer shapes. */
export function isOfficerType(type: ChangeEventType): boolean {
  return rolesForType(type).length > 0
}

const CAPACITIES_BY_ROLE: Record<OfficerRole, OfficerCapacity[]> = {
  company_secretary: ['company_secretary'],
  director: ['director', 'alternate_director'],
  reserve_director: ['director', 'alternate_director'],
}

/** The boxes an officer's block prints, by office (the registry's three). */
export function capacitiesForRole(role: OfficerRole): OfficerCapacity[] {
  return CAPACITIES_BY_ROLE[role]
}

/**
 * The officer keys the form builds. Everything else the grammar recognizes is
 * carried through an edit untouched (`mergeBuiltPayload`) — the form must not
 * silently drop a part of the payload it cannot show — and the protected class
 * is not a member at all.
 */
export const OFFERED_OFFICER_KEYS: ReadonlySet<string> = new Set([
  'role',
  'capacity',
  'alternate_to',
  'name_zh',
  'name_en',
  'surname_en',
  'other_names_en',
  'hkid_partial',
  'passport_partial',
  'passport_issuing_country_region',
  'email',
  'br_number',
  'appointment_date',
  'existing_director_at_appointment',
  'cessation_date',
  'cessation_reason',
  'continues_office_after_cessation',
])

export interface OfficerFormState {
  role: OfficerRole
  nameEn: string
  nameZh: string
  surnameEn: string
  otherNamesEn: string
  capacities: OfficerCapacity[]
  alternateTo: string
  appointmentDate: string
  existingDirectorAtAppointment: boolean
  cessationDate: string
  cessationReason: CessationReason
  continuesOfficeAfterCessation: boolean
  hkidPartial: string
  passportPartial: string
  passportIssuingCountryRegion: string
  email: string
  brNumber: string
}

export interface AddressFormState {
  flat: string
  building: string
  street: string
  district: string
  country: string
}

/** One form's whole state — the create and edit forms share it. */
export interface EventFormState {
  type: ChangeEventType
  effectiveDate: string
  effectiveDateText: string
  informedDate: string
  officer: OfficerFormState
  address: AddressFormState
  summary: string
}

/** A new entry's blank form: no dates chosen, no payload invented. */
export function blankEventForm(): EventFormState {
  return {
    type: 'director_appointment',
    effectiveDate: '',
    effectiveDateText: '',
    informedDate: '',
    officer: {
      role: 'director',
      nameEn: '',
      nameZh: '',
      surnameEn: '',
      otherNamesEn: '',
      capacities: [],
      alternateTo: '',
      appointmentDate: '',
      existingDirectorAtAppointment: false,
      cessationDate: '',
      cessationReason: 'resignation_others',
      continuesOfficeAfterCessation: false,
      hkidPartial: '',
      passportPartial: '',
      passportIssuingCountryRegion: '',
      email: '',
      brNumber: '',
    },
    address: { flat: '', building: '', street: '', district: '', country: '' },
    summary: '',
  }
}

/**
 * The same form with another officer role: a capacity box the new office's
 * block does not print is dropped, and with it the `alternate_to` text the
 * alternate box owns — the grammar refuses the name without its box.
 */
export function formStateForRole(form: EventFormState, role: OfficerRole): EventFormState {
  const boxes = capacitiesForRole(role)
  const capacities = form.officer.capacities.filter((box) => boxes.includes(box))
  return {
    ...form,
    officer: {
      ...form.officer,
      role,
      capacities,
      alternateTo: capacities.includes('alternate_director') ? form.officer.alternateTo : '',
    },
  }
}

/**
 * The same form switched to another type: the officer's role falls back to a
 * role the new type admits, and the capacity group follows the role. The
 * entered names stay — they are the same person's.
 */
export function formStateForType(form: EventFormState, type: ChangeEventType): EventFormState {
  const roles = rolesForType(type)
  if (roles.length === 0) return { ...form, type }
  const role = roles.includes(form.officer.role) ? form.officer.role : roles[0]
  return { ...formStateForRole({ ...form, type }, role), type }
}

function putText(target: Record<string, unknown>, key: string, value: string): void {
  const text = value.trim()
  if (text !== '') target[key] = text
}

function trimmedOrNull(value: string): string | null {
  const text = value.trim()
  return text === '' ? null : text
}

function buildAddress(address: AddressFormState): Record<string, unknown> {
  const built: Record<string, unknown> = {
    flat: address.flat.trim(),
    building: address.building.trim(),
    street: address.street.trim(),
    district: address.district.trim(),
  }
  putText(built, 'country', address.country)
  return built
}

function buildOfficer(form: EventFormState): Record<string, unknown> {
  const officer = form.officer
  const built: Record<string, unknown> = { role: officer.role }
  if (officer.capacities.length > 0) built.capacity = [...officer.capacities]
  if (officer.capacities.includes('alternate_director')) {
    putText(built, 'alternate_to', officer.alternateTo)
  }
  putText(built, 'name_zh', officer.nameZh)
  putText(built, 'name_en', officer.nameEn)
  putText(built, 'surname_en', officer.surnameEn)
  putText(built, 'other_names_en', officer.otherNamesEn)
  putText(built, 'hkid_partial', officer.hkidPartial)
  putText(built, 'passport_partial', officer.passportPartial)
  putText(built, 'passport_issuing_country_region', officer.passportIssuingCountryRegion)
  putText(built, 'email', officer.email)
  putText(built, 'br_number', officer.brNumber)
  if (form.type === 'director_cessation' || form.type === 'secretary_cessation') {
    built.cessation_date = officer.cessationDate
    built.cessation_reason = officer.cessationReason
    if (officer.continuesOfficeAfterCessation) built.continues_office_after_cessation = true
  } else {
    built.appointment_date = officer.appointmentDate
    if (officer.existingDirectorAtAppointment) built.existing_director_at_appointment = true
  }
  return built
}

/** The payload the form's state describes, in the type's own shape. */
export function buildPayload(form: EventFormState): Record<string, unknown> {
  if (form.type === 'registered_office_change') {
    return { registered_office: { address: buildAddress(form.address) } }
  }
  if (form.type === 'other') {
    return { summary: form.summary.trim() }
  }
  const shape =
    form.type === 'director_cessation' || form.type === 'secretary_cessation'
      ? 'cessation'
      : 'appointment'
  return { [shape]: { officer: buildOfficer(form) } }
}

/** The manual-entry body for `POST /companies/{id}/change-events`. */
export function createRequestFrom(form: EventFormState): CreateChangeEventRequest {
  return {
    type: form.type,
    effective_date: form.effectiveDate,
    effective_date_text: trimmedOrNull(form.effectiveDateText),
    informed_date: form.informedDate === '' ? null : form.informedDate,
    payload: buildPayload(form),
  }
}

function asObject(value: unknown): Record<string, unknown> | null {
  if (value === null || typeof value !== 'object' || Array.isArray(value)) return null
  return value as Record<string, unknown>
}

function readText(source: Record<string, unknown>, key: string): string {
  const value = source[key]
  return typeof value === 'string' ? value : ''
}

function readBoolean(source: Record<string, unknown>, key: string): boolean {
  return source[key] === true
}

function readRole(source: Record<string, unknown>, type: ChangeEventType): OfficerRole {
  const roles = rolesForType(type)
  // A type with no officer block (a registered-office change, `other`) has no
  // role to read back; the default is the one every officer type admits.
  if (roles.length === 0) return 'director'
  const value = source.role
  if (typeof value === 'string' && roles.includes(value as OfficerRole)) return value as OfficerRole
  return roles[0]
}

function readCapacities(source: Record<string, unknown>, role: OfficerRole): OfficerCapacity[] {
  const boxes = capacitiesForRole(role)
  const value = source.capacity
  if (!Array.isArray(value)) return []
  return value.filter((box): box is OfficerCapacity => boxes.includes(box as OfficerCapacity))
}

function readCessationReason(source: Record<string, unknown>): CessationReason {
  return source.cessation_reason === 'deceased' ? 'deceased' : 'resignation_others'
}

function officerPayload(event: ChangeEvent): Record<string, unknown> {
  const shape = event.type === 'director_cessation' || event.type === 'secretary_cessation'
    ? 'cessation'
    : 'appointment'
  const body = asObject(event.payload[shape])
  const officer = body === null ? null : asObject(body.officer)
  return officer ?? {}
}

/** The current version's payload, read back into the edit form's state. */
export function formStateFromChangeEvent(event: ChangeEvent): EventFormState {
  const officer = officerPayload(event)
  const role = readRole(officer, event.type)
  const registeredOffice = asObject(event.payload.registered_office)
  const address = registeredOffice === null ? null : asObject(registeredOffice.address)
  return {
    type: event.type,
    effectiveDate: event.effective_date,
    effectiveDateText: event.effective_date_text ?? '',
    informedDate: event.informed_date ?? '',
    officer: {
      role,
      nameEn: readText(officer, 'name_en'),
      nameZh: readText(officer, 'name_zh'),
      surnameEn: readText(officer, 'surname_en'),
      otherNamesEn: readText(officer, 'other_names_en'),
      capacities: readCapacities(officer, role),
      alternateTo: readText(officer, 'alternate_to'),
      appointmentDate: readText(officer, 'appointment_date'),
      existingDirectorAtAppointment: readBoolean(officer, 'existing_director_at_appointment'),
      cessationDate: readText(officer, 'cessation_date'),
      cessationReason: readCessationReason(officer),
      continuesOfficeAfterCessation: readBoolean(officer, 'continues_office_after_cessation'),
      hkidPartial: readText(officer, 'hkid_partial'),
      passportPartial: readText(officer, 'passport_partial'),
      passportIssuingCountryRegion: readText(officer, 'passport_issuing_country_region'),
      email: readText(officer, 'email'),
      brNumber: readText(officer, 'br_number'),
    },
    address: {
      flat: address === null ? '' : readText(address, 'flat'),
      building: address === null ? '' : readText(address, 'building'),
      street: address === null ? '' : readText(address, 'street'),
      district: address === null ? '' : readText(address, 'district'),
      country: address === null ? '' : readText(address, 'country'),
    },
    summary: readText(event.payload, 'summary'),
  }
}

function canonicalJson(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(',')}]`
  if (value !== null && typeof value === 'object') {
    const entries = Object.entries(value as Record<string, unknown>).sort(([left], [right]) =>
      left.localeCompare(right),
    )
    return `{${entries.map(([key, item]) => `${JSON.stringify(key)}:${canonicalJson(item)}`).join(',')}}`
  }
  return JSON.stringify(value) ?? 'null'
}

/** Whether two payloads say the same thing, key order aside. */
export function samePayload(left: unknown, right: unknown): boolean {
  return canonicalJson(left) === canonicalJson(right)
}

/**
 * The payload an edit sends for a type the event already carries: the form's
 * own fields, with every officer part the form does **not** offer (previous
 * names, aliases, the address groups, the TCSP licence) carried through from
 * the version — an edit must not silently drop what it cannot show. A field
 * the form does offer is the form's answer, so a cleared one is gone.
 */
export function mergeBuiltPayload(
  type: ChangeEventType,
  preserved: Record<string, unknown>,
  built: Record<string, unknown>,
): Record<string, unknown> {
  const shape =
    type === 'director_cessation' || type === 'secretary_cessation' ? 'cessation' : 'appointment'
  const preservedBody = asObject(preserved[shape])
  const builtBody = asObject(built[shape])
  if (preservedBody === null || builtBody === null) return built
  const preservedOfficer = asObject(preservedBody.officer)
  const builtOfficer = asObject(builtBody.officer)
  if (preservedOfficer === null || builtOfficer === null) return built
  const carried: Record<string, unknown> = {}
  for (const [key, value] of Object.entries(preservedOfficer)) {
    if (!OFFERED_OFFICER_KEYS.has(key)) carried[key] = value
  }
  return { [shape]: { officer: { ...carried, ...builtOfficer } } }
}

/**
 * The edit fields an edit **actually changes**, compared against the current
 * version: an absent field keeps what the version has, so a save that changed
 * nothing sends nothing — and the API answers its own
 * `change_event_invalid: edit: nothing to change` rather than a guess here.
 * `effective_date_text`/`informed_date` travel as an explicit `null` when
 * cleared, which is how the API distinguishes "clear" from "keep".
 */
export function editChangesFrom(current: ChangeEvent, form: EventFormState): ChangeEventEditFields {
  const changes: ChangeEventEditFields = {}
  if (form.type !== current.type) changes.type = form.type
  if (form.effectiveDate !== current.effective_date) changes.effective_date = form.effectiveDate
  const statedText = trimmedOrNull(form.effectiveDateText)
  if (statedText !== (current.effective_date_text ?? null)) changes.effective_date_text = statedText
  const informed = form.informedDate === '' ? null : form.informedDate
  if (informed !== (current.informed_date ?? null)) changes.informed_date = informed
  const payload =
    form.type === current.type
      ? mergeBuiltPayload(current.type, current.payload, buildPayload(form))
      : buildPayload(form)
  if (!samePayload(payload, current.payload)) changes.payload = payload
  return changes
}

// --- The payload, as a reader sees it ---------------------------------------

export interface PayloadRow {
  path: string
  label: string
  value: string
}

/**
 * The payload's leaves, flattened for the detail screen: the raw registry path
 * beside a label where one is known. A path this vocabulary does not know is
 * shown as the path itself — never hidden.
 */
export function payloadRows(payload: Record<string, unknown>): PayloadRow[] {
  const rows: PayloadRow[] = []
  walkPayload(payload, 'payload', '', rows)
  return rows
}

const GROUP_LABELS: Record<string, string> = {
  appointment: '',
  cessation: '',
  officer: '',
  address: '',
  registered_office: 'Registered office',
  correspondence_address: 'Correspondence address',
  registered_office_address: 'Registered office address',
}

const LEAF_LABELS: Record<string, string> = {
  role: 'Role',
  capacity: 'Capacity (registry boxes)',
  alternate_to: 'Alternate to (代替)',
  name_en: 'Name (English)',
  name_zh: 'Name (Chinese)',
  surname_en: 'Surname (English)',
  other_names_en: 'Other names (English)',
  previous_names: 'Previous names',
  aliases: 'Aliases',
  hkid_partial: 'HKID (partial)',
  passport_partial: 'Passport (partial)',
  passport_issuing_country_region: 'Passport issuing country/region',
  email: 'Email',
  br_number: 'BR number',
  appointment_date: 'Appointment date',
  existing_director_at_appointment: 'A director at appointment',
  cessation_date: 'Cessation date',
  cessation_reason: 'Cessation reason',
  continues_office_after_cessation: 'Continues the office after cessation',
  tcsp_licence: 'TCSP licence',
  licence_no: 'Licence number',
  not_required: 'Not required',
  not_required_reason: 'Why not required',
  flat: 'Flat',
  building: 'Building',
  street: 'Street',
  district: 'District',
  country: 'Country',
  summary: 'Summary',
}

function leafOf(path: string): string {
  return path.slice(path.lastIndexOf('.') + 1)
}

function leafLabel(path: string, prefix: string): string {
  const known = LEAF_LABELS[leafOf(path)]
  return known === undefined ? path : `${prefix}${known}`
}

function leafText(path: string, value: unknown): string {
  const leaf = leafOf(path)
  if (value === null) return '—'
  if (typeof value === 'boolean') return value ? 'Yes' : 'No'
  if (typeof value === 'number') return String(value)
  if (typeof value === 'string') {
    if (leaf === 'role') return officerRoleLabel(value)
    if (leaf === 'capacity') return capacityLabel(value)
    if (leaf === 'cessation_reason') return cessationReasonLabel(value)
    return value
  }
  return JSON.stringify(value)
}

function walkPayload(value: unknown, path: string, prefix: string, rows: PayloadRow[]): void {
  if (Array.isArray(value)) {
    const scalars = value.every((item) => item === null || typeof item !== 'object')
    if (scalars) {
      rows.push({
        path,
        label: leafLabel(path, prefix),
        value: value.length === 0 ? '—' : value.map((item) => leafText(path, item)).join(', '),
      })
      return
    }
    // A list of objects (a name history): element by element, numbered, so no
    // entry is silently collapsed into a JSON string.
    value.forEach((item, index) => {
      walkPayload(item, `${path}[${index}]`, `${prefix}${index + 1}. `, rows)
    })
    return
  }
  const nested = asObject(value)
  if (nested !== null) {
    const keys = Object.keys(nested)
    if (keys.length === 0) {
      rows.push({ path, label: leafLabel(path, prefix), value: '—' })
      return
    }
    for (const key of keys) {
      const group = GROUP_LABELS[key]
      const childPrefix = group === undefined || group === '' ? prefix : `${group} — `
      walkPayload(nested[key], `${path}.${key}`, childPrefix, rows)
    }
    return
  }
  rows.push({ path, label: leafLabel(path, prefix), value: leafText(path, value) })
}
