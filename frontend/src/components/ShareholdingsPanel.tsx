/**
 * The company screen's member panel (scope §2.1; contract §5.1's
 * Shareholdings row): the company's member positions newest `valid_from`
 * first, an add form that creates the person inline, an edit form over the
 * holding's own fields and a confirmed removal.
 *
 * Money and the percentage cross as **strings** in both directions (the house
 * `Numeric(19,2)` convention), which is why the form holds them as text and
 * why a value the member boxes cannot carry comes back as the API's own
 * `shareholding_invalid: <field>`. As in the officer panel, a person's
 * identity is corrected on the Administrator-only persons surface, and the
 * API's refusal is shown verbatim.
 */

import { useState } from 'react'
import type { Shareholding, ShareholdingUpdate } from '../api/types'
import {
  addressLine,
  blankPerson,
  blankShareholding,
  inOffice,
  personName,
  personTypeLabel,
  shareholdingChangesFrom,
  shareholdingFormFrom,
  type PersonFormState,
  type ShareholdingFormState,
} from '../officers'
import { formatDate, isoDate } from '../format'
import { AddressFields, PersonFields } from './PersonFields'
import { RefusalNotice } from './RefusalNotice'

export function ShareholdingsPanel({
  shareholdings,
  onCreate,
  onUpdate,
  onDelete,
}: {
  shareholdings: Shareholding[]
  onCreate: (person: PersonFormState, holding: ShareholdingFormState) => Promise<void>
  onUpdate: (shareholdingId: string, changes: ShareholdingUpdate) => Promise<void>
  onDelete: (shareholdingId: string) => Promise<void>
}) {
  const today = isoDate(new Date())
  const [adding, setAdding] = useState(false)
  const [person, setPerson] = useState<PersonFormState>(blankPerson)
  const [holding, setHolding] = useState<ShareholdingFormState>(blankShareholding)
  const [editState, setEditState] = useState<ShareholdingFormState>(blankShareholding)
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

  const edited = shareholdings.find((candidate) => candidate.id === editing) ?? null
  const removed = shareholdings.find((candidate) => candidate.id === removing) ?? null

  return (
    <section className="panel" id="members">
      <header className="panel-head">
        <div>
          <h2>Members (shareholdings)</h2>
          <p className="muted">
            The member positions as filed, newest first — the draft&rsquo;s member boxes. Money and
            the percentage are decimals carried as text, exactly as the API takes them. A holding
            that ends is a window correction (<code>valid_to</code>); removing a row is for one
            entered by mistake. Person identities are corrected on the Administrator-only persons
            surface.
          </p>
        </div>
        {!adding && (
          <button
            type="button"
            onClick={() => {
              setPerson(blankPerson())
              setHolding(blankShareholding())
              setError(null)
              setAdding(true)
            }}
          >
            Record a member…
          </button>
        )}
      </header>

      {adding && (
        <form
          className="record-form"
          onSubmit={(event) => {
            event.preventDefault()
            void run(() => onCreate(person, holding))
          }}
        >
          <PersonFields legend="The person holding the shares" value={person} onChange={setPerson} />
          <HoldingFields value={holding} onChange={setHolding} />
          <p className="muted">
            A new person is created through the contacts write path and the holding is recorded on
            the same transaction: if the API refuses, nothing is written.
          </p>
          <span className="row-actions">
            <button type="submit" disabled={busy}>
              {busy ? 'Working…' : 'Record the member'}
            </button>
            <button type="button" className="secondary" disabled={busy} onClick={() => setAdding(false)}>
              Cancel
            </button>
          </span>
        </form>
      )}

      {shareholdings.length === 0 ? (
        <p className="muted">
          No member position is recorded for this company yet. Record the holdings as filed above —
          the draft&rsquo;s member boxes come from these records.
        </p>
      ) : (
        <table className="table">
          <thead>
            <tr>
              <th scope="col">Member</th>
              <th scope="col">Class and shares</th>
              <th scope="col">Amounts</th>
              <th scope="col">Window</th>
              <th scope="col"></th>
            </tr>
          </thead>
          <tbody>
            {shareholdings.map((record) => (
              <tr key={record.id}>
                <td>
                  <span className="company-name">{personName(record.person)}</span>
                  <span className="brn">
                    {personTypeLabel(record.person.person_type)}
                    {record.jointly_held === null ? '' : ` · jointly held: ${record.jointly_held}`}
                  </span>
                  {record.address !== null && <span className="brn">{addressLine(record.address)}</span>}
                  {record.remarks !== null && <span className="brn">{record.remarks}</span>}
                </td>
                <td>
                  <span className="company-name">
                    {record.shares_held.toLocaleString('en-GB')} × {record.class_name}
                  </span>
                  <span className="brn">
                    {record.percentage_held === null
                      ? 'percentage not stated'
                      : `${record.percentage_held}%`}
                    {record.allotment_date === null
                      ? ''
                      : ` · allotted ${formatDate(record.allotment_date)}`}
                  </span>
                </td>
                <td>
                  <span className="brn">
                    paid {record.amount_paid ?? '—'} · unpaid {record.amount_unpaid ?? '—'}
                  </span>
                </td>
                <td>
                  <span>
                    from {formatDate(record.valid_from)}
                    {record.valid_to === null ? ' — open-ended' : ` to ${formatDate(record.valid_to)}`}
                  </span>
                  <span className="identity-badges">
                    {inOffice(record.valid_from, record.valid_to, today) ? (
                      <span className="badge status-active">Held</span>
                    ) : (
                      <span className="badge status-unrecorded">
                        {record.valid_from > today ? 'Not yet held' : 'Ended'}
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
                        setEditState(shareholdingFormFrom(record))
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
            void run(() => onUpdate(edited.id, shareholdingChangesFrom(editState)))
          }}
        >
          <h3>Edit {personName(edited.person)}&rsquo;s holding</h3>
          <p className="muted">
            The holding&rsquo;s own fields only — the person&rsquo;s identity is corrected on the
            Administrator-only persons surface. A blank optional field is sent as an explicit null,
            which clears it.
          </p>
          <HoldingFields value={editState} onChange={setEditState} />
          <span className="row-actions">
            <button type="submit" disabled={busy}>
              {busy ? 'Working…' : 'Save the holding'}
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
            Remove {personName(removed.person)}&rsquo;s {removed.class_name} holding? The row and
            its window go; the audit chain keeps what it said. A holding that ended is not this —
            set the window&rsquo;s end instead.
          </p>
          <span className="row-actions">
            <button
              type="button"
              className="danger"
              disabled={busy}
              onClick={() => void run(() => onDelete(removed.id))}
            >
              {busy ? 'Working…' : 'Yes, remove the holding'}
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

function HoldingFields({
  value,
  onChange,
}: {
  value: ShareholdingFormState
  onChange: (next: ShareholdingFormState) => void
}) {
  function set<K extends keyof ShareholdingFormState>(
    field: K,
    fieldValue: ShareholdingFormState[K],
  ): void {
    onChange({ ...value, [field]: fieldValue })
  }
  return (
    <fieldset className="field-group">
      <legend>The holding</legend>
      <label>
        Class of shares
        <input
          required
          value={value.class_name}
          onChange={(event) => set('class_name', event.target.value)}
        />
      </label>
      <label>
        Shares held
        <input
          type="number"
          min="0"
          step="1"
          required
          value={value.shares_held}
          onChange={(event) => set('shares_held', event.target.value)}
        />
      </label>
      <label>
        Jointly held with <span className="muted">optional</span>
        <input value={value.jointly_held} onChange={(event) => set('jointly_held', event.target.value)} />
      </label>
      <label>
        Percentage held <span className="muted">optional, e.g. 25.00</span>
        <input
          value={value.percentage_held}
          onChange={(event) => set('percentage_held', event.target.value)}
        />
      </label>
      <label>
        Amount paid
        <input value={value.amount_paid} onChange={(event) => set('amount_paid', event.target.value)} />
      </label>
      <label>
        Amount unpaid
        <input
          value={value.amount_unpaid}
          onChange={(event) => set('amount_unpaid', event.target.value)}
        />
      </label>
      <label>
        Allotment date
        <input
          type="date"
          value={value.allotment_date}
          onChange={(event) => set('allotment_date', event.target.value)}
        />
      </label>
      <label>
        Held from
        <input
          type="date"
          required
          value={value.validFrom}
          onChange={(event) => set('validFrom', event.target.value)}
        />
      </label>
      <label>
        Held to <span className="muted">optional — the day the holding ended</span>
        <input
          type="date"
          value={value.validTo}
          onChange={(event) => set('validTo', event.target.value)}
        />
      </label>
      <label>
        Remarks
        <textarea
          rows={2}
          value={value.remarks}
          onChange={(event) => set('remarks', event.target.value)}
        />
      </label>
      <AddressFields
        legend="Address as filed"
        value={value.address}
        onChange={(next) => set('address', next)}
        note="The member's address as it is filed — the four CR boxes, plus a country only outside Hong Kong."
      />
    </fieldset>
  )
}
