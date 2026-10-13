/**
 * A refused request, as the panel shows it: the backend's own code and what it
 * means for staff, and every violation it listed — one generic "something went
 * wrong" line is what this exists to avoid.
 *
 * The reading of the detail lives in `src/renewals.ts` (`describeRefusal`), so
 * the shapes can be checked directly; this renders it.
 */

import { describeRefusal } from '../renewals'

export function RefusalNotice({ error }: { error: unknown }) {
  const refusal = describeRefusal(error)
  return (
    <div className="error" role="alert">
      <p>{refusal.message}</p>
      {refusal.violations.length > 0 && (
        <ul className="plain-list">
          {refusal.violations.map((violation) => (
            <li key={violation}>{violation}</li>
          ))}
        </ul>
      )}
    </div>
  )
}
