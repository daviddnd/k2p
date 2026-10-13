/**
 * The change-event entry/edit form: the typed-payload grammar per event type,
 * as controls — one component for both paths, because create
 * (`POST /companies/{id}/change-events`) and edit (`action: edit` on the
 * transitions route) send the same shape through different routes.
 *
 * It holds the form's state and calls back; the route that owns the write is
 * the caller's. A refusal is rendered as the backend's own code and words
 * (`RefusalNotice`), never swallowed — including the ones only a live submit
 * meets: `change_event_invalid: <violations>`, `protected_class_unavailable`,
 * `reserved_event_type`.
 *
 * **What it deliberately does not offer.** The §1 protected class
 * (`hkid_full`, `hkid_check_digit`, `passport_full`,
 * `usual_residential_address`) is not a control of this form: the API refuses
 * those fields by name, and a form that offered them would fail on every
 * submit. The public-page partial identifiers are ordinary fields and are
 * offered. `company_name_change` renders as a disabled option with its reason
 * (reserved for Wave 2/NNC2). The payload parts this form does not carry
 * (previous names, aliases, the address groups, the TCSP licence) are not
 * entry fields here; an edit carries the ones already on the version through
 * untouched (`mergeBuiltPayload`), so this form cannot silently drop them.
 */

import { useState, type FormEvent } from 'react'
import type { ChangeEventType, OfficerCapacity, OfficerRole } from '../api/types'
import {
  ENTERABLE_CHANGE_EVENT_TYPES,
  PROTECTED_CLASS_FIELDS,
  capacitiesForRole,
  capacityLabel,
  cessationReasonLabel,
  changeEventTypeLabel,
  formStateForRole,
  formStateForType,
  isOfficerType,
  officerRoleLabel,
  rolesForType,
  type EventFormState,
  type OfficerFormState,
} from '../change_events'
import { RefusalNotice } from './RefusalNotice'

const MAX_TEXT = 200

export function ChangeEventForm({
  legend,
  submitLabel,
  initial,
  note,
  onSubmit,
  onCancel,
}: {
  legend: string
  submitLabel: string
  initial: EventFormState
  /** What this save does, said for the path that opened the form. */
  note?: string
  onSubmit: (form: EventFormState) => Promise<void>
  onCancel: () => void
}) {
  const [form, setForm] = useState(initial)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<unknown>(null)

  function changeType(type: ChangeEventType) {
    setForm((current) => formStateForType(current, type))
  }

  function changeRole(role: OfficerRole) {
    setForm((current) => formStateForRole(current, role))
  }

  function toggleCapacity(box: OfficerCapacity) {
    setForm((current) => {
      const ticked = current.officer.capacities.includes(box)
      const capacities = ticked
        ? current.officer.capacities.filter((item) => item !== box)
        : [...current.officer.capacities, box]
      return {
        ...current,
        officer: {
          ...current.officer,
          capacities,
          alternateTo: capacities.includes('alternate_director')
            ? current.officer.alternateTo
            : '',
        },
      }
    })
  }

  function patchOfficer(patch: Partial<OfficerFormState>) {
    setForm((current) => ({ ...current, officer: { ...current.officer, ...patch } }))
  }

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    setBusy(true)
    setError(null)
    try {
      await onSubmit(form)
    } catch (caught) {
      setError(caught)
    } finally {
      setBusy(false)
    }
  }

  const cessation = form.type === 'director_cessation' || form.type === 'secretary_cessation'
  const alternateTicked = form.officer.capacities.includes('alternate_director')

  return (
    <form className="decision-form" onSubmit={submit}>
      <fieldset>
        <legend>{legend}</legend>
        <label>
          Change type
          <select
            name="type"
            value={form.type}
            onChange={(event) => changeType(event.target.value as ChangeEventType)}
          >
            {ENTERABLE_CHANGE_EVENT_TYPES.map((type) => (
              <option key={type} value={type}>
                {changeEventTypeLabel(type)}
              </option>
            ))}
            <option value="company_name_change" disabled>
              {changeEventTypeLabel('company_name_change')} — reserved for Wave 2 (NNC2)
            </option>
          </select>
        </label>

        <label>
          Effective date
          <input
            type="date"
            name="effective_date"
            required
            value={form.effectiveDate}
            onChange={(event) => setForm({ ...form, effectiveDate: event.target.value })}
          />
        </label>
        <p className="muted">
          The day the change took effect. The 15-day statutory window is counted from this date,
          never from the day the TCSP learned of it.
        </p>

        <label>
          Effective date as stated — optional
          <input
            type="text"
            name="effective_date_text"
            maxLength={500}
            value={form.effectiveDateText}
            onChange={(event) => setForm({ ...form, effectiveDateText: event.target.value })}
          />
        </label>
        <p className="muted">
          The source text the date was read from (a letter, an email), when staff are copying one.
        </p>

        <label>
          Informed date — optional
          <input
            type="date"
            name="informed_date"
            min={form.effectiveDate === '' ? undefined : form.effectiveDate}
            value={form.informedDate}
            onChange={(event) => setForm({ ...form, informedDate: event.target.value })}
          />
        </label>
        <p className="muted">
          The day the TCSP learned of the change. It may not fall before the effective date — the
          API refuses that (change_event_invalid), and the picker will not offer it.
        </p>

        {isOfficerType(form.type) && (
          <OfficerFields
            form={form}
            cessation={cessation}
            alternateTicked={alternateTicked}
            onRole={changeRole}
            onToggleCapacity={toggleCapacity}
            onPatch={patchOfficer}
          />
        )}

        {form.type === 'registered_office_change' && (
          <AddressFields
            form={form}
            onPatch={(patch) => setForm({ ...form, address: { ...form.address, ...patch } })}
          />
        )}

        {form.type === 'other' && (
          <label>
            Summary
            <input
              type="text"
              name="summary"
              maxLength={200}
              required
              value={form.summary}
              onChange={(event) => setForm({ ...form, summary: event.target.value })}
            />
          </label>
        )}
        {form.type === 'other' && (
          <p className="muted">
            One line of staff&rsquo;s own words — the one place the grammar admits prose, because an
            unclassifiable change that cannot say what it is is a row nobody can review.
          </p>
        )}

        <p className="muted">
          No §1 protected-class field is on this form:{' '}
          {PROTECTED_CLASS_FIELDS.map((field, index) => (
            <span key={field}>
              {index > 0 ? (index === PROTECTED_CLASS_FIELDS.length - 1 ? ' and ' : ', ') : ''}
              <code>{field}</code>
            </span>
          ))}{' '}
          are refused by the API by name until field-level encryption exists. The public-page
          partial identifiers are ordinary fields.{' '}
          {isOfficerType(form.type) &&
            'At least one name is required — the API refuses an officer with none.'}
        </p>

        {note !== undefined && <p className="muted">{note}</p>}

        <span className="row-actions">
          <button type="submit" disabled={busy}>
            {busy ? 'Working…' : submitLabel}
          </button>
          <button type="button" className="secondary" disabled={busy} onClick={onCancel}>
            Cancel
          </button>
        </span>

        {error !== null && <RefusalNotice error={error} />}
      </fieldset>
    </form>
  )
}

