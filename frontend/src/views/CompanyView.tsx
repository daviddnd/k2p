/**
 * The company view (scope §2.1/§2.3/§2.9): one company's master data as of
 * today, its change events, its prior-filing extraction runs, the review queue
 * for the run the reviewer picks, its officer and member records, and its
 * drafts — with the audit compliance report (the §5.1 Audit row) on its own
 * tab.
 *
 * Pure presentation over the reads the route owns — the master-data snapshot,
 * the change-event list, the run list, the selected run's records, the
 * appointments, the shareholdings, the draft list and, once the tab is
 * opened, the audit report. The review table itself
 * is `components/BatchReview` (where confirm/reject and their evidence live),
 * the change-event entry form is `components/ChangeEventForm` (the typed
 * payload grammar), the officer and member panels are
 * `components/OfficersPanel` / `components/ShareholdingsPanel` (the inline
 * person, the appointment/holding fields and their confirmed removals), and
 * the drafts section is `components/CompanyDrafts` (the list and the gated
 * generation form); each calls back to the route that owns the write.
 *
 * The change-events panel is the company's own list: every event with its
 * current version and the earlier ones, its 15-day statutory window, and a
 * link to the detail where the version history, the payload and the actor's
 * allowed actions live. Recording one by hand is scope §2.7's fallback input
 * — the form is opened here, and the event lands in the review queue.
 *
 * The states are deliberate, not fallbacks: no run has been read yet (with the
 * API call that starts one, since triggering extraction is not on this
 * screen), every run that exists — including one that proposed nothing — a
 * company with no change event yet, no officer or member record yet, and no
 * draft yet.
 */

import { useState } from 'react'
import {
  actorLabel,
  entitySummary,
  groupEntriesByArea,
  type AuditReport,
} from '../auditReport'
import type {
  ChangeEvent,
  CompanyMasterData,
  Draft,
  ImportBatch,
  ImportBatchSummary,
  OfficerAppointment,
  OfficerAppointmentUpdate,
  Shareholding,
  ShareholdingUpdate,
} from '../api/types'
import { BatchReview, type BatchDecision } from '../components/BatchReview'
import { ChangeEventForm } from '../components/ChangeEventForm'
import { CompanyDrafts, type GenerateDraftForm } from '../components/CompanyDrafts'
import { ErrorPanel } from '../components/ErrorPanel'
import { OfficersPanel } from '../components/OfficersPanel'
import { ShareholdingsPanel } from '../components/ShareholdingsPanel'
import { ChangeEventStatusBadge, CompanyStatusBadge, RiskBadge, AlertBadge } from '../components/badges'
import { blankEventForm, changeEventTypeLabel, type EventFormState } from '../change_events'
import type { AppointmentFormState, PersonFormState, ShareholdingFormState } from '../officers'
import {
  companyTypeLabel,
  formatDate,
  formatDateTime,
  formatDaysRemaining,
  formLabel,
  shapeLabel,
} from '../format'
import { hrefForChangeEvent } from '../route'

