/**
 * The two field groups the company screen's officer and member forms share,
 * stated once: a new person's officer-facing fields (`PersonFormState`) and
 * the §2.5 address (`AddressFormState`). Both are pure controlled groups —
 * they render what they are given and call back with the next state — so the
 * SSR verification renders them directly, and every control carries the wire
 * field's own name.
 *
 * The §1 protected class has no control here, deliberately: `hkid_full`,
 * `hkid_check_digit`, `passport_full` and `usual_residential_address` are
 * refused by name at the API (the `src/officers.ts` docstring says why); the
 * public-page partial identifiers are ordinary fields and are offered.
 */

import type { ReactNode } from 'react'
import type { AddressFormState, PersonFormState } from '../officers'

function Field({
  label,
  hint,
  children,
}: {
  label: string
  hint?: string
  children: ReactNode
}) {
  return (
    <label>
      {label}
      {hint !== undefined && <span className="muted"> {hint}</span>}
      {children}
    </label>
  )
}

function TextField({
  name,
  label,
  hint,
  value,
  onChange,
  type = 'text',
}: {
  name: string
  label: string
  hint?: string
  value: string
  onChange: (next: string) => void
  type?: string
}) {
  return (
    <Field label={label} hint={hint}>
      <input
        name={name}
        type={type}
        value={value}
        onChange={(event) => onChange(event.target.value)}
      />
    </Field>
  )
}

export function PersonFields({
  value,
  onChange,
  legend,
}: {
  value: PersonFormState
  onChange: (next: PersonFormState) => void
  legend: string
}) {
  function set<K extends keyof PersonFormState>(field: K, fieldValue: PersonFormState[K]): void {
    onChange({ ...value, [field]: fieldValue })
  }
  return (
    <fieldset className="field-group">
      <legend>{legend}</legend>
      <p className="muted">
        A natural person needs at least one name part; a body corporate its registered English
        name. The identity fields are the public-page values — a full HKID or passport number is
        not stored, and the API refuses one by name.
      </p>
      <Field label="Person type">
        <select
          name="person_type"
          value={value.personType}
          onChange={(event) => set('personType', event.target.value as PersonFormState['personType'])}
        >
          <option value="natural_person">Natural person</option>
          <option value="body_corporate">Body corporate</option>
        </select>
      </Field>
      <TextField
        name="name_en"
        label="Name (English)"
        value={value.nameEn}
        onChange={(next) => set('nameEn', next)}
      />
      <TextField
        name="name_zh"
        label="Name (Chinese)"
        value={value.nameZh}
        onChange={(next) => set('nameZh', next)}
      />
      <TextField
        name="surname_en"
        label="Surname (English)"
        value={value.surnameEn}
        onChange={(next) => set('surnameEn', next)}
      />
      <TextField
        name="other_names_en"
        label="Other names (English)"
        value={value.otherNamesEn}
        onChange={(next) => set('otherNamesEn', next)}
      />
      <Field label="Previous names" hint="one per line; English | Chinese">
        <textarea
          name="previous_names"
          rows={2}
          placeholder="Wong Kar Ming | 黃家明"
          value={value.previousNames}
          onChange={(event) => set('previousNames', event.target.value)}
        />
      </Field>
      <Field label="Aliases" hint="one per line; English | Chinese">
        <textarea
          name="aliases"
          rows={2}
          value={value.aliases}
          onChange={(event) => set('aliases', event.target.value)}
        />
      </Field>
      <TextField
        name="email"
        label="Email"
        type="email"
        value={value.email}
        onChange={(next) => set('email', next)}
      />
      <TextField
        name="br_number"
        label="BR number"
        value={value.brNumber}
        onChange={(next) => set('brNumber', next)}
      />
      <TextField
        name="hkid_partial"
        label="HKID (partial, as published)"
        value={value.hkidPartial}
        onChange={(next) => set('hkidPartial', next)}
      />
      <TextField
        name="passport_partial"
        label="Passport (partial)"
        value={value.passportPartial}
        onChange={(next) => set('passportPartial', next)}
      />
      <TextField
        name="passport_issuing_country_region"
        label="Passport issuing country/region"
        value={value.passportIssuingCountryRegion}
        onChange={(next) => set('passportIssuingCountryRegion', next)}
      />
      <TextField
        name="tcsp_licence_no"
        label="TCSP licence number"
        value={value.tcspLicenceNo}
        onChange={(next) => set('tcspLicenceNo', next)}
      />
      <label className="checkbox">
        <input
          name="tcsp_not_required"
          type="checkbox"
          checked={value.tcspNotRequired}
          onChange={(event) => set('tcspNotRequired', event.target.checked)}
        />
        TCSP licence not required — that has to be said with a reason
      </label>
      {value.tcspNotRequired && (
        <TextField
          name="tcsp_not_required_reason"
          label="Reason the licence is not required"
          value={value.tcspNotRequiredReason}
          onChange={(next) => set('tcspNotRequiredReason', next)}
        />
      )}
    </fieldset>
  )
}

export function AddressFields({
  legend,
  value,
  onChange,
  note,
}: {
  legend: string
  value: AddressFormState
  onChange: (next: AddressFormState) => void
  note?: string
}) {
  function set(field: keyof AddressFormState, fieldValue: string): void {
    onChange({ ...value, [field]: fieldValue })
  }
  return (
    <fieldset className="field-group">
      <legend>{legend}</legend>
      {note !== undefined && <p className="muted">{note}</p>}
      <TextField name="flat" label="Flat" value={value.flat} onChange={(next) => set('flat', next)} />
      <TextField
        name="building"
        label="Building"
        value={value.building}
        onChange={(next) => set('building', next)}
      />
      <TextField
        name="street"
        label="Street"
        value={value.street}
        onChange={(next) => set('street', next)}
      />
      <TextField
        name="district"
        label="District"
        value={value.district}
        onChange={(next) => set('district', next)}
      />
      <TextField
        name="country"
        label="Country"
        hint="leave blank for a Hong Kong address"
        value={value.country}
        onChange={(next) => set('country', next)}
      />
    </fieldset>
  )
}
