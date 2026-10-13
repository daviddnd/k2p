/**
 * The change-event queue route: fetch `GET /api/v1/change-events`, then render
 * it. One read; the API's order and grant-scoping travel with the payload.
 */

import { fetchChangeEventQueue } from '../api/endpoints'
import { ErrorPanel } from '../components/ErrorPanel'
import { useMfaRefusalGuard } from '../mfa'
import { useResource } from '../useResource'
import { ChangeEventsQueueView } from './ChangeEventsQueueView'

export function ChangeEventsQueueRoute() {
  const [resource, reload] = useResource(fetchChangeEventQueue, 'change-event-queue')
  useMfaRefusalGuard(resource.error)

  if (resource.status === 'error') return <ErrorPanel error={resource.error} onRetry={reload} />
  if (resource.data === null) return <p className="panel muted">Loading the change-event queue…</p>
  return <ChangeEventsQueueView items={resource.data} onRefresh={reload} />
}
