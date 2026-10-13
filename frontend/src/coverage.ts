/**
 * Which companies the calendar feed serves, derived from the dashboard's own
 * "service not covered" flags (scope §2.4; contract §5.1.5).
 *
 * The feed refuses portfolio-wide when the §5.1 gate denies `calendar_sync`
 * for every company — on Free, or after a lease lapses, the reason is the
 * tier's own `tier_capability_missing`. The dashboard payload already carries
 * exactly that decision per company, because the flags come from the same
 * gate, so the panel explains the refusal *before* anyone creates a
 * credential the feed would never serve.
 *
 * The rule is a translation of the gate's vocabulary, not a second gate: a
 * company is excluded when its flag says so, and the whole portfolio is
 * uncovered only when every company is flagged for the tier reason.
 */

import type { CompanyCard, GatingReason, ServiceGap } from './api/types'

export interface ExcludedCompany {
  company_id: string
  company_name: string | null
  br_number: string
  reason: GatingReason
}

export interface CalendarSyncCoverage {
  kind: 'no_companies' | 'tier_missing' | 'covered'
  /** Companies the feed will not serve, and the gate's reason for each. */
  excluded: ExcludedCompany[]
}

export function calendarSyncCoverage(
  cards: CompanyCard[],
  gaps: ServiceGap[],
): CalendarSyncCoverage {
  if (cards.length === 0) return { kind: 'no_companies', excluded: [] }
  const byCompany = new Map(gaps.filter(isCalendarSync).map((gap) => [gap.company_id, gap]))
  const excluded: ExcludedCompany[] = []
  for (const card of cards) {
    const gap = byCompany.get(card.company_id)
    if (gap === undefined) continue
    excluded.push({
      company_id: card.company_id,
      company_name: card.company_name,
      br_number: card.br_number,
      reason: gap.reason,
    })
  }
  const tierMissing =
    excluded.length === cards.length &&
    excluded.every((item) => item.reason === 'tier_capability_missing')
  return { kind: tierMissing ? 'tier_missing' : 'covered', excluded }
}

function isCalendarSync(gap: ServiceGap): boolean {
  return gap.automation === 'calendar_sync'
}
