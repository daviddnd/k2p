/**
 * The milestone configuration panel (scope §2.4/§2.15; contract §5.3) — the
 * Administrator's side of the one board: the dates this deployment shows a
 * client, set as counts of days **back** from each company's anniversary
 * (made-up) date.
 *
 * The screen states the two things a reader gets wrong:
 *
 *   - **every offset is counted back**, so a bigger number is an *earlier*
 *     date, and the API enforces the chain the work follows (continuation ask,
 *     data confirmation, client reply, documents out);
 *   - **CR's 42 days are emergency margin, not a deadline**. The product never
 *     prints CR's last day to a client: the offsets are the TCSP's own working
 *     deadlines, and what the API refuses is a configuration that would ask a
 *     client to reply after the filing fell due.
 *
 * The form saves the **whole** configuration, because that is what the route
 * replaces: the naming templates are read, shown as they will be written, and
 * sent back unchanged — this panel edits the dates, and the request carries
 * both blocks or the templates would be silently overwritten.
 *
 * A stored block the API cannot read is **not** a missing screen: the read
 * answers with that block's shipped defaults and says which block it could not
 * read and why (`unreadable_blocks`), and this view states it as the first
 * thing a reader sees — what the numbers below are, and that saving is the
 * repair. A silent fallback would hide a real integrity problem behind a
 * working form, so the notice is part of the panel rather than a log line.
 *
 * Validation is the API's, and its refusals are shown as its own list of
 * violations, field by field — a 422's `milestones.…` entries. The form only
 * pre-checks what it cannot send at all (a day count that is not a whole
 * number, a cadence that is not a list of them).
 */

import { useState } from 'react'
import type { NamingTemplates, TenantConfig, TenantConfigRequest } from '../api/types'
import { RefusalNotice } from '../components/RefusalNotice'
import { formatDateTime } from '../format'

interface Draft {
  continuation: string
  dataConfirmation: string
  clientReply: string
  signaturePost: string
  replyWindow: string
  cadence: string
}

type DayKey = 'continuation' | 'dataConfirmation' | 'clientReply' | 'signaturePost' | 'replyWindow'

interface DayField {
  key: DayKey
  /** The wire name, so a reader can match the form against the contract. */
  field: string
  label: string
  hint: string
}

const DAY_FIELDS: DayField[] = [
  {
    key: 'continuation',
    field: 'continuation_request_days_before',
    label: 'Continuation ask',
    hint: 'When staff ask whether the client is continuing the engagement. Nothing can send the ask yet (the sending slice does not exist), so the product holds the answer.',
  },
  {
    key: 'dataConfirmation',
    field: 'data_confirmation_days_before',
    label: 'Data confirmation',
    hint: 'When the confirmation request is generated from the master data resolved that day. This date is what the letter’s reply-by date is counted from.',
  },
  {
    key: 'clientReply',
    field: 'client_reply_days_before',
    label: 'Client reply by',
    hint: 'The date the client is asked to reply by — the date the letter prints. Bounded by the minimum reply window below.',
  },
  {
    key: 'signaturePost',
    field: 'signature_post_days_before',
    label: 'Documents to the client',
    hint: 'When the drafts go out for signature. The release gate holds this until the period is in scope and paid.',
  },
  {
    key: 'replyWindow',
    field: 'minimum_reply_window_days',
    label: 'Minimum reply window',
    hint: 'The floor under the reply-by date: however late a request is generated, the client is never asked to reply in fewer days than this. It is the second of the two numbers measured against CR’s 42 days.',
  },
]

