/**
 * The failure panel: one place for "the read did not happen", with a retry
 * affordance. 403 reads as "not permitted", a dead network as "could not
 * reach the server", anything else as the status plus the backend's detail.
 *
 * A refusal the backend answered as a **list** of violations is shown as the
 * list — every field it names, not one joined line — which is what the
 * configuration reads and the panel's own 409 can carry.
 */

import { describeRefusal } from '../renewals'

export function ErrorPanel({ error, onRetry }: { error: unknown; onRetry: () => void }) {
  const refusal = describeRefusal(error)
  return (
    <div className="panel error-panel" role="alert">
      <div>
        <p>{refusal.message}</p>
        {refusal.violations.length > 0 && (
          <ul className="plain-list">
            {refusal.violations.map((violation) => (
              <li key={violation}>{violation}</li>
            ))}
          </ul>
        )}
      </div>
      <button type="button" onClick={onRetry}>
        Retry
      </button>
    </div>
  )
}
