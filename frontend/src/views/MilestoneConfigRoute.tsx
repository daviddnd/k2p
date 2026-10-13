/**
 * The milestone configuration route: fetches the tenant's effective
 * configuration and owns the save. Administrator-only at the API, so this is
 * mounted only for an Administrator (see `ClientActionsRoute`, the
 * `DashboardRoute` → `CalendarFeedRoute` precedent).
 *
 * An unreadable stored block is **not** an error state here, deliberately: the
 * read answers with that block's shipped defaults and names the block and its
 * violations, so the screen that reports the problem is also the screen that
 * offers the repair (`MilestoneConfigView` states it, and the ordinary save is
 * the `PUT` that replaces the row). What fails here is only what fails
 * everywhere: not permitted, the server unreachable, a status this screen does
 * not know — the `ErrorPanel` case.
 */

import { fetchTenantConfig, saveTenantConfig } from '../api/endpoints'
import { ErrorPanel } from '../components/ErrorPanel'
import { useMfaRefusalGuard } from '../mfa'
import { useResource } from '../useResource'
import { MilestoneConfigView } from './MilestoneConfigView'

export function MilestoneConfigRoute() {
  const [config, reload] = useResource(fetchTenantConfig, 'tenant-config')
  useMfaRefusalGuard(config.error)

  if (config.status === 'error') {
    return <ErrorPanel error={config.error} onRetry={reload} />
  }
  if (config.data === null) {
    return <p className="panel muted">Loading the milestone configuration…</p>
  }
  return (
    <MilestoneConfigView
      config={config.data}
      onSave={async (payload) => {
        await saveTenantConfig(payload)
        reload()
      }}
    />
  )
}
