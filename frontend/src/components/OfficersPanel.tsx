/**
 * The company screen's officer panel (scope §2.1; contract §5.1's Officers
 * row): the company's appointments newest `valid_from` first, an add form
 * that creates the person inline, an edit form over the appointment's own
 * fields and a confirmed removal.
 *
 * The panel is presentational plus form state: the route owns the reads and
 * the writes, and this component renders what it is given, calls back, and
 * shows the API's own refusal — verbatim — when a write is refused. Two
 * boundaries are stated on the screen rather than hidden: a person's identity
 * is corrected on the Administrator-only persons surface (the API's
 * `PATCH /officers/{id}` takes the appointment's fields only), and a cessation
 * is a window correction — `valid_to` = the first day out of office — while
 * removing a row is for one entered by mistake.
 */

import { useState } from 'react'
import type { OfficerAppointment, OfficerAppointmentUpdate } from '../api/types'
import {
  addressLine,
  appointmentChangesFrom,
  appointmentFormFrom,
  appointmentWithRole,
  blankAppointment,
  blankPerson,
  capacityChoices,
  capacityLabel,
  inOffice,
  OFFICER_ROLES,
  officerRoleLabel,
  personName,
  personTypeLabel,
  type AppointmentFormState,
  type PersonFormState,
} from '../officers'
import { formatDate, isoDate } from '../format'
import { AddressFields, PersonFields } from './PersonFields'
import { RefusalNotice } from './RefusalNotice'

