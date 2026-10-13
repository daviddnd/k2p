/** The deadline table shared by the dashboard's lists and the calendar. */

import type { CalendarEntry } from '../api/types'
import { formatDate, formatDaysRemaining, formatHkd } from '../format'
import { AlertBadge, RiskBadge } from './badges'

export function DeadlineTable({
  entries,
  emptyText,
}: {
  entries: CalendarEntry[]
  emptyText: string
}) {
  if (entries.length === 0) return <p className="muted">{emptyText}</p>
  return (
    <table className="table">
      <thead>
        <tr>
          <th scope="col">Company</th>
          <th scope="col">Due date</th>
          <th scope="col">Days remaining</th>
          <th scope="col">Alert</th>
          <th scope="col">Risk</th>
          <th scope="col">Late fee if filed today</th>
        </tr>
      </thead>
      <tbody>
        {entries.map((entry) => (
          <tr key={`${entry.company_id}-${entry.due_date}`}>
            <td>
              <span className="company-name">{entry.company_name ?? 'No name on record'}</span>
              <span className="brn">BRN {entry.br_number}</span>
            </td>
            <td>{formatDate(entry.due_date)}</td>
            <td className={entry.overdue ? 'overdue' : undefined}>
              {formatDaysRemaining(entry.days_remaining)}
            </td>
            <td>
              <AlertBadge alert={entry.alert} />
            </td>
            <td>
              <RiskBadge risk={entry.risk} />
            </td>
            <td className={entry.late_fee_exposure.late ? 'overdue' : undefined}>
              {entry.late_fee_exposure.late ? formatHkd(entry.late_fee_exposure.fee_hkd) : '—'}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}
