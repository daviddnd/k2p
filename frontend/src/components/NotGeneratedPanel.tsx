/**
 * The companies for which no deadline was generated, and the backend's own
 * reason (`nar1_rule_not_established`). Deliberate, not an error: the domain
 * refused to guess a due date.
 *
 * The dashboard can name these companies (its cards carry every visible
 * company); the calendar payload carries only the id and the company type,
 * so it passes no `nameFor` and the panel falls back to a shortened id.
 */

import type { NotGenerated } from '../api/types'

export function NotGeneratedPanel({
  items,
  nameFor,
}: {
  items: NotGenerated[]
  nameFor?: (companyId: string) => string | null
}) {
  if (items.length === 0) return null
  return (
    <section className="panel">
      <h2>Deadlines not generated</h2>
      <p className="muted">
        No NAR1 due date is generated for these companies: the product has no established due-date
        rule for their company type, and reports that rather than guessing a date.
      </p>
      <ul className="plain-list">
        {items.map((item) => (
          <li key={item.company_id}>
            <span className="company-name">
              {nameFor?.(item.company_id) ?? `Company ${item.company_id.slice(0, 8)}`}
            </span>{' '}
            <span className="muted">
              — company type: {item.company_type} — reason: {item.reason}
            </span>
          </li>
        ))}
      </ul>
    </section>
  )
}
