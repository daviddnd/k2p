/**
 * The pre-use risk-acknowledgment interstitial (the 2026-10-04 founder
 * rulings) — pure presentation. The app gate mounts this in place of the whole
 * shell whenever the served state is not acknowledged; no navigation past it
 * exists.
 *
 * Ruling R5: the statement renders from the API response, verbatim — the
 * served text inside a `pre-wrap` block, never a paraphrase or a client-held
 * copy. The version beside it is the statement's own identifier (a bump is
 * what re-opens this screen), not a summary of the text.
 *
 * An Administrator gets the explicit confirm action — "I have read and
 * understood this statement" — which the route POSTs; the button is disabled
 * while the POST is in flight and a refusal renders the API's own detail (the
 * house refusal pattern). Any other role reads the statement with the plain
 * notice that an Administrator must confirm before the deployment is used —
 * no confirm affordance at all.
 */

import { RefusalNotice } from '../components/RefusalNotice'
import type { RiskAcknowledgment, Role } from '../api/types'

export function RiskAcknowledgmentView({
  data,
  viewerRole,
  busy,
  error,
  onConfirm,
}: {
  data: RiskAcknowledgment
  viewerRole: Role
  busy: boolean
  error: unknown
  onConfirm: () => Promise<void>
}) {
  // The acknowledged state renders nothing: the interstitial is exactly the
  // not-acknowledged surface, and the gate's "acknowledged" branch is the
  // ordinary shell.
  if (data.acknowledged) return null
  const isAdministrator = viewerRole === 'administrator'

  return (
    <main className="login">
      <div className="panel risk-ack-card">
        <p className="muted">Statement version {data.statement_version}</p>
        <div className="risk-statement">{data.statement_text}</div>
        {isAdministrator ? (
          <>
            {error !== null && <RefusalNotice error={error} />}
            <button type="button" disabled={busy} onClick={() => void onConfirm()}>
              {busy ? 'Confirming…' : 'I have read and understood this statement'}
            </button>
          </>
        ) : (
          <p className="notice">
            An Administrator must confirm this statement before this deployment
            is used. This account cannot confirm it — please ask an
            Administrator to read the statement above and confirm it.
          </p>
        )}
      </div>
    </main>
  )
}
