/**
 * The company screen's drafts section (scope §2.9; contract §5.1's Drafts
 * row): each lineage's current version with a link to its detail, and the
 * generation form (`form_type` + `target_date`) that posts the gated
 * automation.
 *
 * Generation is the one gated action of the draft lifecycle (DoD §9 item 27):
 * when the §5.1 gate denies it, the API answers `403 automation_denied:
 * <reason>` and writes nothing — the refusal is rendered here in place, with
 * the gate's own reason worded by `describeRefusal`, rather than letting the
 * form fail silently. The other draft actions live on the detail screen, from
 * that draft's own transitions read.
 */

import { useState } from 'react'
import type { Draft, FormCode } from '../api/types'
import { formatDate, formatDateTime, formLabel, shortId } from '../format'
import { hrefForDraft } from '../route'
import { DraftStatusBadge } from './badges'
import { RefusalNotice } from './RefusalNotice'

export interface GenerateDraftForm {
  formType: FormCode
  targetDate: string
}

export function CompanyDrafts({
  companyId,
  drafts,
  onGenerate,
}: {
  companyId: string
  drafts: Draft[]
  onGenerate: (form: GenerateDraftForm) => Promise<void>
}) {
  const [formOpen, setFormOpen] = useState(false)
  const [formType, setFormType] = useState<FormCode>('nar1')
  const [targetDate, setTargetDate] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<unknown>(null)

  async function generate(): Promise<void> {
    setBusy(true)
    setError(null)
    try {
      await onGenerate({ formType, targetDate })
      setFormOpen(false)
      setTargetDate('')
    } catch (caught) {
      setError(caught)
    } finally {
      setBusy(false)
    }
  }

  return (
    <section className="panel" id="drafts">
      <header className="panel-head">
        <div>
          <h2>Drafts</h2>
          <p className="muted">
            Each lineage&rsquo;s current version, newest first — the forms this company&rsquo;s
            master data and approved changes resolve into. Generating is a gated automation (the
            API consults the §5.1 gate and writes nothing when it denies); everything after — the
            resolved values, the change highlighting, the filing metadata and the actions — lives
            on the draft&rsquo;s own screen.
          </p>
        </div>
        {!formOpen && (
          <button type="button" onClick={() => setFormOpen(true)}>
            Generate a draft…
          </button>
        )}
      </header>

      {formOpen && (
        <form
          className="record-form"
          onSubmit={(event) => {
            event.preventDefault()
            void generate()
          }}
        >
          <fieldset className="field-group">
            <legend>Generate a draft</legend>
            <label>
              Form
              <select
                value={formType}
                onChange={(event) => setFormType(event.target.value as FormCode)}
              >
                <option value="nar1">NAR1 — annual return</option>
                <option value="nd2a">ND2A — change of secretary/director</option>
              </select>
            </label>
            <label>
              Target date — the draft&rsquo;s as-of date
              <input
                type="date"
                required
                value={targetDate}
                onChange={(event) => setTargetDate(event.target.value)}
              />
            </label>
            <p className="muted">
              Every effective date resolves from this date: the return&rsquo;s made-up date anchors
              an NAR1, and a change applies only when its effective date has arrived by then. The
              form version the draft is built against is pinned on the draft.
            </p>
          </fieldset>
          <span className="row-actions">
            <button type="submit" disabled={busy}>
              {busy ? 'Working…' : 'Generate the draft'}
            </button>
            <button type="button" className="secondary" disabled={busy} onClick={() => setFormOpen(false)}>
              Cancel
            </button>
          </span>
          {error !== null && <RefusalNotice error={error} />}
        </form>
      )}

      {drafts.length === 0 ? (
        <p className="muted">
          No draft exists for this company yet. Generate one above once the company&rsquo;s master
          data is in — the draft resolves what the records say on the target date, and every value
          it places is traceable to its source.
        </p>
      ) : (
        <table className="table">
          <thead>
            <tr>
              <th scope="col">Form</th>
              <th scope="col">Version</th>
              <th scope="col">Status</th>
              <th scope="col">Target date</th>
              <th scope="col">Generated</th>
              <th scope="col"></th>
            </tr>
          </thead>
          <tbody>
            {drafts.map((draft) => (
              <tr key={draft.id}>
                <td>
                  <span className="company-name">{formLabel(draft.form_type)}</span>
                  <span className="brn">specification {draft.form_version.specification_no}</span>
                </td>
                <td>
                  v{draft.version_no}
                  {draft.history.length > 0 && (
                    <span className="brn">
                      {draft.history.length} earlier version{draft.history.length === 1 ? '' : 's'}
                    </span>
                  )}
                </td>
                <td>
                  <DraftStatusBadge status={draft.status} />
                </td>
                <td>{formatDate(draft.target_date)}</td>
                <td>
                  {formatDateTime(draft.created_at)}
                  {draft.created_by !== null && <span className="brn">by staff {shortId(draft.created_by)}</span>}
                </td>
                <td>
                  <a href={hrefForDraft(companyId, draft.id)}>Open the draft</a>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  )
}
