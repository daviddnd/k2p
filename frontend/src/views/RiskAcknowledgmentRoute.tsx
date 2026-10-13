/**
 * The route-level risk-acknowledgment gate: the fetching half of the
 * interstitial, mounted by the app shell around every screen.
 *
 * On app load (a fresh mount, so the state is re-read every time — nothing is
 * cached across loads) the gate fetches `GET /api/v1/risk-acknowledgment`.
 * While the read is in flight the shell stays unmounted behind a loading
 * card; a failed read gets the failure panel with its retry. When the served
 * state says `acknowledged: false`, every role meets the interstitial instead
 * of the shell — an Administrator can confirm (the POST unlocks on success),
 * everyone else reads the statement with the awaiting-Administrator notice.
 * A statement version bump answers `acknowledged: false` again on the next
 * load, so the re-consent surface re-opens by construction.
 */

import { useState } from 'react'
import type { ReactNode } from 'react'
import { acceptRiskAcknowledgment, fetchRiskAcknowledgment } from '../api/endpoints'
import { useResource } from '../useResource'
import { useSessionState } from '../session'
import { ErrorPanel } from '../components/ErrorPanel'
import { RiskAcknowledgmentView } from './RiskAcknowledgmentView'

export function RiskAcknowledgmentRoute({ children }: { children: ReactNode }) {
  const { session } = useSessionState()
  const [resource, reload] = useResource(fetchRiskAcknowledgment, 'risk-acknowledgment')
  const [confirmed, setConfirmed] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<unknown>(null)

  if (session === null) return null

  if (resource.status === 'error') {
    return (
      <main className="login">
        <ErrorPanel error={resource.error} onRetry={reload} />
      </main>
    )
  }
  if (resource.status === 'loading' || resource.data === null) {
    return (
      <main className="login">
        <div className="panel login-card">
          <p className="muted">Checking this deployment's acknowledgment state…</p>
        </div>
      </main>
    )
  }
  if (confirmed || resource.data.acknowledged) return <>{children}</>

  async function confirm(): Promise<void> {
    setBusy(true)
    setError(null)
    try {
      await acceptRiskAcknowledgment()
      // The POST answered the resulting state: acknowledged. The next load
      // re-derives it from the chain, so nothing here outlives the session.
      setConfirmed(true)
    } catch (caught) {
      setError(caught)
    } finally {
      setBusy(false)
    }
  }

  return (
    <RiskAcknowledgmentView
      data={resource.data}
      viewerRole={session.user.role}
      busy={busy}
      error={error}
      onConfirm={confirm}
    />
  )
}
