/**
 * The officer and shareholding records' translations and the form state the
 * company screen's panels are built from — kept out of the views so the SSR
 * harness can call the builders directly (the `change_events.ts` /
 * `renewals.ts` precedent for a rule that outlives one render).
 *
 * It **translates and assembles, it does not decide.** The role and capacity
 * vocabularies are `change_events.ts`'s (the domain's own values, one home in
 * the frontend too); the address is §2.5's four required parts plus a country
 * that is omitted for an HK-only address. Every rule stays the API's: a body
 * built here is what a form sends, and the API's own named refusal
 * (`person_name_required`, `address_invalid`, `capacity_role_mismatch`,
 * `shareholding_invalid: <field>`, …) is what a caller who leaves the rules
 * sees, verbatim.
 *
 * Two boundaries the forms state rather than hide. The §1 protected class has
 * no control anywhere here — `hkid_full`, `hkid_check_digit`, `passport_full`
 * and `usual_residential_address` are refused by name at the API and nothing
 * here can build one; the public-page partial identifiers are ordinary fields
 * and are offered. And a person's identity is corrected on the
 * Administrator-only persons surface: the appointment and shareholding forms
 * edit the record's own fields, never the person's.
 */

import type {
  Address,
  NamePair,
  OfficerAppointment,
  OfficerAppointmentCreate,
  OfficerAppointmentUpdate,
  OfficerCapacity,
  OfficerRole,
  PersonSnapshot,
  PersonType,
  PersonWrite,
  Shareholding,
  ShareholdingCreate,
  ShareholdingUpdate,
  TcspLicence,
} from './api/types'
import { capacitiesForRole, capacityLabel, officerRoleLabel } from './change_events'

// --- The vocabularies, as words ---------------------------------------------

/** The three offices an appointment may record (§2.4). */
export const OFFICER_ROLES: OfficerRole[] = ['company_secretary', 'director', 'reserve_director']

const PERSON_TYPE_LABELS: Record<PersonType, string> = {
  natural_person: 'Natural person',
  body_corporate: 'Body corporate',
}

export function personTypeLabel(personType: string): string {
  return PERSON_TYPE_LABELS[personType as PersonType] ?? personType
}

export { capacityLabel, officerRoleLabel }

/** The role and capacity words an appointment row shows, or `—` when none. */
export function capacityText(capacity: OfficerCapacity[]): string {
  return capacity.length === 0 ? '—' : capacity.map((value) => capacityLabel(value)).join(', ')
}

/** A person's name as a row shows it: English, else Chinese, else the parts. */
export function personName(person: PersonSnapshot): string {
  const parts = [person.surname_en, person.other_names_en].filter(
    (part): part is string => part !== null && part !== '',
  )
  const english = person.name_en ?? (parts.length > 0 ? parts.join(' ') : null)
  const text = english !== null && english.trim() !== '' ? english : null
  if (text !== null) return person.name_zh === null ? text : `${text} · ${person.name_zh}`
  return person.name_zh ?? 'No name on record'
}

// --- The service window and the address, as rendered -------------------------

/**
 * Whether a record holds office as of a day — the API's own half-open window
 * (`valid_from <= as_of < valid_to`), rendered rather than re-derived from
 * anything else. `asOf` and the dates are `YYYY-MM-DD`, which compares as
 * text.
 */
export function inOffice(validFrom: string, validTo: string | null, asOf: string): boolean {
  if (asOf < validFrom) return false
  return validTo === null || asOf < validTo
}

/** The §2.5 address as one line, with an absent country simply omitted. */
export function addressLine(address: Address | null): string {
  if (address === null) return '—'
  return [address.flat, address.building, address.street, address.district, address.country]
    .filter((part) => part !== null && part !== undefined && part !== '')
    .join(', ')
}

/** The §2.5 address as the request carries it: no country key for HK-only. */
export function addressFrom(form: AddressFormState): Address | null {
  const address: Address = {
    flat: form.flat.trim(),
    building: form.building.trim(),
    street: form.street.trim(),
    district: form.district.trim(),
  }
  const country = form.country.trim()
  if (country !== '') address.country = country
  const stated = Object.values(address).some((part) => part !== '')
  return stated ? address : null
}

export function addressFormFrom(address: Address | null): AddressFormState {
  return {
    flat: address?.flat ?? '',
    building: address?.building ?? '',
    street: address?.street ?? '',
    district: address?.district ?? '',
    country: address?.country ?? '',
  }
}

export function blankAddress(): AddressFormState {
  return { flat: '', building: '', street: '', district: '', country: '' }
}

export interface AddressFormState {
  flat: string
  building: string
  street: string
  district: string
  country: string
}

// --- The person's fields (the add forms' inline person) ----------------------

/**
 * A new person's officer-facing fields, as an add form holds them. Every
 * string is a text input's own; blank means "not stated", which is what the
 * builders below leave out of the body (and the API's own refusal names a
 * requirement a blank field leaves unmet).
 */
