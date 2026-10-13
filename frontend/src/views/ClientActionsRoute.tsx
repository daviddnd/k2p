/**
 * The client-actions route: owns the panel read — the look-ahead window is its
 * input — and the three acts staff record about a period (a decision, the fee,
 * a correction). The panel is a portfolio-wide read, so it is open to every
 * active account; the milestone configuration beside it is Administrator-only
 * at the API and renders only for an Administrator, the way the dashboard
 * renders the calendar-feed panel.
 *
 * **Recording an answer on a period with no record** opens the record first:
 * the decision route is about a row ("create it first" is its own refusal for a
 * period that has none), and the create is create-or-return and idempotent, so
 * the panel does it in the same act and says so on the form. The reload after a
 * write is the panel's own re-read: the acts change the period's status and
 * every milestone state derived from it.
 */

import { useState } from 'react'
import {
  correctRenewalDecision,
  fetchClientActions,
  openServiceRenewal,
  recordRenewalDecision,
  recordRenewalPayment,
} from '../api/endpoints'
import type { ClientActionPeriod } from '../api/types'
import { ErrorPanel } from '../components/ErrorPanel'
import { useSessionState } from '../session'
import { useResource } from '../useResource'
import { useMfaRefusalGuard } from '../mfa'
import { ClientActionsView, type RenewalAct } from './ClientActionsView'
import { MilestoneConfigRoute } from './MilestoneConfigRoute'

/** The API's own default horizon for the panel (§5.1.9). */
const DEFAULT_DUE_WITHIN_DAYS = 90

export function ClientActionsRoute() {
  const { session } = useSessionState()
  const [dueWithin, setDueWithin] = useState(DEFAULT_DUE_WITHIN_DAYS)
  const [panel, reload] = useResource(
    () => fetchClientActions(dueWithin),
    `client-actions:${dueWithin}`,
  )
  useMfaRefusalGuard(panel.error)

  async function act(period: ClientActionPeriod, action: RenewalAct): Promise<void> {
    let wrote = false
    try {
      if (action.kind === 'decision') {
        if (period.renewal_status === null) {
          await openServiceRenewal(period.company_id, period.anniversary_year)
          wrote = true
        }
        await recordRenewalDecision(
          period.company_id,
          period.anniversary_year,
          action.decision,
          action.note,
        )
        wrote = true
      } else if (action.kind === 'payment') {
        await recordRenewalPayment(period.company_id, period.anniversary_year, action.note)
        wrote = true
      } else {
        await correctRenewalDecision(
          period.company_id,
          period.anniversary_year,
          action.decision,
          action.note,
        )
        wrote = true
      }
    } finally {
      // A write that landed is re-read even when a later call in the same act
      // was refused: a create followed by a refused decision leaves the period's
      // row behind, and the panel would otherwise still show the old status.
      if (wrote) reload()
    }
  }

  if (panel.status === 'error') return <ErrorPanel error={panel.error} onRetry={reload} />
  if (panel.data === null) return <p className="panel muted">Loading the client periods…</p>
  return (
    <>
      <ClientActionsView
        data={panel.data}
        dueWithin={dueWithin}
        viewerRole={session?.user.role}
        onApplyWindow={setDueWithin}
        onRefresh={reload}
        onAct={act}
      />
      {session?.user.role === 'administrator' && <MilestoneConfigRoute />}
    </>
  )
}
