/**
 * The per-company audit compliance report (contract §5.1 Audit row; DoD 19) —
 * the wire shape of `GET /api/v1/companies/{id}/audit-report` and the pure
 * presentation rules over it.
 *
 * The backend classifies every audit action into one of the five report
 * areas and carries the area on each entry, so this module translates and
 * orders — it never re-derives audit semantics from action strings. The five
 * areas render in a fixed order (the §2.13 capture-list order) with a count
 * each, including an honest zero, because the compliance read is also proof
 * of what did *not* happen.
 *
 * Pure and framework-free: no fetching, no I/O. The route fetches the
 * report; the view renders what this module groups and labels.
 */

import { shortId } from './format'

/** DoD 18's five report areas — the closed vocabulary the API answers. */
export type ReportArea =
  | 'client_communications'
  | 'event_lifecycle'
  | 'data_changes'
  | 'ai_suggestions'
  | 'approvals'

export interface AuditReportEntry {
  seq: number
  occurred_at: string
  /** The acting user's id, or null for system-written entries. */
  actor: string | null
  action: string
  area: ReportArea
  entity_type: string | null
  entity_id: string | null
  details: Record<string, unknown>
}

export interface AuditReportChain {
  verified: boolean
  checked_entries: number
  problems: string[]
  /** Ruling R6's note: the verdict covers the tenant's full chain. */
  scope: string
}

export interface AuditReportCompany {
  company_id: string
  name: string | null
  br_number: string
  cr_number: string | null
}

export interface AuditReport {
  report_version: number
  generated_at: string
  company: AuditReportCompany
  chain: AuditReportChain
  entries: AuditReportEntry[]
}

export const REPORT_AREA_ORDER: ReportArea[] = [
  'client_communications',
  'event_lifecycle',
  'data_changes',
  'ai_suggestions',
  'approvals',
]

export const REPORT_AREA_LABELS: Record<ReportArea, string> = {
  client_communications: 'Client communications',
  event_lifecycle: 'Event lifecycle',
  data_changes: 'Data changes',
  ai_suggestions: 'AI suggestions',
  approvals: 'Approvals',
}

export interface AreaGroup {
  area: ReportArea
  label: string
  entries: AuditReportEntry[]
}

/** The entries grouped under each area, newest first, in the fixed order. */
export function groupEntriesByArea(entries: AuditReportEntry[]): AreaGroup[] {
  const newestFirst = [...entries].sort(
    (a, b) => Date.parse(b.occurred_at) - Date.parse(a.occurred_at) || b.seq - a.seq,
  )
  return REPORT_AREA_ORDER.map((area) => ({
    area,
    label: REPORT_AREA_LABELS[area],
    entries: newestFirst.filter((entry) => entry.area === area),
  }))
}

/**
 * Who acted: "system" for entries the platform wrote itself, otherwise the
 * actor's id in its short form (the full id is in the downloaded JSON; the
 * response does not carry emails — see the task report).
 */
export function actorLabel(entry: AuditReportEntry): string {
  return entry.actor === null ? 'system' : shortId(entry.actor)
}

/** What was acted on: the entity type with its id, or "—" for tenant-wide entries. */
export function entitySummary(entry: AuditReportEntry): string {
  if (entry.entity_type === null) return '—'
  return entry.entity_id === null
    ? entry.entity_type
    : `${entry.entity_type} ${shortId(entry.entity_id)}`
}

/** The blob's filename: `{br_number}-audit-report-{generated date}.json`. */
export function reportFileName(report: AuditReport): string {
  return `${report.company.br_number}-audit-report-${report.generated_at.slice(0, 10)}.json`
}
