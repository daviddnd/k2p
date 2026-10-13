/**
 * One extraction run as the reviewer decides it: the proposals, the evidence
 * behind each one, and the confirm/reject actions.
 *
 * What the reviewer is shown, and why:
 *
 *   - **the proposal itself** — the master-data field it targets and the value
 *     the filing stated;
 *   - **the confidence** the extractor scored, plus the run's §2.7 routing and
 *     overall score (the minimum field score, so one doubtful box routes the
 *     run) — the numbers the API returns, not a re-derived band;
 *   - **the provenance** — how the value was read and any signal that capped
 *     it (`assessment.signal_log.fields[target]`), plus the model that read the
 *     document, so "0.8" is never a number without a reason;
 *   - **the effective date**, which master data is dated by: the filing's own
 *     made-up date when it stated one, otherwise the reviewer's, and the API
 *     refuses (422) rather than writing undated data — the input is always
 *     there, so supplying one needs no second attempt at a guess.
 *
 * Confirming writes master data; rejecting writes nothing. Both statements are
 * on screen, and both are true of the backend: a rejection leaves the
 * proposal in the run as the record of what the filing said.
 */

import { useState } from 'react'
import { ApiError, errorText } from '../api/client'
import type { ImportBatch, ImportBatchRecord } from '../api/types'
import {
  formatDate,
  formatDateTime,
  formLabel,
  isStaticTarget,
  recordStatusLabel,
  routingLabel,
  shapeLabel,
  shortId,
  signalLabel,
  targetLabel,
} from '../format'

export type BatchDecision =
  | { kind: 'confirm'; effectiveDate: string | null }
  | { kind: 'reject' }

export function BatchReview({
  batch,
  onDecide,
}: {
  batch: ImportBatch
  onDecide: (recordId: string, decision: BatchDecision) => Promise<void>
}) {
  const [openRecord, setOpenRecord] = useState<string | null>(null)
  const [decision, setDecision] = useState<'confirm' | 'reject'>('confirm')
  const [effectiveDate, setEffectiveDate] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<{ recordId: string; message: string } | null>(null)

  function open(record: ImportBatchRecord, kind: 'confirm' | 'reject') {
    setOpenRecord(record.id)
    setDecision(kind)
    setEffectiveDate(record.effective_date ?? '')
    setError(null)
  }

  async function submit(record: ImportBatchRecord) {
    setBusy(true)
    setError(null)
    try {
      await onDecide(
        record.id,
        decision === 'confirm'
          ? {
              kind: 'confirm',
              // Statics are corrected in place: sending a date would suggest
              // one applies.
              effectiveDate: isStaticTarget(record.target)
                ? null
                : effectiveDate === ''
                  ? null
                  : effectiveDate,
            }
          : { kind: 'reject' },
      )
      setOpenRecord(null)
    } catch (caught) {
      setError({ recordId: record.id, message: decisionErrorText(caught) })
    } finally {
      setBusy(false)
    }
  }

  const assessment = batch.assessment
  return (
    <>
      <div className="batch-summary">
        <p>
          <strong>{formLabel(batch.form)}</strong> — {shapeLabel(batch.shape)} — read{' '}
          {formatDateTime(batch.created_at)}
          {batch.created_by !== null && ` by staff ${shortId(batch.created_by)}`}
        </p>
        {assessment === null ? (
          <p className="muted">No assessment was recorded for this run.</p>
        ) : (
          <p className="muted">
            Routing: <strong>{routingLabel(assessment.routing)}</strong>
            {Object.keys(assessment.field_scores).length === 0
              ? ' — no field produced a value, so the run has no score'
              : ` — overall score ${assessment.overall_score ?? 'n/a'} (the weakest of ${
                  Object.keys(assessment.field_scores).length
                } scored fields)`}{' '}
            — reader model <code>{assessment.model}</code>
          </p>
        )}
        <p className="muted">
          Confirming a proposal writes it into the company&rsquo;s master data as of its effective
          date. Rejecting writes nothing to master data — the proposal stays here as the record of
          what the filing said and what staff decided.
        </p>
        {batch.records.length > 0 && batch.records.every((record) => record.status !== 'pending') && (
          <p className="muted">
            Every proposal in this run has been decided: the confirmed ones are in master data, the
            rejected ones were left out, and nothing here is still outstanding.
          </p>
        )}
      </div>

      {batch.records.length === 0 ? (
        <EmptyRun batch={batch} />
      ) : (
        <table className="table review-table">
          <thead>
            <tr>
              <th scope="col">Field</th>
              <th scope="col">Proposed value</th>
              <th scope="col">Confidence</th>
              <th scope="col">Provenance</th>
              <th scope="col">Effective date</th>
              <th scope="col">Decision</th>
            </tr>
          </thead>
          <tbody>
            {batch.records.map((record) => (
              <RecordRow
                key={record.id}
                record={record}
                signals={assessment?.signal_log.fields?.[record.target] ?? []}
                open={openRecord === record.id}
                decision={decision}
                effectiveDate={effectiveDate}
                busy={busy}
                error={error?.recordId === record.id ? error.message : null}
                onOpen={open}
                onEffectiveDate={setEffectiveDate}
                onSubmit={submit}
                onCancel={() => {
                  setOpenRecord(null)
                  setError(null)
                }}
              />
            ))}
          </tbody>
        </table>
      )}
    </>
  )
}