function OfficerFields({
  form,
  cessation,
  alternateTicked,
  onRole,
  onToggleCapacity,
  onPatch,
}: {
  form: EventFormState
  cessation: boolean
  alternateTicked: boolean
  onRole: (role: OfficerRole) => void
  onToggleCapacity: (box: OfficerCapacity) => void
  onPatch: (patch: Partial<OfficerFormState>) => void
}) {
  const officer = form.officer
  const boxes = capacitiesForRole(officer.role)
  return (
    <>
      <label>
        Role
        <select
          name="role"
          value={officer.role}
          onChange={(event) => onRole(event.target.value as OfficerRole)}
        >
          {rolesForType(form.type).map((role) => (
            <option key={role} value={role}>
              {officerRoleLabel(role)}
            </option>
          ))}
        </select>
      </label>

      <label>
        Name (English)
        <input
          type="text"
          name="name_en"
          maxLength={MAX_TEXT}
          value={officer.nameEn}
          onChange={(event) => onPatch({ nameEn: event.target.value })}
        />
      </label>
      <label>
        Name (Chinese)
        <input
          type="text"
          name="name_zh"
          maxLength={MAX_TEXT}
          value={officer.nameZh}
          onChange={(event) => onPatch({ nameZh: event.target.value })}
        />
      </label>
      <label>
        Surname (English) — optional
        <input
          type="text"
          name="surname_en"
          maxLength={MAX_TEXT}
          value={officer.surnameEn}
          onChange={(event) => onPatch({ surnameEn: event.target.value })}
        />
      </label>
      <label>
        Other names (English) — optional
        <input
          type="text"
          name="other_names_en"
          maxLength={MAX_TEXT}
          value={officer.otherNamesEn}
          onChange={(event) => onPatch({ otherNamesEn: event.target.value })}
        />
      </label>
      <p className="muted">
        Other names require the surname (the form&rsquo;s own name split), so the API refuses one
        without it.
      </p>

      <fieldset>
        <legend>Capacity (the registry&rsquo;s boxes)</legend>
        {boxes.map((box) => (
          <label key={box}>
            <input
              type="checkbox"
              name="capacity"
              value={box}
              checked={officer.capacities.includes(box)}
              onChange={() => onToggleCapacity(box)}
            />
            {capacityLabel(box)}
          </label>
        ))}
      </fieldset>
      {alternateTicked && (
        <label>
          Alternate to (代替) — required with the alternate-director box
          <input
            type="text"
            name="alternate_to"
            maxLength={MAX_TEXT}
            required
            value={officer.alternateTo}
            onChange={(event) => onPatch({ alternateTo: event.target.value })}
          />
        </label>
      )}
      <p className="muted">
        The alternate-director box names the director being substituted; the name is required
        exactly when that box is ticked, and refused without it.
      </p>

      {cessation ? (
        <>
          <label>
            Cessation date
            <input
              type="date"
              name="cessation_date"
              required
              value={officer.cessationDate}
              onChange={(event) => onPatch({ cessationDate: event.target.value })}
            />
          </label>
          <label>
            Cessation reason
            <select
              name="cessation_reason"
              value={officer.cessationReason}
              onChange={(event) =>
                onPatch({ cessationReason: event.target.value as OfficerFormState['cessationReason'] })
              }
            >
              <option value="resignation_others">
                {cessationReasonLabel('resignation_others')}
              </option>
              <option value="deceased">{cessationReasonLabel('deceased')}</option>
            </select>
          </label>
          <label>
            <input
              type="checkbox"
              name="continues_office_after_cessation"
              checked={officer.continuesOfficeAfterCessation}
              onChange={(event) => onPatch({ continuesOfficeAfterCessation: event.target.checked })}
            />
            Continues the office after cessation
          </label>
        </>
      ) : (
        <>
          <label>
            Appointment date
            <input
              type="date"
              name="appointment_date"
              required
              value={officer.appointmentDate}
              onChange={(event) => onPatch({ appointmentDate: event.target.value })}
            />
          </label>
          <label>
            <input
              type="checkbox"
              name="existing_director_at_appointment"
              checked={officer.existingDirectorAtAppointment}
              onChange={(event) =>
                onPatch({ existingDirectorAtAppointment: event.target.checked })
              }
            />
            A director at the appointment
          </label>
        </>
      )}

      <label>
        HKID (partial) — optional
        <input
          type="text"
          name="hkid_partial"
          maxLength={40}
          value={officer.hkidPartial}
          onChange={(event) => onPatch({ hkidPartial: event.target.value })}
        />
      </label>
      <label>
        Passport (partial) — optional
        <input
          type="text"
          name="passport_partial"
          maxLength={40}
          value={officer.passportPartial}
          onChange={(event) => onPatch({ passportPartial: event.target.value })}
        />
      </label>
      <label>
        Passport issuing country/region — optional
        <input
          type="text"
          name="passport_issuing_country_region"
          maxLength={40}
          value={officer.passportIssuingCountryRegion}
          onChange={(event) => onPatch({ passportIssuingCountryRegion: event.target.value })}
        />
      </label>

      <label>
        Email — optional
        <input
          type="text"
          name="email"
          maxLength={320}
          value={officer.email}
          onChange={(event) => onPatch({ email: event.target.value })}
        />
      </label>
      <label>
        BR number — optional
        <input
          type="text"
          name="br_number"
          maxLength={40}
          value={officer.brNumber}
          onChange={(event) => onPatch({ brNumber: event.target.value })}
        />
      </label>
    </>
  )
}

