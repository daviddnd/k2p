/**
 * The change-event detail (`#/companies/{companyId}/change-events/{eventId}`):
 * the current version, its payload, the version history, the statutory window,
 * and **the actions this actor may perform now — rendered from the API's
 * transitions read and nothing else.**
 *
 * The backend is the sole authority on allowed actions: this view holds no
 * §4.1 rule of its own, maps `transitions.actions` into buttons in the read's
 * order, and lets the API's own codes (409/422) come back verbatim when a
 * stale screen submits one the event no longer admits. An action the read
 * marks rationale-required collects the reason before posting; edit collects
 * the fields a revision changes (the same typed-payload form manual entry
 * uses); delete is confirmed first, because `deleted` is an end.
 *
 * The API has no single-event read: the route finds this event in the
 * company's own list (which is the read that carries the version history), so
 * the detail's facts are the list's, re-read after every action.
 */

import { useState } from 'react'
import type {
  AllowedChangeEventAction,
  ChangeEvent,
  ChangeEventAction,
  ChangeEventTransitions,
  ChangeEventVersion,
  ExtractionAssessment,
  TransitionRequest,
} from '../api/types'
import { ChangeEventForm } from '../components/ChangeEventForm'
import { RefusalNotice } from '../components/RefusalNotice'
import { AlertBadge, ChangeEventStatusBadge, RiskBadge } from '../components/badges'
import {
  actionNeedsInput,
  changeEventActionLabel,
  changeEventActionNote,
  changeEventSourceLabel,
  changeEventTypeLabel,
  describeChangeWindow,
  editChangesFrom,
  formStateFromChangeEvent,
  payloadRows,
  type EventFormState,
} from '../change_events'
import {
  formatDate,
  formatDateTime,
  formatDaysRemaining,
  routingLabel,
  shortId,
} from '../format'
import { hrefForCompany } from '../route'

