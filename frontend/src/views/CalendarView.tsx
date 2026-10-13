/**
 * The compliance calendar (scope §2.4): the deadlines in
 * `GET /api/v1/compliance/calendar?from=&to=`, with the 30/15/7-day alert
 * bands and the green/amber/red risk colours.
 *
 * Pure presentation like the dashboard: the route owns the requested range
 * (which is *input* to the fetch) and this view owns the client-side search,
 * risk filter and sort over the entries already loaded. The range is limited
 * to 366 days by the backend and refused beyond (422); the form pre-checks
 * the same bound so the mistake reads as a form error, not a failed request.
 */

import { useState } from 'react'
import type { CalendarEntry, CalendarResponse, RiskLevel } from '../api/types'
import { DeadlineTable } from '../components/DeadlineTable'
import { NotGeneratedPanel } from '../components/NotGeneratedPanel'
import { formatDate } from '../format'

type SortOption = 'due' | 'due_desc' | 'company' | 'risk'

const RISK_ORDER: Record<RiskLevel, number> = { red: 0, amber: 1, green: 2 }

const RISK_FILTERS: Array<{ value: 'all' | RiskLevel; label: string }> = [
  { value: 'all', label: 'All risks' },
  { value: 'red', label: 'Red' },
  { value: 'amber', label: 'Amber' },
  { value: 'green', label: 'Green' },
]

const SORTS: Array<{ value: SortOption; label: string }> = [
  { value: 'due', label: 'Due date (soonest first)' },
  { value: 'due_desc', label: 'Due date (latest first)' },
  { value: 'company', label: 'Company name' },
  { value: 'risk', label: 'Risk (most urgent first)' },
]

export function CalendarView({
  data,
  range,
  onApplyRange,
  onRefresh,
}: {
  data: CalendarResponse
  range: { from: string; to: string }
  onApplyRange: (range: { from: string; to: string }) => void
  onRefresh?: () => void
}) {
  const [fromInput, setFromInput] = useState(range.from)
  const [toInput, setToInput] = useState(range.to)
  const [rangeError, setRangeError] = useState<string | null>(null)
  const [search, setSearch] = useState('')
  const [risk, setRisk] = useState<'all' | RiskLevel>('all')
  const [sort, setSort] = useState<SortOption>('due')

  function apply() {
    const error = validateRange(fromInput, toInput)
    setRangeError(error)
    if (error === null) onApplyRange({ from: fromInput, to: toInput })
  }

  const needle = search.trim().toLowerCase()
  const entries = data.entries
    .filter((entry) => {
      if (needle !== '') {
        const haystack = `${entry.company_name ?? ''} ${entry.br_number}`.toLowerCase()
        if (!haystack.includes(needle)) return false
      }
      return risk === 'all' || entry.risk === risk
    })
    .sort(comparatorFor(sort))

  return (
    <div className="view">
      <section className="panel">
        <header className="panel-head">
          <div>
            <h2>Compliance calendar</h2>
            <p className="muted">
              NAR1 deadlines between {formatDate(data.from)} and {formatDate(data.to)}, read as of{' '}
              {formatDate(data.as_of)}. Risk colour: green = more than 15 days remaining, amber = 15
              days or fewer, red = 7 days or fewer or overdue; alert bands are shown at 30, 15 and 7
              days.
            </p>
          </div>
          {onRefresh !== undefined && (
            <button type="button" className="secondary" onClick={onRefresh}>
              Refresh
            </button>
          )}
        </header>
        <div className="filters">
          <label>
            From
            <input
              type="date"
              value={fromInput}
              onChange={(event) => setFromInput(event.target.value)}
            />
          </label>
          <label>
            To
            <input type="date" value={toInput} onChange={(event) => setToInput(event.target.value)} />
          </label>
          <button type="button" onClick={apply}>
            Apply range
          </button>
        </div>
        {rangeError !== null && (
          <p className="error" role="alert">
            {rangeError}
          </p>
        )}
        <p className="muted">
          Any range up to 366 days can be requested. The 15-day change-filing window is not a
          calendar entry: it is shown on each change event — the review queue, a company&rsquo;s
          event list and the event&rsquo;s detail.
        </p>
      </section>

      <section className="panel">
        <div className="filters">
          <label>
            Search
            <input
              type="search"
              placeholder="Name or BRN"
              value={search}
              onChange={(event) => setSearch(event.target.value)}
            />
          </label>
          <label>
            Risk
            <select value={risk} onChange={(event) => setRisk(event.target.value as 'all' | RiskLevel)}>
              {RISK_FILTERS.map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </select>
          </label>
          <label>
            Sort
            <select value={sort} onChange={(event) => setSort(event.target.value as SortOption)}>
              {SORTS.map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </select>
          </label>
        </div>
        <p className="muted">
          Showing {entries.length} of {data.entries.length} deadlines.
        </p>
        <DeadlineTable
          entries={entries}
          emptyText={
            data.entries.length === 0
              ? 'No NAR1 deadlines fall in this range. Widen the range to look further ahead or back.'
              : 'No deadline matches these filters.'
          }
        />
      </section>

      <NotGeneratedPanel items={data.not_generated} />
    </div>
  )
}

/** The backend's own guard, pre-checked: ≤ 366 days, `to` not before `from`. */
function validateRange(from: string, to: string): string | null {
  if (from === '' || to === '') return 'Choose both a start and an end date.'
  if (to < from) return 'The end date is before the start date.'
  const days = (Date.parse(`${to}T00:00:00Z`) - Date.parse(`${from}T00:00:00Z`)) / 86_400_000 + 1
  if (days > 366) return 'The range is limited to 366 days.'
  return null
}

function comparatorFor(sort: SortOption): (a: CalendarEntry, b: CalendarEntry) => number {
  if (sort === 'due_desc') {
    return (a, b) => b.due_date.localeCompare(a.due_date) || a.br_number.localeCompare(b.br_number)
  }
  if (sort === 'company') {
    return (a, b) =>
      (a.company_name ?? a.br_number).localeCompare(b.company_name ?? b.br_number) ||
      a.br_number.localeCompare(b.br_number)
  }
  if (sort === 'risk') {
    return (a, b) =>
      RISK_ORDER[a.risk] - RISK_ORDER[b.risk] ||
      a.due_date.localeCompare(b.due_date) ||
      a.br_number.localeCompare(b.br_number)
  }
  return (a, b) => a.due_date.localeCompare(b.due_date) || a.br_number.localeCompare(b.br_number)
}
