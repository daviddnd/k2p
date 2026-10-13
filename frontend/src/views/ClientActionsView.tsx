/**
 * The client-actions panel (scope §2.4/§2.15; contract §5.1.9) — the one board:
 * what each client period needs next, and the three acts staff record about it.
 *
 * Pure presentation over `GET /api/v1/client-actions`: the route fetches, this
 * renders. Per company and anniversary year it shows every milestone the
 * tenant's configuration defines, in the order the client meets them, with the
 * date, the days remaining and the **backend's own state** — `pending | due |
 * overdue | done | blocked`, shown as what each one is and never renamed:
 * `done` is the record that completes the milestone existing (a decided period
 * for the continuation ask, a generated request for the data confirmation),
 * `blocked` is something refusing the work — and the read names the leg
 * (`blocked_reason`), so the badge's note carries the remedy rather than a
 * staff member's guess. The two honest limits are on the
 * screen rather than implied: nothing in the product records that a client
 * *replied* or that the documents were *posted*, so those two milestones read
 * by their dates; and the continuation ask itself is staff's to make, because
 * the sending slice does not exist yet.
 *
 * The acts are offered only where the API's own rules admit them — a fee can be
 * recorded only for a confirmed period, a correction only where there is a
 * decision to change — so the screen does not invite a request the route must
 * refuse. When one is refused anyway (a stale panel, another staff member's
 * action), the refusal is shown as the backend's own code and remedy, never as
 * a generic failure.
 */

import { useState } from 'react'
import type { ClientActionMilestone, ClientActionPeriod, ClientActionsResponse, RenewalDecision } from '../api/types'
import { RefusalNotice } from '../components/RefusalNotice'
import {
  formatDate,
  formatDaysRemaining,
  milestoneKindLabel,
  milestoneStateLabel,
  renewalStatusLabel,
} from '../format'
import { blockedReasonNote, correctionTargets, knownRenewalStatus } from '../renewals'
import { hrefForCompany } from '../route'

/** What the route records for a period, in the three acts the panel offers. */
export type RenewalAct =
  | { kind: 'decision'; decision: RenewalDecision; note: string | null }
  | { kind: 'payment'; note: string | null }
  | { kind: 'correction'; decision: RenewalDecision; note: string | null }

type ActKind = RenewalAct['kind']

/** The backend's own guard, pre-checked so the mistake is a form error. */
const MIN_DUE_WITHIN = 1
const MAX_DUE_WITHIN = 365