export function MilestoneConfigView({
  config,
  onSave,
}: {
  config: TenantConfig
  onSave: (payload: TenantConfigRequest) => Promise<void>
}) {
  const [draft, setDraft] = useState<Draft>(() => draftFrom(config))
  const [busy, setBusy] = useState(false)
  const [inputError, setInputError] = useState<string | null>(null)
  const [error, setError] = useState<unknown>(null)
  const [saved, setSaved] = useState(false)

  function edit(patch: Partial<Draft>) {
    setDraft((current) => ({ ...current, ...patch }))
    setSaved(false)
  }

  function editDay(key: DayKey, value: string) {
    setDraft((current) => ({ ...current, [key]: value }))
    setSaved(false)
  }

  async function save() {
    const read = readDraft(draft, config.naming_templates)
    if ('problem' in read) {
      setInputError(read.problem)
      setError(null)
      return
    }
    setInputError(null)
    setError(null)
    setBusy(true)
    try {
      await onSave(read.payload)
      setSaved(true)
    } catch (caught) {
      setError(caught)
    } finally {
      setBusy(false)
    }
  }

  return (
    <section className="panel">
      <header className="panel-head">
        <div>
          <h2>Milestone configuration (Administrator)</h2>
          <p className="muted">
            {config.updated_at === null
              ? 'This tenant has never saved a configuration — the shipped defaults below are in force.'
              : config.unreadable_blocks.length > 0
                ? `The stored row was last written ${formatDateTime(config.updated_at)}, and a block of it cannot be read — see the notice below.`
                : `Last saved ${formatDateTime(config.updated_at)}.`}{' '}
            The dates are derived from this block, not stored per period, so a change applies at once
            to every company and every period.
          </p>
        </div>
      </header>

      {config.unreadable_blocks.length > 0 && (
        <div className="error" role="alert">
          <p>
            <strong>The stored configuration cannot be read.</strong> The values below are the
            shipped defaults, <em>not</em> what this tenant stored: the API names the block it could
            not read and every rule it failed, and it returns the defaults so this screen can still
            assemble the repair. Saving replaces the two blocks this screen carries — the naming
            templates and the milestones — in one request; the block it does not carry
            (<code>send_modes</code>) is left as stored, and if <em>that</em> is the unreadable one
            the save is refused 422 with the same violations instead of repairing it, because the
            API will not silently reset a block a request did not carry.
          </p>
          <ul className="plain-list">
            {config.unreadable_blocks.map((problem) => (
              <li key={problem.block}>
                <code>{problem.block}</code>
                <ul className="plain-list">
                  {problem.violations.map((violation) => (
                    <li key={violation}>{violation}</li>
                  ))}
                </ul>
              </li>
            ))}
          </ul>
        </div>
      )}

      <div className="notice">
        <p>
          <strong>
            Every number here is days counted <em>back</em> from each company’s anniversary (its
            made-up) date — so a bigger number is an <em>earlier</em> date.
          </strong>{' '}
          CR allows 42 days after the anniversary to file. Those 42 days are <strong>emergency
          margin, not a deadline the product prints</strong>: under the shipped defaults a client is
          asked to reply 62 days before the return falls due. The configuration is refused
          (<code>no_client_reply_window</code>) if it would ask a client to reply after the filing fell
          due; CR’s last day is never shown to a client.
        </p>
        <p>
          The order the API enforces is the order the work happens in: the continuation ask, then the
          data confirmation, then the client’s reply-by date, then the documents go out. Because every
          offset counts back, each of the first four numbers must be <em>bigger</em> than the one after
          it — a block that posted documents for signature before the client had confirmed what they
          are signing is refused, not stored.
        </p>
      </div>

      <form
        onSubmit={(event) => {
          event.preventDefault()
          void save()
        }}
      >
        <table className="table">
          <thead>
            <tr>
              <th scope="col">Client-facing date</th>
              <th scope="col">Days before the anniversary</th>
              <th scope="col">What it is</th>
            </tr>
          </thead>
          <tbody>
            {DAY_FIELDS.map((day) => (
              <tr key={day.key}>
                <td>
                  <span className="company-name">{day.label}</span>
                  <span className="brn">{day.field}</span>
                </td>
                <td>
                  <input
                    type="number"
                    min={1}
                    aria-label={day.label}
                    value={draft[day.key]}
                    onChange={(event) => editDay(day.key, event.target.value)}
                  />
                </td>
                <td className="muted">{day.hint}</td>
              </tr>
            ))}
            <tr>
              <td>
                <span className="company-name">Reminder cadence</span>
                <span className="brn">reminder_cadence_days</span>
              </td>
              <td>
                <input
                  type="text"
                  aria-label="Reminder cadence"
                  placeholder="3, 7, 10"
                  value={draft.cadence}
                  onChange={(event) => edit({ cadence: event.target.value })}
                />
              </td>
              <td className="muted">
                Days <em>after</em> a request is sent — a different computation from the offsets
                above. Comma-separated and strictly increasing (two reminders on one day are one
                reminder); empty means no reminders. The reminder sweep (contract §5.1.12) acts on
                this cadence: under the client destination&rsquo;s <code>automatic</code> mode
                reminders send themselves; under <code>review_required</code> they are drafted and
                held for a staff release. On a Free tier nothing sends automatically — the dashboard
                shows the due dates to act on by hand.
              </td>
            </tr>
            <tr>
              <td>
                <span className="company-name">Anchor</span>
                <span className="brn">anchor</span>
              </td>
              <td>
                <input type="text" aria-label="Anchor" value={config.milestones.anchor} readOnly />
              </td>
              <td className="muted">
                What every offset above counts back from: the company’s anniversary (made-up) date,
                already the basis of the NAR1 due date. The vocabulary is closed — a stored block
                naming another anchor is refused rather than arithmetic’d on.
              </td>
            </tr>
          </tbody>
        </table>

        <p className="muted">
          The client’s reply offset above is held inside CR’s 42-day window together with the minimum
          reply window: it may not exceed 42 days minus that window. The two numbers are the two
          measures taken against the one statutory window, and the bound keeps them together. It is a
          coherence rule for this block, not a promise that a late trigger can still generate: the
          generator judges that on the day it runs.
        </p>

        <h3>Document naming templates</h3>
        <p className="muted">
          The save replaces the whole configuration, so this panel sends the naming templates it read,
          unchanged. They are shown here because they travel with it — editing them is not part of this
          panel.
          {config.unreadable_blocks.some((problem) => problem.block === 'naming_templates') &&
            ' These are the shipped defaults: the stored pair could not be read, so saving this configuration writes them over it, which repairs that block too.'}
        </p>
        <ul className="plain-list">
          <li>
            <code>{config.naming_templates.document}</code>
          </li>
          <li>
            <code>{config.naming_templates.generated}</code>
          </li>
        </ul>

        <span className="row-actions">
          <button type="submit" disabled={busy}>
            {busy ? 'Saving…' : 'Save configuration'}
          </button>
        </span>
        {saved && <p className="muted">Saved. The new numbers apply to every period at once.</p>}
      </form>

      {inputError !== null && (
        <p className="error" role="alert">
          {inputError}
        </p>
      )}
      {error !== null && <RefusalNotice error={error} />}
    </section>
  )
}

