/** Shared badges: the risk colours, the alert band, the company status, and
 * the change-event and draft lifecycles' own statuses. */

import type { AlertKind, RiskLevel } from '../api/types'
import { changeEventStatusLabel } from '../change_events'
import { draftStatusLabel } from '../drafts'
import { alertLabel, companyStatusLabel, riskLabel } from '../format'

export function RiskBadge({ risk }: { risk: RiskLevel }) {
  return <span className={`badge risk-${risk}`}>{riskLabel(risk)}</span>
}

export function AlertBadge({ alert }: { alert: AlertKind | null }) {
  if (alert === null) return <span className="muted">—</span>
  return <span className="badge alert">{alertLabel(alert)}</span>
}

export function CompanyStatusBadge({ status }: { status: string | null }) {
  const slug = status === null ? 'unrecorded' : status
  return <span className={`badge status-${slug}`}>{companyStatusLabel(status)}</span>
}

export function ChangeEventStatusBadge({ status }: { status: string }) {
  return <span className={`badge event-${status}`}>{changeEventStatusLabel(status)}</span>
}

export function DraftStatusBadge({ status }: { status: string }) {
  return <span className={`badge draft-${status}`}>{draftStatusLabel(status)}</span>
}