export function ClientActionsView({
  data,
  dueWithin,
  viewerRole,
  onApplyWindow,
  onRefresh,
  onAct,
}: {
  data: ClientActionsResponse
  dueWithin: number
  viewerRole?: 'administrator' | 'general'
  onApplyWindow: (days: number) => void
  onRefresh?: () => void
  onAct: (period: ClientActionPeriod, act: RenewalAct) => Promise<void>
}) {
  const [windowInput, setWindowInput] = useState(String(dueWithin))
  const [windowError, setWindowError] = useState<string | null>(null)
  const [openKey, setOpenKey] = useState<string | null>(null)
  const [actKind, setActKind] = useState<ActKind>('decision')
  const [decision, setDecision] = useState<RenewalDecision>('confirmed')
  const [note, setNote] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<{ key: string; error: unknown } | null>(null)

  function open(period: ClientActionPeriod, kind: ActKind) {
    setOpenKey(periodKey(period))
    setActKind(kind)
    setDecision(
      kind === 'correction' ? (correctionTargets(period.renewal_status)[0] ?? 'confirmed') : 'confirmed',
    )
    setNote('')
    setError(null)
  }

  function applyWindow() {
    const days = Number(windowInput)
    if (!Number.isInteger(days) || days < MIN_DUE_WITHIN || days > MAX_DUE_WITHIN) {
      setWindowError(`Enter a whole number of days between ${MIN_DUE_WITHIN} and ${MAX_DUE_WITHIN}.`)
      return
    }
    setWindowError(null)
    onApplyWindow(days)
  }

  async function submit(period: ClientActionPeriod) {
    setBusy(true)
    setError(null)
    try {
      await onAct(period, act(actKind, decision, note))
      setOpenKey(null)
    } catch (caught) {
      setError({ key: periodKey(period), error: caught })
    } finally {
      setBusy(false)
    }
  }

  return (
    <section className="panel">
      <header className="panel-head">
        <div>
          <h2>Client actions</h2>
          <p className="muted">
            Every company period and the dates this deployment shows a client, read as of{' '}
            {formatDate(data.as_of)}. A period is listed while one of its milestone dates falls between{' '}
            {formatDate(data.window_from)} and {formatDate(data.window_to)}
            {viewerRole === 'administrator'
              ? '; the milestone configuration these dates come from is on this page, below.'
              : '.'}
          </p>
        </div>
        {onRefresh !== undefined && (
          <button type="button" className="secondary" onClick={onRefresh}>
            Refresh
          </button>
        )}
      </header>

      <div className="filters">
        <label>
          Look ahead (days)
          <input
            type="number"
            min={MIN_DUE_WITHIN}
            max={MAX_DUE_WITHIN}
            value={windowInput}
            onChange={(event) => setWindowInput(event.target.value)}
          />
        </label>
        <button type="button" onClick={applyWindow}>
          Apply window
        </button>
      </div>
      {windowError !== null && (
        <p className="error" role="alert">
          {windowError}
        </p>
      )}
      <p className="muted">
        A period drops off this panel 30 days after its last date has passed — the same lookback the
        dashboard uses. The window can be 1 to 365 days ahead.
      </p>

      <p className="muted">
        States, as the API derives them: <strong>pending</strong> — the day is still to come;{' '}
        <strong>due</strong> — the day itself; <strong>overdue</strong> — the day has passed;{' '}
        <strong>done</strong> — the record that completes the milestone exists (it reads done even when
        it ran late); <strong>blocked</strong> — something refuses the work right now. Done outranks the
        date; blocked outranks it too, and the note beside the badge names the leg that refused and
        what clears it.
      </p>

      {data.periods.length === 0 ? (
        <p className="muted">
          No period has a milestone date in this window. Widen the window to look further ahead.
        </p>
      ) : (
        <div className="periods">
          {data.periods.map((period) => {
            const key = periodKey(period)
            return (
              <article className="period" key={key}>
                <header className="panel-head">
                  <div>
                    <h3>
                      <a href={hrefForCompany(period.company_id)}>
                        {period.company_name ?? `Company ${period.br_number}`}
                      </a>
                    </h3>
                    <p className="muted">
                      BRN {period.br_number} · period {period.anniversary_year} — anniversary (made-up)
                      date {formatDate(period.anniversary_date)}
                    </p>
                  </div>
                  <span className="identity-badges">
                    <span className={`badge renewal-${period.renewal_status ?? 'none'}`}>
                      {renewalStatusLabel(period.renewal_status)}
                    </span>
                  </span>
                </header>

                <table className="table">
                  <thead>
                    <tr>
                      <th scope="col">Milestone</th>
                      <th scope="col">Date</th>
                      <th scope="col">Days remaining</th>
                      <th scope="col">State</th>
                    </tr>
                  </thead>
                  <tbody>
                    {period.milestones.map((milestone) => (
                      <tr key={milestone.kind}>
                        <td>
                          <span className="company-name">{milestoneKindLabel(milestone.kind)}</span>
                          <span className="muted">{milestoneHint(milestone.kind)}</span>
                        </td>
                        <td>{formatDate(milestone.date)}</td>
                        <td className={milestone.state === 'overdue' ? 'overdue' : undefined}>
                          {formatDaysRemaining(milestone.days_remaining)}
                        </td>
                        <td>
                          <span className={`badge state-${milestone.state}`}>
                            {milestoneStateLabel(milestone.state)}
                          </span>
                          <StateNote milestone={milestone} />
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>

                <PeriodActs
                  period={period}
                  open={openKey === key}
                  actKind={actKind}
                  decision={decision}
                  note={note}
                  busy={busy}
                  error={error?.key === key ? error.error : null}
                  onOpen={open}
                  onDecision={setDecision}
                  onNote={setNote}
                  onSubmit={submit}
                  onCancel={() => {
                    setOpenKey(null)
                    setError(null)
                  }}
                />
              </article>
            )
          })}
        </div>
      )}
    </section>
  )
}

function periodKey(period: ClientActionPeriod): string {
  return `${period.company_id}:${period.anniversary_year}`
}

function act(actKind: ActKind, decision: RenewalDecision, note: string): RenewalAct {
  const written = note.trim()
  const carried = written === '' ? null : written
  if (actKind === 'payment') return { kind: 'payment', note: carried }
  return { kind: actKind, decision, note: carried }
}

/** What happens at each milestone (contract §5.3's four client-facing dates). */
function milestoneHint(kind: string): string {
  if (kind === 'continuation_request') {
    return 'Staff ask whether the client is continuing the engagement. Nothing can send that ask yet, so the product holds the answer.'
  }
  if (kind === 'data_confirmation') {
    return 'The confirmation request is generated from the master data resolved that day.'
  }
  if (kind === 'client_reply') {
    return 'The date the client is asked to reply by — the date the letter prints. A reply received on the request’s thread completes this milestone; until one arrives, it reads by its date.'
  }
  return 'The drafts go out to the client for signature. This is the release the gate holds until the period is in scope and paid.'
}

/**
 * Why a milestone reads the way it does, when the badge alone would not say.
 *
 * `blocked` names the leg that refused, and it is the **backend's** leg — the
 * read's own `blocked_reason` — never a client-side guess at what the period's
 * status implies. The two situations behind it are different kinds of thing
 * and the reason says which: the reply is blocked by a *precondition* (no
 * letter exists to reply to, `no_confirmation_request`), and the signature
 * posting by the release gate, whose leg the reason names. A blocked milestone
 * whose reason this screen does not know says exactly that rather than
 * inventing a remedy, and the posting's remaining limit is stated where it
 * applies: this read judges the legs a period can see, and the form leg is
 * judged at the download.
 */
function StateNote({ milestone }: { milestone: ClientActionMilestone }) {
  if (milestone.state === 'blocked') {
    const reason = milestone.blocked_reason
    const note = reason === null ? null : blockedReasonNote(reason)
    return (
      <span className="muted">
        {reason === null || note === null ? (
          'This milestone is blocked, and the read names no reason this screen knows. Reload, and check that this panel and the API are the same version.'
        ) : (
          <>
            Blocked — <code>{reason}</code>: {note}.
          </>
        )}{' '}
        {milestone.kind === 'signature_post' &&
          'The gate answers with the first leg that fails; this read judges the legs a period can see — the assignment, its coverage and the record — and a document whose form the package does not cover is refused at the download, where its form is known.'}
      </span>
    )
  }
  if (milestone.state === 'done' && milestone.kind === 'continuation_request') {
    return <span className="muted">An answer is recorded for this period.</span>
  }
  if (milestone.state === 'done' && milestone.kind === 'data_confirmation') {
    return <span className="muted">A confirmation request has been generated for this period.</span>
  }
  if (milestone.state === 'done' && milestone.kind === 'client_reply') {
    return (
      <span className="muted">
        A reply from the client has arrived on the request’s thread. What the reply then
        needs — a parse, staff review — is not this panel’s business.
      </span>
    )
  }
  return null
}

/**
 * The three acts, and the reason each one is missing where the API would not
 * admit it: a decision stands against a second answer, a fee follows the
 * client's confirmation and only that, and a correction needs a decision to
 * change. Nothing is offered that the route would refuse for the status shown.
 */
function PeriodActs({
  period,
  open,
  actKind,
  decision,
  note,
  busy,
  error,
  onOpen,
  onDecision,
  onNote,
  onSubmit,
  onCancel,
}: {
  period: ClientActionPeriod
  open: boolean
  actKind: ActKind
  decision: RenewalDecision
  note: string
  busy: boolean
  error: unknown
  onOpen: (period: ClientActionPeriod, kind: ActKind) => void
  onDecision: (decision: RenewalDecision) => void
  onNote: (note: string) => void
  onSubmit: (period: ClientActionPeriod) => void
  onCancel: () => void
}) {
  const status = period.renewal_status
  const targets = correctionTargets(status)
  const canDecide = status === null || status === 'pending'
  const canPay = status === 'confirmed'

  // A status this screen does not know gets no acts and no claim about the
  // record: fail visibly rather than describe a row in the wrong words.
  if (!knownRenewalStatus(status)) {
    return (
      <div className="period-acts">
        <p className="muted">
          This period reads <code>{status}</code>, which is not a status this screen knows. No act is
          offered for a record it cannot describe — reload, and check that this panel and the API are
          the same version.
        </p>
      </div>
    )
  }

  return (
    <div className="period-acts">
      <p className="muted">
        {status === null
          ? 'No record exists for this period yet — nothing has been recorded about the client’s continuation, and a period with no record holds only the release of its letter.'
          : status === 'pending'
            ? 'The record exists and the client’s answer is not on it yet. That refuses nothing but the release of the period’s letter to the client.'
            : status === 'confirmed'
              ? 'The client is continuing this period. The fee is what the release of the period’s letter waits for.'
              : status === 'declined'
                ? 'The client declined this period. That ends nothing by itself — the service assignment is untouched, and the period has no renewed coverage once it runs out.'
                : 'The fee is recorded, so the period’s letter may be released to the client.'}
      </p>

      <ul className="plain-list">
        <li>
          {canDecide ? (
            open && actKind === 'decision' ? (
              <form
                className="decision-form"
                onSubmit={(event) => {
                  event.preventDefault()
                  onSubmit(period)
                }}
              >
                <fieldset>
                  <legend>The client’s answer</legend>
                  <label>
                    <input
                      type="radio"
                      name={`decision-${periodKey(period)}`}
                      checked={decision === 'confirmed'}
                      onChange={() => onDecision('confirmed')}
                    />
                    Confirmed — the engagement continues
                  </label>
                  <label>
                    <input
                      type="radio"
                      name={`decision-${periodKey(period)}`}
                      checked={decision === 'declined'}
                      onChange={() => onDecision('declined')}
                    />
                    Declined
                  </label>
                </fieldset>
                <label>
                  Note{decision === 'declined' ? ' — required for a decline' : ' — optional'}
                  <textarea
                    rows={2}
                    maxLength={500}
                    required={decision === 'declined'}
                    value={note}
                    onChange={(event) => onNote(event.target.value)}
                  />
                </label>
                <p className="muted">
                  {decision === 'declined'
                    ? 'A decline needs a reason: the API refuses the request without one (note_required), and the reason is what a reader finds in the record and in the audit chain.'
                    : 'The answer is a record: it holds the release of the period’s letter, and refusing nothing else.'}
                  {status === null &&
                    ' This period has no record yet — recording the answer opens it (the row starts as “awaiting the client’s answer”).'}
                </p>
                <span className="row-actions">
                  <button type="submit" disabled={busy}>
                    {busy ? 'Working…' : 'Record the decision'}
                  </button>
                  <button type="button" className="secondary" disabled={busy} onClick={onCancel}>
                    Cancel
                  </button>
                </span>
              </form>
            ) : (
              <button type="button" disabled={busy} onClick={() => onOpen(period, 'decision')}>
                Record the client’s answer…
              </button>
            )
          ) : (
            <span className="muted">
              The client’s answer is recorded. A decision stands against a second answer
              (already_closed) — a wrong one is put right with Correct the decision.
            </span>
          )}
        </li>

        <li>
          {canPay ? (
            open && actKind === 'payment' ? (
              <form
                className="decision-form"
                onSubmit={(event) => {
                  event.preventDefault()
                  onSubmit(period)
                }}
              >
                <label>
                  Note — optional
                  <textarea
                    rows={2}
                    maxLength={500}
                    value={note}
                    onChange={(event) => onNote(event.target.value)}
                  />
                </label>
                <p className="muted">
                  Records that the fee arrived: a timestamp, your account and this note — and nothing
                  about money. No amount, no currency, no invoice, no receipt, no payment request. This
                  is what releases the period’s letter to the client.
                </p>
                <span className="row-actions">
                  <button type="submit" disabled={busy}>
                    {busy ? 'Working…' : 'Record the fee'}
                  </button>
                  <button type="button" className="secondary" disabled={busy} onClick={onCancel}>
                    Cancel
                  </button>
                </span>
              </form>
            ) : (
              <button type="button" disabled={busy} onClick={() => onOpen(period, 'payment')}>
                Record the fee…
              </button>
            )
          ) : (
            <span className="muted">
              {status === 'payment_received'
                ? 'The fee is already recorded for this period (a second one is already_closed).'
                : status === 'declined'
                  ? 'A declined period admits no fee — correct the decision first if it was recorded wrongly.'
                  : 'Not yet: the client’s answer comes first (the API refuses a fee for an unconfirmed period: service_not_confirmed).'}
            </span>
          )}
        </li>

        <li>
          {targets.length > 0 ? (
            open && actKind === 'correction' ? (
              <form
                className="decision-form"
                onSubmit={(event) => {
                  event.preventDefault()
                  onSubmit(period)
                }}
              >
                <label>
                  Corrected to
                  <select
                    value={decision}
                    onChange={(event) => onDecision(event.target.value as RenewalDecision)}
                  >
                    {targets.map((target) => (
                      <option key={target} value={target}>
                        {target === 'confirmed' ? 'Confirmed' : 'Declined'}
                      </option>
                    ))}
                  </select>
                </label>
                <label>
                  Reason — required
                  <textarea
                    rows={2}
                    maxLength={500}
                    required
                    value={note}
                    onChange={(event) => onNote(event.target.value)}
                  />
                </label>
                <p className="muted">
                  A correction overwrites a recorded decision — a change to a regulated client record,
                  so the reason is required and lands in the audit chain beside the answer it moved from
                  and to. Only a decision the record does not already hold can be chosen
                  (decision_unchanged is the other case).
                  {status === 'payment_received' &&
                    ' Correcting away from a paid period takes the payment back (how a bounced cheque is put right) — record the fee again when it really arrives.'}
                </p>
                <span className="row-actions">
                  <button type="submit" disabled={busy}>
                    {busy ? 'Working…' : 'Correct the decision'}
                  </button>
                  <button type="button" className="secondary" disabled={busy} onClick={onCancel}>
                    Cancel
                  </button>
                </span>
              </form>
            ) : (
              <button type="button" className="secondary" disabled={busy} onClick={() => onOpen(period, 'correction')}>
                Correct the decision…
              </button>
            )
          ) : (
            <span className="muted">
              Nothing is recorded for this period to correct yet — record the client’s answer first
              (not_decided).
            </span>
          )}
        </li>
      </ul>

      {error !== null && <RefusalNotice error={error} />}
    </div>
  )
}
