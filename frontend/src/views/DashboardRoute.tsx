/** The dashboard route: fetch `GET /api/v1/dashboard`, then render it. */

import { fetchDashboard } from '../api/endpoints'
import { ErrorPanel } from '../components/ErrorPanel'
import { useMfaRefusalGuard } from '../mfa'
import { useSessionState } from '../session'
import { useResource } from '../useResource'
import { CalendarFeedRoute } from './CalendarFeedRoute'
import { DashboardView } from './DashboardView'

export function DashboardRoute() {
  const { session } = useSessionState()
  const [resource, reload] = useResource(fetchDashboard, 'dashboard')
  useMfaRefusalGuard(resource.error)

  if (resource.status === 'error') return <ErrorPanel error={resource.error} onRetry={reload} />
  if (resource.data === null) return <p className="panel muted">Loading the portfolio…</p>
  return (
    <>
      <DashboardView data={resource.data} viewerRole={session?.user.role} onRefresh={reload} />
      {/* The calendar feed is Administrator-only at the API, and the panel
          says why the feed refuses below Standard — so it renders for the
          accounts that could act on it. */}
      {session?.user.role === 'administrator' && <CalendarFeedRoute data={resource.data} />}
    </>
  )
}