export function ChangeEventDetailView({
  companyName,
  brNumber,
  event,
  transitions,
  assessments,
  onAct,
  onRefresh,
}: {
  companyName: string | null
  brNumber: string
  event: ChangeEvent
  transitions: ChangeEventTransitions
  assessments: ExtractionAssessment[]
  onAct: (request: TransitionRequest) => Promise<void>
  onRefresh: () => void
}) {
  const [openAction, setOpenAction] = useState<ChangeEventAction | null>(null)
  const [rationale, setRationale] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<unknown>(null)
  const company = companyName ?? `Company ${brNumber}`

  async function perform(request: TransitionRequest): Promise<void> {
    setBusy(true)
    setError(null)
    try {
      await onAct(request)
      setOpenAction(null)
    } catch (caught) {
      setError(caught)
    } finally {
      setBusy(false)
    }
  }

  /** The edit path: the form owns the refusal it meets, so it is not caught here. */
  async function performEdit(form: EventFormState): Promise<void> {
    setBusy(true)
    setError(null)
    try {
      await onAct({ action: 'edit', ...editChangesFrom(event, form) })
      setOpenAction(null)
    } finally {
      setBusy(false)
    }
  }

  function press(allowed: AllowedChangeEventAction) {
    if (actionNeedsInput(allowed.action, allowed.rationale_required)) {
      setOpenAction(allowed.action)
      setRationale('')
      setError(null)
      return
    }
    void perform({ action: allowed.action })
  }

  const versions: Array<{ version: ChangeEventVersion; current: boolean }> = [
    { version: event, current: true },
    ...event.history.map((version) => ({ version, current: false })),
  ]
  const payloadLeaves = payloadRows(event.payload)

  return (
    <div className="view">
      <p className="muted">
        <a href="#/change-events">← Change events awaiting review</a> ·{' '}
        <a href={hrefForCompany(event.company_id)}>← {company}</a>
      </p>

      <section className="panel">
        <header className="panel-head">
          <div>
            <h1>
              {changeEventTypeLabel(event.type)} <ChangeEventStatusBadge status={event.status} />
            </h1>
            <p className="muted">
              <a href={hrefForCompany(event.company_id)}>{company}</a> · BRN {brNumber} · version{' '}
              {event.version_no} · recorded {formatDateTime(event.created_at)}
              {event.created_by !== null && <> by staff {shortId(event.created_by)}</>}
            </p>
          </div>
          <button type="button" className="secondary" onClick={onRefresh}>
            Refresh
          </button>
        </header>
        <p className="muted">
          The event&rsquo;s id stays the same across versions: an edit appends a new version and the
          earlier ones stay on the record. Nothing here is submitted to any registry — the draft
          that carries a confirmed event is later work.
        </p>
        <table className="table">
          <tbody>
            <tr>
              <th scope="row">Effective date</th>
              <td>
                {formatDate(event.effective_date)}
                {event.effective_date_text !== null && (
                  <span className="brn">as stated: “{event.effective_date_text}”</span>
                )}
              </td>
            </tr>
            <tr>
              <th scope="row">Informed date</th>
              <td>
                {event.informed_date === null
                  ? 'Not recorded'
                  : formatDate(event.informed_date)}
              </td>
            </tr>
            <tr>
              <th scope="row">Source</th>
              <td>{changeEventSourceLabel(event.source_type)}</td>
            </tr>
            <tr>
              <th scope="row">Reviewed</th>
              <td>
                {event.reviewed_by !== null && event.reviewed_at !== null
                  ? `Reviewed by staff ${shortId(event.reviewed_by)} on ${formatDateTime(event.reviewed_at)}`
                  : 'Not reviewed yet'}
              </td>
            </tr>
            <tr>
              <th scope="row">Approved</th>
              <td>
                {event.approved_by !== null && event.approved_at !== null
                  ? `Approved by staff ${shortId(event.approved_by)} on ${formatDateTime(event.approved_at)}`
                  : 'Not approved yet'}
              </td>
            </tr>
            {event.rationale !== null && (
              <tr>
                <th scope="row">Rationale</th>
                <td>{event.rationale}</td>
              </tr>
            )}
          </tbody>
        </table>
      </section>

      <section className="panel">
        <h2>The statutory 15-day window</h2>
        <p className="muted">
          {describeChangeWindow(event.change_window)} It is counted from the change&rsquo;s effective
          date — never from the day the TCSP learned of it — and the risk colour and alert band are
          the compliance calendar&rsquo;s own 30/15/7 thresholds.
        </p>
        <ul className="plain-list">
          <li>
            Deadline <strong>{formatDate(event.change_window.deadline)}</strong>
          </li>
          <li>
            Days remaining{' '}
            <span className={event.change_window.is_late ? 'overdue' : undefined}>
              {formatDaysRemaining(event.change_window.days_remaining)}
            </span>
            {event.change_window.is_late && <span className="muted"> — late</span>}
          </li>
          <li>
            <span className="identity-badges">
              <RiskBadge risk={event.change_window.risk} />
              <AlertBadge alert={event.change_window.alert} />
            </span>
          </li>
        </ul>
      </section>

      <section className="panel">
        <h2>What this event says</h2>
        <p className="muted">
          The payload as the API stores it, in the registry&rsquo;s own vocabulary — this is what a
          draft will read. No §1 protected-class field can be on it: the API refuses{' '}
          <code>hkid_full</code>, <code>hkid_check_digit</code>, <code>passport_full</code> and{' '}
          <code>usual_residential_address</code> by name; the partial identifiers shown here are the
          public-page values.
        </p>
        {payloadLeaves.length === 0 ? (
          <p className="muted">The payload is empty.</p>
        ) : (
          <table className="table">
            <thead>
              <tr>
                <th scope="col">Field</th>
                <th scope="col">Registry path</th>
                <th scope="col">Value</th>
              </tr>
            </thead>
            <tbody>
              {payloadLeaves.map((row) => (                <tr key={row.path}>
                  <td>{row.label}</td>
                  <td>
                    <code>{row.path}</code>
                  </td>
                  <td>{row.value}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>

      <section className="panel">
        <h2>What you may do now</h2>
        <p className="muted">
          The API&rsquo;s own list for this account and this event&rsquo;s status — rendered here,
          never decided here: the backend is the sole authority on allowed actions. The list is a
          read taken when this page loaded; an action that is not here is not offered — it may need
          a permission this account does not hold, or a step the event&rsquo;s status does not
          admit. After every action the page re-reads the event.
        </p>

        {transitions.actions.length === 0 ? (
          <p className="muted">
            No action is available to this account on this event now. A deleted or withdrawn event
            is an end, and a system row (apply, submit, record the client&rsquo;s reply) is
            nobody&rsquo;s to press — the API never offers either.
          </p>
        ) : (
          <ul className="plain-list">
            {transitions.actions.map((allowed) => (
              <li key={allowed.action}>
                {openAction === allowed.action && allowed.action === 'edit' ? (
                  <ChangeEventForm
                    legend="Edit this event"
                    submitLabel="Save the new version"
                    initial={formStateFromChangeEvent(event)}
                    note="Saving appends a new version — the current one becomes history; the linkage and the provenance stay. A save with nothing changed sends nothing, and the API answers its own refusal."
                    onSubmit={performEdit}
                    onCancel={() => setOpenAction(null)}
                  />
                ) : openAction === allowed.action && allowed.action === 'delete' ? (
                  <>
                    <p className="muted">
                      Deleting sets the event to <code>deleted</code> — nothing is removed, and it
                      stays visible on the company&rsquo;s list, marked. No action leads out of
                      deleted, so a mistake is corrected by recording what stands.
                    </p>
                    <span className="row-actions">
                      <button
                        type="button"
                        disabled={busy}
                        onClick={() => void perform({ action: 'delete' })}
                      >
                        {busy ? 'Working…' : 'Yes, delete the event'}
                      </button>
                      <button
                        type="button"
                        className="secondary"
                        disabled={busy}
                        onClick={() => setOpenAction(null)}
                      >
                        Cancel
                      </button>
                    </span>
                  </>
                ) : openAction === allowed.action && allowed.rationale_required ? (
                  <form
                    className="decision-form"
                    onSubmit={(submitEvent) => {
                      submitEvent.preventDefault()
                      void perform({ action: allowed.action, rationale: rationale.trim() })
                    }}
                  >
                    <label>
                      Reason — required
                      <textarea
                        rows={2}
                        maxLength={500}
                        required
                        value={rationale}
                        onChange={(changeEvent) => setRationale(changeEvent.target.value)}
                      />
                    </label>
                    <p className="muted">
                      {allowed.action === 'client_withdraw'
                        ? 'Record the withdrawal as the client gave it — by phone or in person; staff act for the client here. The reason is required (rationale_required) and lands on the event and in the audit chain.'
                        : 'A rejection needs a reason — the API refuses it without one (rationale_required), and what you write lands on the event and in the audit chain.'}
                    </p>
                    <span className="row-actions">
                      <button type="submit" disabled={busy}>
                        {busy
                          ? 'Working…'
                          : `${changeEventActionLabel(allowed.action)}${
                              allowed.action === 'client_withdraw' ? '' : ' the event'
                            }`}
                      </button>
                      <button
                        type="button"
                        className="secondary"
                        disabled={busy}
                        onClick={() => setOpenAction(null)}
                      >
                        Cancel
                      </button>
                    </span>
                  </form>
                ) : (
                  <>
                    <button type="button" disabled={busy} onClick={() => press(allowed)}>
                      {changeEventActionLabel(allowed.action)}
                      {actionNeedsInput(allowed.action, allowed.rationale_required) ? '…' : ''}
                    </button>{' '}
                    <span className="muted">
                      {changeEventActionNote(allowed.action) ??
                        'the API lists this action for this account now'}
                      {allowed.rationale_required && ' A reason will be required.'}
                    </span>
                  </>
                )}
              </li>
            ))}
          </ul>
        )}

        {error !== null && <RefusalNotice error={error} />}
      </section>

      <section className="panel">
        <h2>Version history</h2>
        {event.history.length === 0 ? (
          <p className="muted">
            This is the event&rsquo;s first version — nothing earlier is on the record.
          </p>
        ) : null}
        <table className="table">
          <thead>
            <tr>
              <th scope="col">Version</th>
              <th scope="col">Status</th>
              <th scope="col">Effective date</th>
              <th scope="col">Recorded</th>
              <th scope="col">Rationale</th>
            </tr>
          </thead>
          <tbody>
            {versions.map(({ version, current }) => (
              <tr key={version.version_no}>
                <td>
                  v{version.version_no}
                  {current && <span className="muted"> (current)</span>}
                </td>
                <td>
                  <ChangeEventStatusBadge status={version.status} />
                </td>
                <td>
                  {formatDate(version.effective_date)}
                  {version.effective_date_text !== null && (
                    <span className="brn">as stated: “{version.effective_date_text}”</span>
                  )}
                </td>
                <td>
                  {formatDateTime(version.created_at)}
                  {version.created_by !== null && (
                    <span className="brn">by staff {shortId(version.created_by)}</span>
                  )}
                </td>
                <td>{version.rationale ?? '—'}</td>
              </tr>
            ))}
          </tbody>
        </table>
        <p className="muted">
          Newest first. A version is never edited in place: every revision appends a row, and a
          status move updates the current one.
        </p>
      </section>

      <section className="panel">
        <h2>Extraction assessments</h2>
        {assessments.length === 0 ? (
          <p className="muted">
            No extraction assessments — this event was entered by hand or through the API, and the
            reply parser that would produce one is not built yet. Nothing is estimated here.
          </p>
        ) : (
          <table className="table">
            <thead>
              <tr>
                <th scope="col">Component</th>
                <th scope="col">Model</th>
                <th scope="col">Overall score</th>
                <th scope="col">Routing</th>
                <th scope="col">Read</th>
              </tr>
            </thead>
            <tbody>
              {assessments.map((assessment) => (
                <tr key={assessment.id}>
                  <td>{assessment.component}</td>
                  <td>{assessment.model}</td>
                  <td>{assessment.overall_score === null ? '—' : assessment.overall_score.toFixed(2)}</td>
                  <td>{routingLabel(assessment.routing)}</td>
                  <td>{formatDateTime(assessment.created_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>
    </div>
  )
}
