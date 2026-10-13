/**
 * The draft's filing-metadata edit form (contract §5.1's `edit` transition):
 * the return's financial period, the member-particulars delivery channel, the
 * presentor, the signature and the consent — the fields §2.8's document
 * carries and the API merges one level into the stored document.
 *
 * It holds the form's state and calls back; the route that owns the write is
 * the caller's, and a refusal is rendered as the backend's own code and words
 * (`RefusalNotice`) — including `filing_metadata_invalid: <field>` with every
 * field the API named, and `edit: nothing to change` for a save that would
 * leave the version's document as it stands. That last one is a live case, not
 * a hypothetical: the body is built by `draftEditRequest` (`src/drafts.ts`),
 * which diffs the form against the version's own document and omits every
 * unchanged field — so a no-op save states no fields, and the API refuses it
 * instead of moving the draft and writing an audit entry for nothing.
 *
 * **What it deliberately does not offer.** The resolved values are not here:
 * an `edit` states the filing metadata only, and a value that is wrong comes
 * from master data (or, for a holding, the member records) and is picked up by
 * regenerating — the panel above says so. The return's **made-up date** is
 * shown but not editable, and the document this form posts leaves it out: the
 * merge runs into `return` key by key, so a key the edit does not state keeps
 * what the version holds.
 */

import { useState, type FormEvent } from 'react'
import {
  DELIVERY_CHOICES,
  DELIVERY_DISABLED_REASONS,
  deliveryLabel,
  filingMetadataRows,
  type FilingMetadataFormState,
} from '../drafts'
import { formatDate } from '../format'
import { RefusalNotice } from './RefusalNotice'

export function FilingMetadataForm({
  initial,
  madeUpDate,
  showStatement,
  submitLabel,
  note,
  onSubmit,
  onCancel,
}: {
  initial: FilingMetadataFormState
  /** The return's made-up date, read-only: it is derived at generation. */
  madeUpDate: string | null
  /** NAR1 item 16's declaration; a notice's edit has no such statement. */
  showStatement: boolean
  submitLabel: string
  note?: string
  onSubmit: (form: FilingMetadataFormState) => Promise<void>
  onCancel: () => void
}) {
  const [form, setForm] = useState(initial)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<unknown>(null)

  function set<K extends keyof FilingMetadataFormState>(
    field: K,
    value: FilingMetadataFormState[K],
  ): void {
    setForm((current) => ({ ...current, [field]: value }))
  }

  async function submit(event: FormEvent<HTMLFormElement>): Promise<void> {
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

  return (
    <form className="decision-form" onSubmit={submit}>
      <h3>{submitLabel}</h3>
      {note !== undefined && <p className="muted">{note}</p>}

      <fieldset className="field-group">
        <legend>Return</legend>
        <p className="muted">
          The made-up date is derived when the draft is generated and is not editable:{' '}
          {madeUpDate === null ? 'not stated on this version.' : formatDate(madeUpDate)}.
        </p>
        <label>
          Financial period start
          <input
            type="date"
            value={form.fsPeriodStart}
            onChange={(event) => set('fsPeriodStart', event.target.value)}
          />
        </label>
        <label>
          Financial period end
          <input
            type="date"
            value={form.fsPeriodEnd}
            onChange={(event) => set('fsPeriodEnd', event.target.value)}
          />
        </label>
      </fieldset>

      <fieldset className="field-group">
        <legend>Member particulars delivery</legend>
        <label>
          Delivery channel <span className="muted">blank clears it</span>
          <select value={form.delivery} onChange={(event) => set('delivery', event.target.value)}>
            <option value="">— not stated —</option>
            {DELIVERY_CHOICES.map((choice) => {
              const reason = DELIVERY_DISABLED_REASONS[choice]
              return (
                <option key={choice} value={choice} disabled={reason !== undefined}>
                  {deliveryLabel(choice)}
                  {reason === undefined ? '' : ` — ${reason}`}
                </option>
              )
            })}
          </select>
        </label>
      </fieldset>

      <fieldset className="field-group">
        <legend>Presentor</legend>
        <label>
          Name
          <input value={form.presentorName} onChange={(event) => set('presentorName', event.target.value)} />
        </label>
        <label>
          Address
          <input
            value={form.presentorAddress}
            onChange={(event) => set('presentorAddress', event.target.value)}
          />
        </label>
        <label>
          Telephone
          <input value={form.presentorTel} onChange={(event) => set('presentorTel', event.target.value)} />
        </label>
        <label>
          Fax
          <input value={form.presentorFax} onChange={(event) => set('presentorFax', event.target.value)} />
        </label>
        <label>
          Email
          <input value={form.presentorEmail} onChange={(event) => set('presentorEmail', event.target.value)} />
        </label>
        <label>
          Reference
          <input
            value={form.presentorReference}
            onChange={(event) => set('presentorReference', event.target.value)}
          />
        </label>
      </fieldset>

      <fieldset className="field-group">
        <legend>Signature</legend>
        <label>
          Name
          <input value={form.signatureName} onChange={(event) => set('signatureName', event.target.value)} />
        </label>
        <label>
          Date
          <input
            type="date"
            value={form.signatureDate}
            onChange={(event) => set('signatureDate', event.target.value)}
          />
        </label>
        <label>
          Capacity
          <input
            value={form.signatureCapacity}
            onChange={(event) => set('signatureCapacity', event.target.value)}
          />
        </label>
      </fieldset>

      <fieldset className="field-group">
        <legend>Consent</legend>
        <label>
          Signatory name
          <input value={form.consentName} onChange={(event) => set('consentName', event.target.value)} />
        </label>
        <label>
          Signatory capacity
          <input
            value={form.consentCapacity}
            onChange={(event) => set('consentCapacity', event.target.value)}
          />
        </label>
      </fieldset>

      {showStatement && (
        <fieldset className="field-group">
          <legend>Item 16 — the private-company declaration</legend>
          <label>
            Statement
            <select
              value={form.statement}
              onChange={(event) =>
                set('statement', event.target.value as FilingMetadataFormState['statement'])
              }
            >
              <option value="">Keep what the version holds</option>
              <option value="true">Stated — the declaration stands for this draft</option>
              <option value="clear">Clear it</option>
            </select>
          </label>
        </fieldset>
      )}

      <p className="muted">
        Saving posts only what this form changes — a blank field clears the stored value, and a
        save that changes nothing states no fields at all, so the API refuses it ({' '}
        <code>edit: nothing to change</code> ) rather than silently moving the draft.
      </p>

      <span className="row-actions">
        <button type="submit" disabled={busy}>
          {busy ? 'Working…' : submitLabel}
        </button>
        <button type="button" className="secondary" disabled={busy} onClick={onCancel}>
          Cancel
        </button>
      </span>

      {error !== null && <RefusalNotice error={error} />}
    </form>
  )
}

/** The document as the metadata panel shows it, read-only. */
export function FilingMetadataSummary({ metadata }: { metadata: Record<string, unknown> }) {
  const rows = filingMetadataRows(metadata)
  if (rows.length === 0) {
    return <p className="muted">Nothing is stated on the document yet — the edit action fills it.</p>
  }
  return (
    <table className="table">
      <tbody>
        {rows.map((row) => (
          <tr key={row.path}>
            <th scope="row">{row.label}</th>
            <td>{row.value}</td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}