export function CompanyView({
  master,
  batches,
  batch,
  selectedBatchId,
  events,
  officers,
  shareholdings,
  drafts,
  onSelectBatch,
  onDecide,
  onCreate,
  onCreateOfficer,
  onUpdateOfficer,
  onDeleteOfficer,
  onCreateShareholding,
  onUpdateShareholding,
  onDeleteShareholding,
  onGenerateDraft,
  onRefresh,
  auditReport,
  auditError,
  onOpenAudit,
  onRetryAudit,
  onDownloadAudit,
}: {
  master: CompanyMasterData
  batches: ImportBatchSummary[]
  batch: ImportBatch | null
  selectedBatchId: string | null
  events: ChangeEvent[]
  officers: OfficerAppointment[]
  shareholdings: Shareholding[]
  drafts: Draft[]
  onSelectBatch: (batchId: string) => void
  onDecide: (recordId: string, decision: BatchDecision) => Promise<void>
  onCreate: (form: EventFormState) => Promise<void>
  onCreateOfficer: (person: PersonFormState, appointment: AppointmentFormState) => Promise<void>
  onUpdateOfficer: (appointmentId: string, changes: OfficerAppointmentUpdate) => Promise<void>
  onDeleteOfficer: (appointmentId: string) => Promise<void>
  onCreateShareholding: (person: PersonFormState, holding: ShareholdingFormState) => Promise<void>
  onUpdateShareholding: (shareholdingId: string, changes: ShareholdingUpdate) => Promise<void>
  onDeleteShareholding: (shareholdingId: string) => Promise<void>
  onGenerateDraft: (form: GenerateDraftForm) => Promise<void>
  onRefresh: () => void
  /** The §5.1 audit compliance report — null until the Audit tab is opened. */
  auditReport: AuditReport | null
  /** The report read's failure, surfaced inside the Audit tab. */
  auditError: unknown | null
  onOpenAudit: () => void
  onRetryAudit: () => void
  onDownloadAudit: () => void
}) {
  const { company } = master
  const [tab, setTab] = useState<'overview' | 'audit'>('overview')
  const name =
    master.name === null
      ? null
      : (master.name.name_en ?? master.name.name_zh ?? master.name.business_name)
  const pending = batches.reduce((total, item) => total + item.records_pending, 0)

  return (
    <div className="view">
      <p className="muted">
        <a href="#/dashboard">← Portfolio dashboard</a>
      </p>

      <section className="panel">
        <header className="panel-head">
          <div>
            <h1>{name ?? 'No name on record'}</h1>
            <p className="muted">
              BRN {company.br_number}
              {company.cr_number === null
                ? ' · no CR number recorded'
                : ` · CR ${company.cr_number}`}{' '}
              · {companyTypeLabel(company.type)} · incorporated{' '}
              {formatDate(company.date_of_incorporation)} · anniversary{' '}
              {formatDate(company.anniversary_date)}
            </p>
          </div>
          <span className="identity-badges">
            <CompanyStatusBadge status={master.status?.status ?? null} />
            <button type="button" className="secondary" onClick={onRefresh}>
              Refresh
            </button>
          </span>
        </header>
        <p className="muted">
          Master data as of {formatDate(master.as_of)}:
          {master.name === null
            ? ' no registered name is in effect.'
            : ` name from a record effective ${formatDate(master.name.effective_date)}.`}
        </p>
      </section>

      <div className="tabs" role="tablist">
        <button
          type="button"
          role="tab"
          aria-selected={tab === 'overview'}
          className={tab === 'overview' ? 'tab active' : 'tab'}
          onClick={() => setTab('overview')}
        >
          Overview
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={tab === 'audit'}
          className={tab === 'audit' ? 'tab active' : 'tab'}
          onClick={() => {
            setTab('audit')
            onOpenAudit()
          }}
        >
          Audit
        </button>
      </div>

      {tab === 'audit' ? (
        <CompanyAuditPanel
          report={auditReport}
          error={auditError}
          onRetry={onRetryAudit}
          onDownload={onDownloadAudit}
        />
      ) : (
        <>
          <OfficersPanel
        officers={officers}
        onCreate={onCreateOfficer}
        onUpdate={onUpdateOfficer}
        onDelete={onDeleteOfficer}
      />

      <ShareholdingsPanel
        shareholdings={shareholdings}
        onCreate={onCreateShareholding}
        onUpdate={onUpdateShareholding}
        onDelete={onDeleteShareholding}
      />

      <CompanyDrafts companyId={company.id} drafts={drafts} onGenerate={onGenerateDraft} />

      <ChangeEventsSection events={events} onCreate={onCreate} />

      <section className="panel">
        <h2>Ingestion review</h2>
        <p className="muted">
          Master-data records extracted from this company&rsquo;s prior filings, awaiting a
          decision. Confirming writes the value into master data; rejecting writes nothing.
          Extraction itself is gated automation and is triggered through the API
          (<code>POST /api/v1/companies/{'{id}'}/import-batches</code>) — this screen decides what a
          run proposed.
        </p>

        {batches.length === 0 ? (
          <p className="notice">
            No prior filing has been read for this company yet, so there is nothing to review. A run
            is created by uploading a prior filing and triggering an extraction — that step is an
            API call today; the proposals it produces appear here.
          </p>
        ) : (
          <>
            <p className="muted">
              {batches.length} run{batches.length === 1 ? '' : 's'}
              {pending > 0 && ` — ${pending} record${pending === 1 ? '' : 's'} awaiting a decision`}
              .
            </p>
            <ul className="batch-list">
              {batches.map((item) => (
                <li key={item.id}>
                  <button
                    type="button"
                    className={item.id === selectedBatchId ? 'batch active' : 'batch'}
                    onClick={() => onSelectBatch(item.id)}
                  >
                    <span className="company-name">{formLabel(item.form)}</span>
                    <span className="brn">{shapeLabel(item.shape)}</span>
                    <span className="brn">read {formatDateTime(item.created_at)}</span>
                    <span className={item.records_pending > 0 ? 'pending' : 'muted'}>
                      {item.records_total} record{item.records_total === 1 ? '' : 's'} —{' '}
                      {item.records_pending} pending, {item.records_confirmed} confirmed,{' '}
                      {item.records_rejected} rejected
                    </span>
                  </button>
                </li>
              ))}
            </ul>
          </>
        )}
      </section>

      {batches.length > 0 && (
        <section className="panel">
          <h2>Proposals in this run</h2>
          {batch === null ? (
            <p className="muted">Loading the run…</p>
          ) : (
            <BatchReview batch={batch} onDecide={onDecide} />
          )}
        </section>
      )}
        </>
      )}
    </div>
  )
}