function draftFrom(config: TenantConfig): Draft {
  const block = config.milestones
  return {
    continuation: String(block.continuation_request_days_before),
    dataConfirmation: String(block.data_confirmation_days_before),
    clientReply: String(block.client_reply_days_before),
    signaturePost: String(block.signature_post_days_before),
    replyWindow: String(block.minimum_reply_window_days),
    cadence: block.reminder_cadence_days.join(', '),
  }
}

/** A whole number of days, or `null` when the field cannot be read as one. */
function readDayCount(value: string): number | null {
  const trimmed = value.trim()
  if (trimmed === '' || !/^\d+$/.test(trimmed)) return null
  return Number(trimmed)
}

/** The cadence as the block carries it: whole days, strictly increasing, or `null`. */
function readCadence(value: string): number[] | null {
  const trimmed = value.trim()
  if (trimmed === '') return []
  const entries = trimmed.split(',').map((entry) => readDayCount(entry))
  if (entries.some((entry) => entry === null)) return null
  return entries.filter((entry): entry is number => entry !== null)
}

/**
 * The draft as the request body, or the field the form cannot read at all.
 * Everything past that — the ordering chain, the bounds, the offset bound — is
 * the API's validation, and its refusal comes back as the list of violations.
 */
function readDraft(
  draft: Draft,
  templates: NamingTemplates,
): { payload: TenantConfigRequest } | { problem: string } {
  const days: Record<DayKey, number> = {
    continuation: 0,
    dataConfirmation: 0,
    clientReply: 0,
    signaturePost: 0,
    replyWindow: 0,
  }
  for (const day of DAY_FIELDS) {
    const value = readDayCount(draft[day.key])
    if (value === null) return { problem: `${day.label} needs a whole number of days.` }
    days[day.key] = value
  }
  const cadence = readCadence(draft.cadence)
  if (cadence === null) {
    return { problem: 'The reminder cadence is comma-separated whole days, or empty for no reminders.' }
  }
  return {
    payload: {
      naming_templates: templates,
      milestones: {
        continuation_request_days_before: days.continuation,
        data_confirmation_days_before: days.dataConfirmation,
        client_reply_days_before: days.clientReply,
        signature_post_days_before: days.signaturePost,
        minimum_reply_window_days: days.replyWindow,
        reminder_cadence_days: cadence,
        anchor: 'anniversary',
      },
    },
  }
}
