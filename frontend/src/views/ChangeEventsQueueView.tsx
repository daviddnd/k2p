/**
 * The cross-company review queue (`#/change-events`): every change event whose
 * current version awaits staff review, across the companies this account can
 * see, in the API's order — the soonest statutory deadline first.
 *
 * Pure presentation over `GET /api/v1/change-events`: the route fetches, this
 * renders. The list is **not** re-sorted and carries **no actions**: the API
 * owns the order and the grant-scoping, and the allowed-actions authority is
 * the per-event transitions read, which each row's detail view makes. The
 * window beside every row is the API's own `change_window` — deadline, days
 * remaining, the calendar's risk colour and alert band — so "approaching" and
 * "late" read in the product's existing vocabulary.
 *
 * An empty queue is a rendered state, not a blank page: nothing awaits review,
 * or this account's grants cover no company with one.
 */

import type { ChangeEventQueueItem } from '../api/types'
import { AlertBadge, ChangeEventStatusBadge, RiskBadge } from '../components/badges'
import { changeEventTypeLabel } from '../change_events'
import { formatDate, formatDaysRemaining } from '../format'
import { hrefForChangeEvent, hrefForCompany } from '../route'

export function ChangeEventsQueueView({
  items,
  onRefresh,
}: {
  items: ChangeEventQueueItem[]
  onRefresh?: () => void
}) {
  return (
    <div className="view">
      <section className="panel">
        <header className="panel-head">
          <div>
            <h1>Change events awaiting review</h1>
            <p className="muted">
              Every change event whose current version awaits staff review — <em>pending</em> or{' '}
              <em>under review</em> — in the API&rsquo;s order: the soonest statutory deadline first.
              Each row is the event&rsquo;s current version; the event&rsquo;s link opens it with
              its version history and the actions the API allows this account to take.
            </p>
          </div>
          {onRefresh !== undefined && (
            <button type="button" className="secondary" onClick={onRefresh}>
              Refresh
            </button>
          )}
        </header>

        {items.length === 0 ? (
          <p className="muted">
            No change event is awaiting staff review. The queue is scoped to the companies this
            account can see — an account with no company access grants sees nothing here — and it
            fills as events are entered by hand or (later) extracted from client communication.
          </p>
        ) : (
          <>
            <p className="muted">
              {items.length} event{items.length === 1 ? '' : 's'} awaiting review. The 15-day window
              beside each one is counted from the change&rsquo;s effective date, not from the day the
              TCSP learned of it.
            </p>
            <table className="table">
              <thead>
                <tr>
                  <th scope="col">Company</th>
                  <th scope="col">Change</th>
                  <th scope="col">Status</th>
                  <th scope="col">Effective date</th>
                  <th scope="col">Window deadline</th>
                  <th scope="col">Days remaining</th>
                  <th scope="col">Risk</th>
                  <th scope="col"></th>
                </tr>
              </thead>
              <tbody>
                {items.map((item) => (
                  <tr key={item.id}>
                    <td>
                      <a href={hrefForCompany(item.company_id)}>
                        <span className="company-name">
                          {item.company_name ?? `Company ${item.br_number}`}
                        </span>
                      </a>
                      <span className="brn">BRN {item.br_number}</span>
                    </td>
                    <td>{changeEventTypeLabel(item.type)}</td>
                    <td>
                      <ChangeEventStatusBadge status={item.status} />
                    </td>
                    <td>{formatDate(item.effective_date)}</td>
                    <td>{formatDate(item.change_window.deadline)}</td>
                    <td className={item.change_window.is_late ? 'overdue' : undefined}>
                      {formatDaysRemaining(item.change_window.days_remaining)}
                      {item.change_window.is_late && <span className="muted"> — late</span>}
                    </td>
                    <td>
                      <span className="identity-badges">
                        <RiskBadge risk={item.change_window.risk} />
                        <AlertBadge alert={item.change_window.alert} />
                      </span>
                    </td>
                    <td>
                      <a href={hrefForChangeEvent(item.company_id, item.id)}>Open event</a>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </>
        )}
      </section>
    </div>
  )
}
