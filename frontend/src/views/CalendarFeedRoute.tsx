/**
 * The calendar-sync settings route: the feed panel's credentials plus the
 * push half — the provider credential, the push status, the guide fetched
 * on the operator's provider choice, and the last probe's result.
 *
 * The coverage state comes from the dashboard payload the route is rendered
 * inside — the same gate flags both panels explain (see `src/coverage.ts`),
 * so neither panel guesses at the tier.
 *
 * Route state, the `issued` precedent: the guide and the probe result live
 * here, not in the view, so they survive the credential/status reloads that
 * follow a save or a probe and are dropped only when the operator
 * disconnects. Completion itself is never route state — the view derives it
 * from the credential read plus the push-status read (ruling R7).
 */

import { useState } from 'react'
import {
  createFeedCredential,
  deleteProviderCredential,
  fetchFeedCredentials,
  fetchOnboardingGuide,
  fetchProviderCredential,
  fetchPushStatus,
  revokeFeedCredential,
  runCalendarProbe,
  saveProviderCredential,
} from '../api/endpoints'
import type {
  CalendarProbeResult,
  DashboardResponse,
  IssuedFeedCredential,
  OnboardingGuide,
  ProviderCredentialRequest,
} from '../api/types'
import { calendarSyncCoverage } from '../coverage'
import { ErrorPanel } from '../components/ErrorPanel'
import { useMfaRefusalGuard } from '../mfa'
import { useResource } from '../useResource'
import { CalendarFeedView } from './CalendarFeedView'
import { CalendarPushView } from './CalendarPushView'

export function CalendarFeedRoute({ data }: { data: DashboardResponse }) {
  const [credentials, reload] = useResource(fetchFeedCredentials, 'feed-credentials')
  const [providerCredential, reloadProviderCredential] = useResource(
    fetchProviderCredential,
    'provider-credential',
  )
  const [pushStatus, reloadPushStatus] = useResource(fetchPushStatus, 'calendar-push-status')
  useMfaRefusalGuard(credentials.error)
  useMfaRefusalGuard(providerCredential.error)
  useMfaRefusalGuard(pushStatus.error)
  const [issued, setIssued] = useState<IssuedFeedCredential | null>(null)
  const [guide, setGuide] = useState<OnboardingGuide | null>(null)
  const [probeResult, setProbeResult] = useState<CalendarProbeResult | null>(null)
  const coverage = calendarSyncCoverage(data.company_cards, data.service_gaps)

  const readError = credentials.error ?? providerCredential.error ?? pushStatus.error
  if (readError !== null) {
    return <ErrorPanel error={readError} onRetry={() => {
      reload()
      reloadProviderCredential()
      reloadPushStatus()
    }} />
  }
  if (credentials.data === null || providerCredential.data === null || pushStatus.data === null) {
    return <p className="panel muted">Loading the calendar sync settings…</p>
  }

  return (
    <>
      <CalendarFeedView
        coverage={coverage}
        credentials={credentials.data}
        issued={issued}
        onCreate={async () => {
          setIssued(await createFeedCredential())
          reload()
        }}
        onRevoke={async (credentialId) => {
          await revokeFeedCredential(credentialId)
          reload()
        }}
        onDismissIssued={() => setIssued(null)}
      />
      <CalendarPushView
        coverage={coverage}
        credential={providerCredential.data}
        pushStatus={pushStatus.data}
        guide={guide}
        probeResult={probeResult}
        companies={data.company_cards}
        onSelectProvider={async (provider) => setGuide(await fetchOnboardingGuide(provider))}
        onSaveCredential={async (payload: ProviderCredentialRequest) => {
          await saveProviderCredential(payload)
          // A new secret means an unprobed credential: the old probe's
          // result no longer describes the stored credential.
          setProbeResult(null)
          reloadProviderCredential()
          reloadPushStatus()
        }}
        onDeleteCredential={async () => {
          await deleteProviderCredential()
          setGuide(null)
          setProbeResult(null)
          reloadProviderCredential()
          reloadPushStatus()
        }}
        onRunProbe={async () => {
          setProbeResult(await runCalendarProbe())
          reloadProviderCredential()
          reloadPushStatus()
        }}
      />
    </>
  )
}