export interface PersonFormState {
  personType: PersonType
  nameZh: string
  nameEn: string
  surnameEn: string
  otherNamesEn: string
  /** One name per line; the English and Chinese forms separated by `|`. */
  previousNames: string
  aliases: string
  email: string
  brNumber: string
  hkidPartial: string
  passportPartial: string
  passportIssuingCountryRegion: string
  tcspLicenceNo: string
  tcspNotRequired: boolean
  tcspNotRequiredReason: string
}

export function blankPerson(): PersonFormState {
  return {
    personType: 'natural_person',
    nameZh: '',
    nameEn: '',
    surnameEn: '',
    otherNamesEn: '',
    previousNames: '',
    aliases: '',
    email: '',
    brNumber: '',
    hkidPartial: '',
    passportPartial: '',
    passportIssuingCountryRegion: '',
    tcspLicenceNo: '',
    tcspNotRequired: false,
    tcspNotRequiredReason: '',
  }
}

/**
 * The name-history entries a textarea holds: one name per line, the English
 * and Chinese forms separated by `|` (either side may be empty). A line that
 * states neither is not an entry.
 */
export function namePairsFrom(text: string): NamePair[] {
  const pairs: NamePair[] = []
  for (const line of text.split('\n')) {
    const [english, chinese] = line.split('|')
    const pair: NamePair = {}
    if (english !== undefined && english.trim() !== '') pair.name_en = english.trim()
    if (chinese !== undefined && chinese.trim() !== '') pair.name_zh = chinese.trim()
    if (pair.name_en !== undefined || pair.name_zh !== undefined) pairs.push(pair)
  }
  return pairs
}

/**
 * The person's fields as the create bodies carry them (`extra="forbid"`: only
 * what the form states travels, so a blank input is an absent key).
 */
export function personWriteFrom(form: PersonFormState): PersonWrite {
  const licence: TcspLicence | undefined = form.tcspNotRequired
    ? {
        licence_no: null,
        not_required: true,
        not_required_reason: text(form.tcspNotRequiredReason),
      }
    : text(form.tcspLicenceNo) === undefined
      ? undefined
      : { licence_no: text(form.tcspLicenceNo), not_required: false }
  const person: PersonWrite = {
    person_type: form.personType,
    name_zh: text(form.nameZh),
    name_en: text(form.nameEn),
    surname_en: text(form.surnameEn),
    other_names_en: text(form.otherNamesEn),
    previous_names: namePairsFrom(form.previousNames),
    aliases: namePairsFrom(form.aliases),
    email: text(form.email),
    br_number: text(form.brNumber),
    hkid_partial: text(form.hkidPartial),
    passport_partial: text(form.passportPartial),
    passport_issuing_country_region: text(form.passportIssuingCountryRegion),
    tcsp_licence: licence,
  }
  for (const key of Object.keys(person) as Array<keyof PersonWrite>) {
    if (person[key] === undefined) delete person[key]
  }
  return person
}

function text(value: string): string | undefined {
  const trimmed = value.trim()
  return trimmed === '' ? undefined : trimmed
}

// --- The appointment's fields ------------------------------------------------

export interface AppointmentFormState {
  role: OfficerRole
  capacities: OfficerCapacity[]
  alternateTo: string
  correspondence: AddressFormState
  registeredOffice: AddressFormState
  validFrom: string
  validTo: string
}

export function blankAppointment(): AppointmentFormState {
  return {
    role: 'director',
    capacities: [],
    alternateTo: '',
    correspondence: blankAddress(),
    registeredOffice: blankAddress(),
    validFrom: '',
    validTo: '',
  }
}

/**
 * The appointment fields a create body states: the role, the boxes ticked,
 * `alternate_to` exactly when the alternate box is ticked (the API's own
 * "required exactly when" rule — a name without the tick is
 * `alternate_to_not_applicable`), the one address the person's type files
 * (§2.4's purpose split), and the window.
 */
export function appointmentCreateFrom(
  form: AppointmentFormState,
  person: PersonFormState,
): OfficerAppointmentCreate {
  const creates: OfficerAppointmentCreate = {
    person: personWriteFrom(person),
    role: form.role,
    capacity: [...form.capacities],
    valid_from: form.validFrom,
  }
  const alternate = text(form.alternateTo)
  if (form.capacities.includes('alternate_director') && alternate !== undefined) {
    creates.alternate_to = alternate
  }
  if (text(form.validTo) !== undefined) creates.valid_to = text(form.validTo)
  if (person.personType === 'body_corporate') {
    const office = addressFrom(form.registeredOffice)
    if (office !== null) creates.registered_office_address = office
  } else {
    const correspondence = addressFrom(form.correspondence)
    if (correspondence !== null) creates.correspondence_address = correspondence
  }
  return creates
}

/**
 * The appointment fields a PATCH states: everything the form holds, because
 * the form *is* the record's new value. The address it does not apply to is
 * sent as an explicit `null` — the API clears a legal field and refuses the
 * one the person's type does not carry, rather than this screen deciding.
 */