function RecordRow({
  record,
  signals,
  open,
  decision,
  effectiveDate,
  busy,
  error,
  onOpen,
  onEffectiveDate,
  onSubmit,
  onCancel,
}: {
  record: ImportBatchRecord
  signals: string[]
  open: boolean
  decision: 'confirm' | 'reject'
  effectiveDate: string
  busy: boolean
  error: string | null
  onOpen: (record: ImportBatchRecord, kind: 'confirm' | 'reject') => void
  onEffectiveDate: (value: string) => void
  onSubmit: (record: ImportBatchRecord) => void
  onCancel: () => void
}) {
  const pending = record.status === 'pending'
  return (
    <tr className={pending ? undefined : 'decided'}>
      <td>
        <span className="company-name">{targetLabel(record.target)}</span>
        <span className="brn">{record.target}</span>
      </td>
      <td className="value-cell">{record.value}</td>
      <td>{record.confidence}</td>
      <td className="muted">
        {signals.length === 0 ? (
          <span>—</span>
        ) : (
          <ul className="plain-list">
            {signals.map((signal) => (
              <li key={signal}>{signalLabel(signal)}</li>
            ))}
          </ul>
        )}
      </td>
      <td>
        {isStaticTarget(record.target) ? (
          <span className="muted">Not applicable — corrected in place</span>
        ) : record.effective_date === null ? (
          <span className="pending">Not stated by the filing</span>
        ) : (
          formatDate(record.effective_date)
        )}
      </td>
      <td>
        <span className={`badge record-${record.status}`}>{recordStatusLabel(record.status)}</span>
        {record.decided_by !== null && (
          <span className="brn">
            staff {shortId(record.decided_by)}
            {record.decided_at !== null && ` · ${formatDateTime(record.decided_at)}`}
          </span>
        )}
        {pending && !open && (
          <span className="row-actions">
            <button type="button" disabled={busy} onClick={() => onOpen(record, 'confirm')}>
              Confirm…
            </button>
            <button
              type="button"
              className="secondary"
              disabled={busy}
              onClick={() => onOpen(record, 'reject')}
            >
              Reject…
            </button>
          </span>
        )}
        {open && (
          <form
            className="decision-form"
            onSubmit={(event) => {
              event.preventDefault()
              onSubmit(record)
            }}
          >
            {isStaticTarget(record.target) ? (
              <p className="muted">
                {decision === 'reject'
                  ? 'Rejecting writes nothing to master data.'
                  : 'This is an anchor static: confirming corrects it in place (an Administrator-only action) and no effective date applies.'}
              </p>
            ) : (
              <>
                <label>
                  Effective date
                  <input
                    type="date"
                    value={effectiveDate}
                    onChange={(event) => onEffectiveDate(event.target.value)}
                    disabled={decision === 'reject'}
                  />
                </label>
                <p className="muted">
                  {decision === 'reject'
                    ? 'Rejecting writes nothing to master data.'
                    : record.effective_date === null
                      ? 'The filing states no effective date, so the API requires one from you before it writes anything. Master data will carry this date.'
                      : 'The filing\u2019s own made-up date is proposed; a date you supply here wins and becomes the date the record carries.'}
                </p>
              </>
            )}
            <span className="row-actions">
              <button type="submit" disabled={busy}>
                {busy
                  ? 'Working…'
                  : decision === 'confirm'
                    ? 'Confirm and write to master data'
                    : 'Reject this proposal'}
              </button>
              <button type="button" className="secondary" onClick={onCancel} disabled={busy}>
                Cancel
              </button>
            </span>
            {error !== null && (
              <p className="error" role="alert">
                {error}
              </p>
            )}
          </form>
        )}
      </td>
    </tr>
  )
}

function EmptyRun({ batch }: { batch: ImportBatch }) {
  const signals = batch.assessment?.signal_log.document ?? []
  return (
    <div className="notice">
      <p>
        {batch.shape === 'needs_ocr'
          ? 'This run proposed nothing: the document is a scan with no text layer, and the local OCR path did not propose values from it.'
          : 'This run proposed nothing: the reader found no field on the page it could map to master data.'}
      </p>
      {signals.length > 0 && (
        <ul className="plain-list">
          {signals.map((signal) => (
            <li key={signal}>— {signalLabel(signal)}</li>
          ))}
        </ul>
      )}
      <p>
        The run is kept — it records what was read and why nothing was proposed. Nothing on this
        screen failed; no empty list is hidden here.
      </p>
    </div>
  )
}

/**
 * A decision failure reads as the backend's own reason: 403 (an anchor static
 * is Administrator-only), 422 (no effective date, or an incomplete fact group)
 * and 409 (already decided) all carry an actionable detail on this screen.
 */
function decisionErrorText(error: unknown): string {
  if (error instanceof ApiError && error.detail !== '') {
    if (error.status === 403) return `Not permitted: ${error.detail}`
    if (error.status === 422 || error.status === 409) return error.detail
  }
  return errorText(error)
}