/**
 * The company's change events, and the manual-entry form (scope §2.7's
 * fallback input). The form opens on demand and closes on a successful write;
 * the list below it is the route's own re-read, so a new event appears without
 * a reload gesture.
 */
function ChangeEventsSection({
  events,
  onCreate,
}: {
  events: ChangeEvent[]
  onCreate: (form: EventFormState) => Promise<void>
}) {
  const [formOpen, setFormOpen] = useState(false)
  return (
    <section className="panel">
      <header className="panel-head">
        <div>
          <h2>Change events</h2>
          <p className="muted">
            Changes recorded for this company, newest first (the API&rsquo;s order) — each with its
            current version, the earlier ones, and its 15-day statutory window counted from the
            effective date. Open an event for its payload, its lineage and the actions the API
            allows on it now.
          </p>
        </div>
        {!formOpen && (
          <button type="button" onClick={() => setFormOpen(true)}>
            Record a change event…
          </button>
        )}
      </header>

      {formOpen && (
        <ChangeEventForm
          legend="Record a change event"
          submitLabel="Record the change"
          initial={blankEventForm()}
          note="The event enters pending at version 1 with source type manual, and lands in the review queue (pending or under review)."
          onSubmit={async (form) => {
            await onCreate(form)
            setFormOpen(false)
          }}
          onCancel={() => setFormOpen(false)}
        />
      )}

      {events.length === 0 ? (
        <p className="muted">
          No change event has been recorded for this company yet. Record one above when the client
          reports a change — a director or secretary appointment or cessation, a registered-office
          move, or anything else staff need to file.
        </p>
      ) : (
        <table className="table">
          <thead>
            <tr>
              <th scope="col">Change</th>
              <th scope="col">Status</th>
              <th scope="col">Effective date</th>
              <th scope="col">Window</th>
              <th scope="col"></th>
            </tr>
          </thead>
          <tbody>
            {events.map((event) => (
              <tr key={event.id}>
                <td>
                  <span className="company-name">{changeEventTypeLabel(event.type)}</span>
                  <span className="brn">
                    version {event.version_no}
                    {event.history.length === 0
                      ? ' — no earlier version'
                      : ` — earlier: ${event.history
                          .map(
                            (version) =>
                              `v${version.version_no} · ${formatDateTime(version.created_at)}`,
                          )
                          .join(', ')}`}
                  </span>
                </td>
                <td>
                  <ChangeEventStatusBadge status={event.status} />
                </td>
                <td>
                  {formatDate(event.effective_date)}
                  {event.effective_date_text !== null && (
                    <span className="brn">as stated: “{event.effective_date_text}”</span>
                  )}
                </td>
                <td>
                  <span className={event.change_window.is_late ? 'overdue' : undefined}>
                    {formatDaysRemaining(event.change_window.days_remaining)}
                  </span>
                  <span className="brn">deadline {formatDate(event.change_window.deadline)}</span>
                  <span className="identity-badges">
                    <RiskBadge risk={event.change_window.risk} />
                    <AlertBadge alert={event.change_window.alert} />
                  </span>
                </td>
                <td>
                  <a href={hrefForChangeEvent(event.company_id, event.id)}>Open event</a>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  )
}

/**
 * The Audit tab: the company's audit compliance report (the §5.1 Audit row).
 * The chain verdict heads the tab — verified, with the population it covered,
 * or the problems the verification found — with ruling R6's scope note in
 * view so the company filter is never mistaken for an independently
 * verifiable subset. Below it the entries sit under the five report areas
 * (the `auditReport` mapper owns the grouping, the order and the counts), and
 * the Download JSON button saves the API response as a blob. The report
 * arrives only when the tab is opened; until then this is the deliberate
 * loading state, and a failed read is the failure panel with its retry.
 */
export function CompanyAuditPanel({
  report,
  error,
  onRetry,
  onDownload,
}: {
  report: AuditReport | null
  error: unknown | null
  onRetry: () => void
  onDownload: () => void
}) {
  if (error !== null) {
    return (
      <section className="panel">
        <ErrorPanel error={error} onRetry={onRetry} />
      </section>
    )
  }
  if (report === null) {
    return <p className="panel muted">Loading the audit report…</p>
  }
  const groups = groupEntriesByArea(report.entries)
  return (
    <section className="panel">
      <header className="panel-head">
        <div>
          <h2>Audit</h2>
          <p className="muted">
            This company&rsquo;s audit trail as a compliance report — every entry it contributed to
            the tenant&rsquo;s hash-chained audit log, grouped under the five report areas. The full
            detail rides in the JSON download.
          </p>
        </div>
        <button type="button" className="secondary" onClick={onDownload}>
          Download JSON
        </button>
      </header>

      <p className={report.chain.verified ? 'chain-verdict' : 'chain-verdict overdue'}>
        {report.chain.verified
          ? `Chain verified ✓ — ${report.chain.checked_entries} entries checked`
          : 'Chain verification found problems'}
      </p>
      {!report.chain.verified && report.chain.problems.length > 0 && (
        <ul className="plain-list">
          {report.chain.problems.map((problem) => (
            <li key={problem}>{problem}</li>
          ))}
        </ul>
      )}
      <p className="muted">
        Scope: {report.chain.scope}. Generated {formatDateTime(report.generated_at)} · report
        version {report.report_version}.
      </p>

      {groups.map((group) => (
        <section key={group.area} className="audit-area">
          <h3>
            {group.label} <span className="muted">— {group.entries.length} entries</span>
          </h3>
          {group.entries.length === 0 ? (
            <p className="muted">No entries in this area.</p>
          ) : (
            <table className="table">
              <thead>
                <tr>
                  <th scope="col">Occurred</th>
                  <th scope="col">Actor</th>
                  <th scope="col">Action</th>
                  <th scope="col">Entity</th>
                </tr>
              </thead>
              <tbody>
                {group.entries.map((entry) => (
                  <tr key={entry.seq}>
                    <td>{formatDateTime(entry.occurred_at)}</td>
                    <td>{actorLabel(entry)}</td>
                    <td>
                      <code>{entry.action}</code>
                    </td>
                    <td>{entitySummary(entry)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </section>
      ))}
    </section>
  )
}