export function appointmentChangesFrom(form: AppointmentFormState): OfficerAppointmentUpdate {
  const alternate = text(form.alternateTo)
  return {
    role: form.role,
    capacity: [...form.capacities],
    alternate_to: form.capacities.includes('alternate_director') ? (alternate ?? null) : null,
    valid_from: form.validFrom,
    valid_to: text(form.validTo) ?? null,
    correspondence_address: addressFrom(form.correspondence),
    registered_office_address: addressFrom(form.registeredOffice),
  }
}

/** The edit form's initial state, read back from the record it edits. */
export function appointmentFormFrom(record: OfficerAppointment): AppointmentFormState {
  return {
    role: record.role,
    capacities: [...record.capacity],
    alternateTo: record.alternate_to ?? '',
    correspondence: addressFormFrom(record.correspondence_address),
    registeredOffice: addressFormFrom(record.registered_office_address),
    validFrom: record.valid_from,
    validTo: record.valid_to ?? '',
  }
}

/** The capacity boxes the role's own office prints, for the form's group. */
export function capacityChoices(role: OfficerRole): OfficerCapacity[] {
  return capacitiesForRole(role)
}

/**
 * The same form with another role: a capacity box the new office does not
 * print is dropped rather than sent for the API to refuse
 * (``capacity_role_mismatch``).
 */
export function appointmentWithRole(
  form: AppointmentFormState,
  role: OfficerRole,
): AppointmentFormState {
  const boxes = capacityChoices(role)
  return { ...form, role, capacities: form.capacities.filter((box) => boxes.includes(box)) }
}

// --- The shareholding's fields -----------------------------------------------

export interface ShareholdingFormState {
  class_name: string
  shares_held: string
  jointly_held: string
  remarks: string
  percentage_held: string
  allotment_date: string
  amount_paid: string
  amount_unpaid: string
  address: AddressFormState
  validFrom: string
  validTo: string
}

export function blankShareholding(): ShareholdingFormState {
  return {
    class_name: '',
    shares_held: '',
    jointly_held: '',
    remarks: '',
    percentage_held: '',
    allotment_date: '',
    amount_paid: '',
    amount_unpaid: '',
    address: blankAddress(),
    validFrom: '',
    validTo: '',
  }
}

export function shareholdingFormFrom(record: Shareholding): ShareholdingFormState {
  return {
    class_name: record.class_name,
    shares_held: String(record.shares_held),
    jointly_held: record.jointly_held ?? '',
    remarks: record.remarks ?? '',
    percentage_held: record.percentage_held ?? '',
    allotment_date: record.allotment_date ?? '',
    amount_paid: record.amount_paid ?? '',
    amount_unpaid: record.amount_unpaid ?? '',
    address: addressFormFrom(record.address),
    validFrom: record.valid_from,
    validTo: record.valid_to ?? '',
  }
}

/**
 * The member fields a create body states. `shares_held` crosses as the number
 * the API wants; the money and the percentage stay **strings** — the house
 * `Numeric` convention, and the API refuses a value the member boxes cannot
 * carry as `shareholding_invalid: <field>`.
 */
export function shareholdingCreateFrom(
  form: ShareholdingFormState,
  person: PersonFormState,
): ShareholdingCreate {
  const creates: ShareholdingCreate = {
    person: personWriteFrom(person),
    class_name: form.class_name.trim(),
    shares_held: sharesFrom(form.shares_held),
    valid_from: form.validFrom,
  }
  for (const [key, value] of [
    ['jointly_held', text(form.jointly_held)],
    ['remarks', text(form.remarks)],
    ['percentage_held', text(form.percentage_held)],
    ['allotment_date', text(form.allotment_date)],
    ['amount_paid', text(form.amount_paid)],
    ['amount_unpaid', text(form.amount_unpaid)],
    ['valid_to', text(form.validTo)],
  ] as const) {
    if (value !== undefined) creates[key] = value
  }
  const address = addressFrom(form.address)
  if (address !== null) creates.address = address
  return creates
}

/** The member fields a PATCH states; a blank optional field is cleared (null). */
export function shareholdingChangesFrom(form: ShareholdingFormState): ShareholdingUpdate {
  return {
    class_name: form.class_name.trim(),
    shares_held: sharesFrom(form.shares_held),
    jointly_held: text(form.jointly_held) ?? null,
    remarks: text(form.remarks) ?? null,
    percentage_held: text(form.percentage_held) ?? null,
    allotment_date: text(form.allotment_date) ?? null,
    amount_paid: text(form.amount_paid) ?? null,
    amount_unpaid: text(form.amount_unpaid) ?? null,
    address: addressFrom(form.address),
    valid_from: form.validFrom,
    valid_to: text(form.validTo) ?? null,
  }
}

/** A share count as the integer the API takes; a blank or bad entry is 0, which
 * the API's own `shareholding_invalid: shares_held` names when it matters. */
function sharesFrom(value: string): number {
  const parsed = Number(value.trim())
  return Number.isFinite(parsed) ? Math.trunc(parsed) : 0
}
