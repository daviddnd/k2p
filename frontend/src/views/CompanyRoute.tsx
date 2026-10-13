/**
 * The company route (`#/companies/{id}`): owns the reads behind the review
 * screen — the master-data snapshot as of today, the company's change events,
 * the extraction runs, the selected run's records, the officer and member
 * records, and the company's drafts — plus the decisions, the manual
 * change-event entry, the officer/member maintenance and the draft generation.
 * The audit compliance report (the §5.1 Audit row) is read only once the
 * Audit tab is opened, and the JSON download is the route's blob save of that
 * response.
 *
 * The selected run defaults to the newest one in the list, so opening a
 * company with pending work shows the work; picking another run re-reads only
 * that run. A decision re-reads the run and the list, because both carry the
 * counts the decision changes; recording a change event re-reads the event
 * list, because the write's echo is not the list's order; an officer or
 * member write re-reads its own panel's list (the draft lists are left alone —
 * a draft resolves master data when it is generated, not when a record
 * moves).
 */

import { useState } from 'react'
import { reportFileName } from '../auditReport'
import {
  confirmImportRecord,
  createChangeEvent,
  createOfficer,
  createShareholding,
  deleteOfficer,
  deleteShareholding,
  fetchCompanyAuditReport,
  fetchCompanyChangeEvents,
  fetchCompanyDrafts,
  fetchCompanyMasterData,
  fetchImportBatch,
  fetchImportBatches,
  fetchOfficers,
  fetchShareholdings,
  generateDraft,
  rejectImportRecord,
  updateOfficer,
  updateShareholding,
} from '../api/endpoints'
import type { OfficerAppointmentUpdate, ShareholdingUpdate } from '../api/types'
import { ErrorPanel } from '../components/ErrorPanel'
import type { BatchDecision } from '../components/BatchReview'
import type { GenerateDraftForm } from '../components/CompanyDrafts'
import { createRequestFrom, type EventFormState } from '../change_events'
import { isoDate } from '../format'
import {
  appointmentCreateFrom,
  shareholdingCreateFrom,
  type AppointmentFormState,
  type PersonFormState,
  type ShareholdingFormState,
} from '../officers'
import { useMfaRefusalGuard } from '../mfa'
import { useResource } from '../useResource'
import { CompanyView } from './CompanyView'

