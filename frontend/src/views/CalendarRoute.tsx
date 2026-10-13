/**
 * The calendar route: owns the requested range (the fetch input), defaults
 * to the last 30 days through the next 180 (inside the backend's 366-day
 * cap), and re-reads whenever the range is applied.
 */

import { useState } from 'react'
import { fetchCalendar } from '../api/endpoints'
import { ErrorPanel } from '../components/ErrorPanel'
import { addDays, isoDate } from '../format'
import { useMfaRefusalGuard } from '../mfa'
import { useResource } from '../useResource'
import { CalendarView } from './CalendarView'

function defaultRange(): { from: string; to: string } {
  const today = new Date()
  return { from: isoDate(addDays(today, -30)), to: isoDate(addDays(today, 180)) }
}

export function CalendarRoute() {
  const [range, setRange] = useState(defaultRange)
  const [resource, reload] = useResource(
    () => fetchCalendar(range.from, range.to),
    `${range.from}..${range.to}`,
  )
  useMfaRefusalGuard(resource.error)

  if (resource.status === 'error') return <ErrorPanel error={resource.error} onRetry={reload} />
  if (resource.data === null) return <p className="panel muted">Loading the calendar…</p>
  return (
    <CalendarView
      data={resource.data}
      range={range}
      onApplyRange={setRange}
      onRefresh={reload}
    />
  )
}