function AddressFields({
  form,
  onPatch,
}: {
  form: EventFormState
  onPatch: (patch: Partial<EventFormState['address']>) => void
}) {
  return (
    <>
      <label>
        Flat
        <input
          type="text"
          name="flat"
          maxLength={MAX_TEXT}
          required
          value={form.address.flat}
          onChange={(event) => onPatch({ flat: event.target.value })}
        />
      </label>
      <label>
        Building
        <input
          type="text"
          name="building"
          maxLength={MAX_TEXT}
          required
          value={form.address.building}
          onChange={(event) => onPatch({ building: event.target.value })}
        />
      </label>
      <label>
        Street
        <input
          type="text"
          name="street"
          maxLength={MAX_TEXT}
          required
          value={form.address.street}
          onChange={(event) => onPatch({ street: event.target.value })}
        />
      </label>
      <label>
        District
        <input
          type="text"
          name="district"
          maxLength={MAX_TEXT}
          required
          value={form.address.district}
          onChange={(event) => onPatch({ district: event.target.value })}
        />
      </label>
      <label>
        Country — optional
        <input
          type="text"
          name="country"
          maxLength={MAX_TEXT}
          value={form.address.country}
          onChange={(event) => onPatch({ country: event.target.value })}
        />
      </label>
      <p className="muted">
        The four CR boxes are all-or-nothing — an address the filing does not state in full is
        refused rather than padded — and the country is omitted for HK-only addresses.
      </p>
    </>
  )
}