export function CompanyRoute({ companyId }: { companyId: string }) {
  const asOf = isoDate(new Date())
  const [master, reloadMaster] = useResource(
    () => fetchCompanyMasterData(companyId, asOf),
    `company-master:${companyId}:${asOf}`,
  )
  const [events, reloadEvents] = useResource(
    () => fetchCompanyChangeEvents(companyId),
    `company-change-events:${companyId}`,
  )
  const [batches, reloadBatches] = useResource(
    () => fetchImportBatches(companyId),
    `company-batches:${companyId}`,
  )
  const [officers, reloadOfficers] = useResource(
    () => fetchOfficers(companyId),
    `company-officers:${companyId}`,
  )
  const [shareholdings, reloadShareholdings] = useResource(
    () => fetchShareholdings(companyId),
    `company-shareholdings:${companyId}`,
  )
  const [drafts, reloadDrafts] = useResource(
    () => fetchCompanyDrafts(companyId),
    `company-drafts:${companyId}`,
  )
  const [picked, setPicked] = useState<string | null>(null)
  const selectedBatchId = picked ?? batches.data?.[0]?.id ?? null
  const [batch, reloadBatch] = useResource(
    () =>
      selectedBatchId === null
        ? Promise.resolve(null)
        : fetchImportBatch(companyId, selectedBatchId),
    selectedBatchId === null ? 'no-batch' : `batch:${selectedBatchId}`,
  )
  // The report is read when the Audit tab is opened, not on the screen's
  // first render — a company visit that never opens the tab never calls it.
  const [auditRequested, setAuditRequested] = useState(false)
  const [audit, reloadAudit] = useResource(
    () => (auditRequested ? fetchCompanyAuditReport(companyId) : Promise.resolve(null)),
    auditRequested ? `company-audit-report:${companyId}` : 'no-audit-report',
  )
  // A refusal on any read may be the MFA gate's 403: let the guard settle
  // whether the session owes the challenge before the error panel stands.
  const firstRefusal =
    [master, events, batches, officers, shareholdings, drafts, batch, audit].find(
      (read) => read.status === 'error',
    )?.error ?? null
  useMfaRefusalGuard(firstRefusal)

  if (master.status === 'error') {
    return (
      <div className="view">
        <BackLink />
        <ErrorPanel error={master.error} onRetry={reloadMaster} />
      </div>
    )
  }
  if (events.status === 'error') {
    return (
      <div className="view">
        <BackLink />
        <ErrorPanel error={events.error} onRetry={reloadEvents} />
      </div>
    )
  }
  if (batches.status === 'error') {
    return (
      <div className="view">
        <BackLink />
        <ErrorPanel error={batches.error} onRetry={reloadBatches} />
      </div>
    )
  }
  if (officers.status === 'error') {
    return (
      <div className="view">
        <BackLink />
        <ErrorPanel error={officers.error} onRetry={reloadOfficers} />
      </div>
    )
  }
  if (shareholdings.status === 'error') {
    return (
      <div className="view">
        <BackLink />
        <ErrorPanel error={shareholdings.error} onRetry={reloadShareholdings} />
      </div>
    )
  }
  if (drafts.status === 'error') {
    return (
      <div className="view">
        <BackLink />
        <ErrorPanel error={drafts.error} onRetry={reloadDrafts} />
      </div>
    )
  }
  if (master.data === null) return <p className="panel muted">Loading the company…</p>
  if (events.data === null) return <p className="panel muted">Loading the change events…</p>
  if (batches.data === null) return <p className="panel muted">Loading the review queue…</p>
  if (officers.data === null) return <p className="panel muted">Loading the officers…</p>
  if (shareholdings.data === null) return <p className="panel muted">Loading the members…</p>
  if (drafts.data === null) return <p className="panel muted">Loading the drafts…</p>

  async function decide(recordId: string, decision: BatchDecision): Promise<void> {
    if (selectedBatchId === null) return
    if (decision.kind === 'confirm') {
      await confirmImportRecord(companyId, selectedBatchId, recordId, decision.effectiveDate)
    } else {
      await rejectImportRecord(companyId, selectedBatchId, recordId)
    }
    reloadBatch()
    reloadBatches()
  }

  async function create(form: EventFormState): Promise<void> {
    await createChangeEvent(companyId, createRequestFrom(form))
    reloadEvents()
  }

  async function addOfficer(
    person: PersonFormState,
    appointment: AppointmentFormState,
  ): Promise<void> {
    await createOfficer(companyId, appointmentCreateFrom(appointment, person))
    reloadOfficers()
  }

  async function editOfficer(appointmentId: string, changes: OfficerAppointmentUpdate): Promise<void> {
    await updateOfficer(appointmentId, changes)
    reloadOfficers()
  }

  async function removeOfficer(appointmentId: string): Promise<void> {
    await deleteOfficer(appointmentId)
    reloadOfficers()
  }

  async function addShareholding(
    person: PersonFormState,
    holding: ShareholdingFormState,
  ): Promise<void> {
    await createShareholding(companyId, shareholdingCreateFrom(holding, person))
    reloadShareholdings()
  }

  async function editShareholding(
    shareholdingId: string,
    changes: ShareholdingUpdate,
  ): Promise<void> {
    await updateShareholding(shareholdingId, changes)
    reloadShareholdings()
  }

  async function removeShareholding(shareholdingId: string): Promise<void> {
    await deleteShareholding(shareholdingId)
    reloadShareholdings()
  }

  async function generate(form: GenerateDraftForm): Promise<void> {
    await generateDraft(companyId, { form_type: form.formType, target_date: form.targetDate })
    reloadDrafts()
  }

  /** The Download JSON button's save: the API response as a local blob. */
  function downloadAuditReport(): void {
    if (audit.data === null) return
    const blob = new Blob([JSON.stringify(audit.data, null, 2)], { type: 'application/json' })
    const url = URL.createObjectURL(blob)
    const anchor = document.createElement('a')
    anchor.href = url
    anchor.download = reportFileName(audit.data)
    anchor.click()
    URL.revokeObjectURL(url)
  }

  return (
    <CompanyView
      master={master.data}
      batches={batches.data}
      batch={batch.data}
      selectedBatchId={selectedBatchId}
      events={events.data}
      officers={officers.data}
      shareholdings={shareholdings.data}
      drafts={drafts.data}
      onSelectBatch={setPicked}
      onDecide={decide}
      onCreate={create}
      onCreateOfficer={addOfficer}
      onUpdateOfficer={editOfficer}
      onDeleteOfficer={removeOfficer}
      onCreateShareholding={addShareholding}
      onUpdateShareholding={editShareholding}
      onDeleteShareholding={removeShareholding}
      onGenerateDraft={generate}
      auditReport={audit.data}
      auditError={auditRequested && audit.status === 'error' ? audit.error : null}
      onOpenAudit={() => setAuditRequested(true)}
      onRetryAudit={reloadAudit}
      onDownloadAudit={downloadAuditReport}
      onRefresh={() => {
        reloadMaster()
        reloadEvents()
        reloadBatches()
        reloadBatch()
        reloadOfficers()
        reloadShareholdings()
        reloadDrafts()
        if (auditRequested) reloadAudit()
      }}
    />
  )
}

function BackLink() {
  return (
    <p className="muted">
      <a href="#/dashboard">← Portfolio dashboard</a>
    </p>
  )
}