export function OfficersPanel({
  officers,
  onCreate,
  onUpdate,
  onDelete,
}: {
  officers: OfficerAppointment[]
  onCreate: (person: PersonFormState, appointment: AppointmentFormState) => Promise<void>
  onUpdate: (appointmentId: string, changes: OfficerAppointmentUpdate) => Promise<void>
  onDelete: (appointmentId: string) => Promise<void>
}) {
  const today = isoDate(new Date())
  const [adding, setAdding] = useState(false)
  const [person, setPerson] = useState<PersonFormState>(blankPerson)
  const [appointment, setAppointment] = useState<AppointmentFormState>(blankAppointment)
  const [editState, setEditState] = useState<AppointmentFormState>(blankAppointment)
  const [editing, setEditing] = useState<string | null>(null)
  const [removing, setRemoving] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<unknown>(null)

  async function run(action: () => Promise<void>): Promise<void> {
    setBusy(true)
    setError(null)
    try {
      await action()
      setAdding(false)
      setEditing(null)
      setRemoving(null)
    } catch (caught) {
      setError(caught)
    } finally {
      setBusy(false)
    }
  }

  const edited = officers.find((candidate) => candidate.id === editing) ?? null
  const removed = officers.find((candidate) => candidate.id === removing) ?? null

  return (
    <section className="panel" id="officers">
      <header className="panel-head">
        <div>
          <h2>Officers</h2>
          <p className="muted">
            This company&rsquo;s appointments as filed, newest first — what the draft resolves its
            officer boxes from. A cessation is a window correction: set <code>valid_to</code> to the
            first day out of office. Removing a row is for one entered by mistake. A person&rsquo;s
            identity is corrected on the Administrator-only persons surface, not here; the API takes
            the appointment&rsquo;s fields only.
          </p>
        </div>
        {!adding && (
          <button
            type="button"
            onClick={() => {
              setPerson(blankPerson())
              setAppointment(blankAppointment())
              setError(null)
              setAdding(true)
            }}
          >
            Record an appointment…
          </button>
        )}
      </header>

      {adding && (
        <form
          className="record-form"
          onSubmit={(event) => {
            event.preventDefault()
            void run(() => onCreate(person, appointment))
          }}
        >
          <PersonFields legend="The person this appointment names" value={person} onChange={setPerson} />
          <AppointmentFields value={appointment} onChange={setAppointment} personType={person.personType} />
          <p className="muted">
            A new person is created through the contacts write path and the office is recorded on
            the same transaction: if the API refuses, nothing is written — no appointment, and no
            person either.
          </p>
          <span className="row-actions">
            <button type="submit" disabled={busy}>
              {busy ? 'Working…' : 'Record the appointment'}
            </button>
            <button type="button" className="secondary" disabled={busy} onClick={() => setAdding(false)}>
              Cancel
            </button>
          </span>
        </form>
      )}

      {officers.length === 0 ? (
        <p className="muted">
          No officer is recorded for this company yet. Record the appointments as filed above — the
          draft resolves its officer boxes from these records, so an empty panel is an empty form.
        </p>
      ) : (
        <table className="table">
          <thead>
            <tr>
              <th scope="col">Officer</th>
              <th scope="col">Office</th>
              <th scope="col">Address as filed</th>
              <th scope="col">Service window</th>
              <th scope="col"></th>
            </tr>
          </thead>
          <tbody>
            {officers.map((record) => (
              <tr key={record.id}>
                <td>
                  <span className="company-name">{personName(record.person)}</span>
                  <span className="brn">
                    {personTypeLabel(record.person.person_type)}
                    {record.person.email === null ? '' : ` · ${record.person.email}`}
                    {record.person.br_number === null ? '' : ` · BR ${record.person.br_number}`}
                  </span>
                </td>
                <td>
                  <span className="company-name">{officerRoleLabel(record.role)}</span>
                  <span className="brn">
                    {record.capacity.length === 0
                      ? 'no capacity boxes ticked'
                      : record.capacity.map((box) => capacityLabel(box)).join(', ')}
                  </span>
                  {record.alternate_to !== null && (
                    <span className="brn">alternate to {record.alternate_to}</span>
                  )}
                </td>
                <td>
                  {record.correspondence_address !== null && (
                    <span className="brn">
                      Correspondence: {addressLine(record.correspondence_address)}
                    </span>
                  )}
                  {record.registered_office_address !== null && (
                    <span className="brn">
                      Registered office: {addressLine(record.registered_office_address)}
                    </span>
                  )}
                </td>
                <td>
                  <span>
                    from {formatDate(record.valid_from)}
                    {record.valid_to === null ? ' — open-ended' : ` to ${formatDate(record.valid_to)}`}
                  </span>
                  <span className="identity-badges">
                    {inOffice(record.valid_from, record.valid_to, today) ? (
                      <span className="badge status-active">In office</span>
                    ) : (
                      <span className="badge status-unrecorded">
                        {record.valid_from > today ? 'Not yet in office' : 'Out of office'}
                      </span>
                    )}
                  </span>
                </td>
                <td>
                  <span className="row-actions">
                    <button
                      type="button"
                      className="secondary"
                      onClick={() => {
                        setError(null)
                        setRemoving(null)
                        setEditState(appointmentFormFrom(record))
                        setEditing(editing === record.id ? null : record.id)
                      }}
                    >
                      {editing === record.id ? 'Close' : 'Edit…'}
                    </button>
                    <button
                      type="button"
                      className="secondary"
                      onClick={() => {
                        setError(null)
                        setEditing(null)
                        setRemoving(removing === record.id ? null : record.id)
                      }}
                    >
                      {removing === record.id ? 'Cancel' : 'Remove…'}
                    </button>
                  </span>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {edited !== null && (
        <form
          className="record-form"
          onSubmit={(event) => {
            event.preventDefault()
            void run(() => onUpdate(edited.id, appointmentChangesFrom(editState)))
          }}
        >
          <h3>
            Edit {personName(edited.person)} — {officerRoleLabel(edited.role)}
          </h3>
          <p className="muted">
            The appointment&rsquo;s own fields only. The person&rsquo;s identity — names, the
            identification values, the TCSP licence — is corrected on the Administrator-only persons
            surface.
          </p>
          <AppointmentFields
            value={editState}
            onChange={setEditState}
            personType={edited.person.person_type}
          />
          <span className="row-actions">
            <button type="submit" disabled={busy}>
              {busy ? 'Working…' : 'Save the appointment'}
            </button>
            <button type="button" className="secondary" disabled={busy} onClick={() => setEditing(null)}>
              Cancel
            </button>
          </span>
        </form>
      )}

      {removed !== null && (
        <div className="notice">
          <p>
            Remove the {officerRoleLabel(removed.role)} appointment of {personName(removed.person)}?
            The row and its window go; the audit chain keeps what it said. A cessation is not this —
            set the window&rsquo;s end instead.
          </p>
          <span className="row-actions">
            <button
              type="button"
              className="danger"
              disabled={busy}
              onClick={() => void run(() => onDelete(removed.id))}
            >
              {busy ? 'Working…' : 'Yes, remove the appointment'}
            </button>
            <button type="button" className="secondary" disabled={busy} onClick={() => setRemoving(null)}>
              Cancel
            </button>
          </span>
        </div>
      )}

      {error !== null && <RefusalNotice error={error} />}
    </section>
  )
}

function AppointmentFields({
  value,
  onChange,
  personType,
}: {
  value: AppointmentFormState
  onChange: (next: AppointmentFormState) => void
  personType: string
}) {
  function set<K extends keyof AppointmentFormState>(
    field: K,
    fieldValue: AppointmentFormState[K],
  ): void {
    onChange({ ...value, [field]: fieldValue })
  }
  return (
    <fieldset className="field-group">
      <legend>The office</legend>
      <label>
        Role
        <select
          value={value.role}
          onChange={(event) =>
            onChange(appointmentWithRole(value, event.target.value as AppointmentFormState['role']))
          }
        >
          {OFFICER_ROLES.map((role) => (
            <option key={role} value={role}>
              {officerRoleLabel(role)}
            </option>
          ))}
        </select>
      </label>
      <fieldset className="field-group">
        <legend>Capacity boxes (the registry&rsquo;s own)</legend>
        {capacityChoices(value.role).map((box) => (
          <label key={box} className="checkbox">
            <input
              type="checkbox"
              checked={value.capacities.includes(box)}
              onChange={(event) =>
                set(
                  'capacities',
                  event.target.checked
                    ? [...value.capacities, box]
                    : value.capacities.filter((candidate) => candidate !== box),
                )
              }
            />
            {capacityLabel(box)}
          </label>
        ))}
      </fieldset>
      {value.capacities.includes('alternate_director') && (
        <label>
          Alternate to — required with the alternate box
          <input
            required
            value={value.alternateTo}
            onChange={(event) => set('alternateTo', event.target.value)}
          />
        </label>
      )}
      <label>
        In office from
        <input
          type="date"
          required
          value={value.validFrom}
          onChange={(event) => set('validFrom', event.target.value)}
        />
      </label>
      <label>
        Out of office from{' '}
        <span className="muted">optional — the cessation&rsquo;s effective date</span>
        <input
          type="date"
          value={value.validTo}
          onChange={(event) => set('validTo', event.target.value)}
        />
      </label>
      {personType === 'body_corporate' ? (
        <AddressFields
          legend="Registered office address as filed"
          value={value.registeredOffice}
          onChange={(next) => set('registeredOffice', next)}
          note="A body corporate files its registered office address; a correspondence address is not applicable to it."
        />
      ) : (
        <AddressFields
          legend="Correspondence address as filed"
          value={value.correspondence}
          onChange={(next) => set('correspondence', next)}
          note="A natural person files a correspondence address; a registered office address is not applicable to them."
        />
      )}
    </fieldset>
  )
}
