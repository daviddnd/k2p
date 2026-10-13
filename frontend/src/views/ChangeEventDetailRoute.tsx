/**
 * The event detail route (`#/companies/{companyId}/change-events/{eventId}`):
 * owns the four reads behind the screen — the company's master data (the
 * header's name), the company's change events (the list the event is found in,
 * which is what carries the version history), the actor's transitions read,
 * and the event's assessments — plus the transition POST.
 *
 * The API has no single-event read, so the event is selected from the company's
 * list by id; an id that is not on that list is a rendered state, not a crash.
 * After a successful action the list and the transitions read are re-read: the
 * status moved, so the actions the API offers may have changed with it.
 */

import {
  fetchChangeEventAssessments,
  fetchChangeEventTransitions,
  fetchCompanyChangeEvents,
  fetchCompanyMasterData,
  postChangeEventTransition,
} from '../api/endpoints'
import type { TransitionRequest } from '../api/types'
import { ErrorPanel } from '../components/ErrorPanel'
import { isoDate } from '../format'
import { useMfaRefusalGuard } from '../mfa'
import { hrefForCompany } from '../route'
import { useResource } from '../useResource'
import { ChangeEventDetailView } from './ChangeEventDetailView'

export function ChangeEventDetailRoute({
  companyId,
  eventId,
}: {
  companyId: string
  eventId: string
}) {
  const asOf = isoDate(new Date())
  const [master, reloadMaster] = useResource(
    () => fetchCompanyMasterData(companyId, asOf),
    `company-master:${companyId}:${asOf}`,
  )
  const [events, reloadEvents] = useResource(
    () => fetchCompanyChangeEvents(companyId),
    `company-change-events:${companyId}`,
  )
  const [transitions, reloadTransitions] = useResource(
    () => fetchChangeEventTransitions(eventId),
    `change-event-transitions:${eventId}`,
  )
  const [assessments, reloadAssessments] = useResource(
    () => fetchChangeEventAssessments(eventId),
    `change-event-assessments:${eventId}`,
  )
  const firstRefusal =
    [master, events, transitions, assessments].find((read) => read.status === 'error')?.error ??
    null
  useMfaRefusalGuard(firstRefusal)

  const event = events.data?.find((item) => item.id === eventId) ?? null

  if (master.status === 'error') {
    return <LoadFailure companyId={companyId} error={master.error} onRetry={reloadMaster} />
  }
  if (events.status === 'error') {
    return <LoadFailure companyId={companyId} error={events.error} onRetry={reloadEvents} />
  }
  if (master.data === null || events.data === null) {
    return <p className="panel muted">Loading the change event…</p>
  }
  // The company's list is the read that says whether this id exists here at
  // all: judged before the event-scoped reads, so a stale or another
  // company's id is the plain "not on this list" state rather than the
  // transitions read's 404.
  if (event === null) return <NotOnTheCompany companyId={companyId} />
  if (transitions.status === 'error') {
    return <LoadFailure companyId={companyId} error={transitions.error} onRetry={reloadTransitions} />
  }
  if (assessments.status === 'error') {
    return (
      <LoadFailure companyId={companyId} error={assessments.error} onRetry={reloadAssessments} />
    )
  }
  if (transitions.data === null || assessments.data === null) {
    return <p className="panel muted">Loading the change event…</p>
  }

  async function act(request: TransitionRequest): Promise<void> {
    await postChangeEventTransition(eventId, request)
    reloadEvents()
    reloadTransitions()
  }

  const name = master.data.name
  return (
    <ChangeEventDetailView
      companyName={name === null ? null : (name.name_en ?? name.name_zh ?? name.business_name)}
      brNumber={master.data.company.br_number}
      event={event}
      transitions={transitions.data}
      assessments={assessments.data}
      onAct={act}
      onRefresh={() => {
        reloadMaster()
        reloadEvents()
        reloadTransitions()
        reloadAssessments()
      }}
    />
  )
}

function BackLinks({ companyId }: { companyId: string }) {
  return (
    <p className="muted">
      <a href="#/change-events">← Change events awaiting review</a> ·{' '}
      <a href={hrefForCompany(companyId)}>← Company</a>
    </p>
  )
}

function LoadFailure({
  companyId,
  error,
  onRetry,
}: {
  companyId: string
  error: unknown
  onRetry: () => void
}) {
  return (
    <div className="view">
      <BackLinks companyId={companyId} />
      <ErrorPanel error={error} onRetry={onRetry} />
    </div>
  )
}

/**
 * The id is not on the company's list: it belongs to another company, or the
 * link is stale. Said plainly, with the way back — never a blank page.
 */
function NotOnTheCompany({ companyId }: { companyId: string }) {
  return (
    <div className="view">
      <BackLinks companyId={companyId} />
      <section className="panel">
        <h2>No change event with that id is on this company&rsquo;s list</h2>
        <p className="muted">
          The event detail is read from the company&rsquo;s own event list, and this id is not on
          it — it may belong to another company, or the link may be out of date. Open the company
          or the queue and pick the event there.
        </p>
      </section>
    </div>
  )
}
