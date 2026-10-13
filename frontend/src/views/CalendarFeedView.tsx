/**
 * The calendar subscription feed panel (scope §2.4; ADR-0013 item 3; contract
 * §5.1.5) — the whole UI for one-way calendar sync while the Microsoft 365 /
 * Google push is unbuilt.
 *
 * The states are the point of this screen:
 *
 *   - **below Standard** — the gate denies `calendar_sync` for every company,
 *     which the dashboard's own flags already say, so the panel explains the
 *     refusal ("calendar sync is a Standard feature…") and offers no create
 *     button instead of letting the reviewer meet a 403;
 *   - **covered, no credential** — what the feed is, and the disclosure that
 *     matters: whichever calendar provider the TCSP subscribes with will see
 *     the portfolio's company names, BRNs and due dates. That is the point of
 *     the feature, so it is stated before the button, not after;
 *   - **just created** — the subscription URL, shown once, with a copy
 *     affordance and the warning that it cannot be shown again (only a digest
 *     is stored);
 *   - **an existing credential** — metadata only, because there is no field
 *     that could carry the secret back;
 *   - **revoking** — a confirmation step, because it is irreversible for the
 *     URL: the provider's next fetch answers 401.
 *
 * Companies whose packages do not cover calendar sync are named, not silently
 * absent: the feed serves the covered portfolio, and the panel says which
 * companies that leaves out and why.
 */

import { useState } from 'react'
import { errorText } from '../api/client'
import type { CalendarSyncCoverage } from '../coverage'
import type { FeedCredential, IssuedFeedCredential } from '../api/types'
import { formatDateTime, gatingReasonLabel, shortId } from '../format'

export function CalendarFeedView({
  coverage,
  credentials,
  issued,
  onCreate,
  onRevoke,
  onDismissIssued,
}: {
  coverage: CalendarSyncCoverage
  credentials: FeedCredential[]
  issued: IssuedFeedCredential | null
  onCreate: () => Promise<void>
  onRevoke: (credentialId: string) => Promise<void>
  onDismissIssued: () => void
}) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<unknown>(null)
  const [revoking, setRevoking] = useState<string | null>(null)

  const active = credentials.find((credential) => credential.revoked_at === null) ?? null

  async function run(action: () => Promise<void>) {
    setBusy(true)
    setError(null)
    try {
      await action()
    } catch (caught) {
      setError(caught)
    } finally {
      setBusy(false)
    }
  }

  return (
    <section className="panel">
      <h2>Calendar subscription feed (ICS)</h2>
      <p className="muted">
        A one-way subscription: your calendar fetches this portfolio&rsquo;s NAR1 due dates and the
        30/15/7-day alerts, and refreshes them itself. Any standards-compliant calendar can
        subscribe (Outlook, Google Calendar, Apple Calendar). Deadlines may only change through the
        audited in-app workflow — this feed is read-only, and two-way sync is not part of v1.0.
      </p>

      {coverage.kind === 'tier_missing' ? (
        <TierMissingNotice />
      ) : coverage.kind === 'no_companies' ? (
        <p className="muted">
          This portfolio has no companies yet, so there is nothing for a feed to carry.
        </p>
      ) : (
        <>
          <p className="notice">
            <strong>Your calendar provider will see this data.</strong> The subscription body
            carries each covered company&rsquo;s name, BRN and statutory due dates — whichever
            provider you subscribe with (Microsoft 365, Google, or another) will therefore see
            exactly that, in your own calendar account. Only companies whose service package covers
            calendar sync appear; the URL can be revoked at any time.
          </p>

          {issued !== null ? (
            <IssuedUrl credential={issued} onDismiss={onDismissIssued} />
          ) : active !== null ? (
            <p className="muted">
              This deployment has an active subscription URL. It is stored only as a digest and
              cannot be shown again — if it was lost, revoke it below and create a new one.
            </p>
          ) : (
            <p className="muted">No subscription URL exists yet.</p>
          )}

          {coverage.excluded.length > 0 && (
            <p className="muted">
              These companies are not in the feed, because their service package does not cover
              calendar sync:{' '}
              {coverage.excluded
                .map(
                  (item) =>
                    `${item.company_name ?? `BRN ${item.br_number}`} (${gatingReasonLabel(
                      item.reason,
                    )})`,
                )
                .join('; ')}
              .
            </p>
          )}
        </>
      )}

      {coverage.kind === 'covered' && active === null && issued === null && (
        <p>
          <button type="button" onClick={() => run(onCreate)} disabled={busy}>
            {busy ? 'Creating…' : 'Create subscription URL'}
          </button>
        </p>
      )}

      {error !== null && (
        <p className="error" role="alert">
          {errorText(error)}
        </p>
      )}

      {credentials.length > 0 && (
        <>
          <h3>Credentials</h3>
          <table className="table">
            <thead>
              <tr>
                <th scope="col">Credential</th>
                <th scope="col">Created</th>
                <th scope="col">Revoked</th>
                <th scope="col">State</th>
              </tr>
            </thead>
            <tbody>
              {credentials.map((credential) => (
                <tr key={credential.id}>
                  <td>
                    <span className="brn">feed credential {shortId(credential.id)}</span>
                    {credential.created_by !== null && (
                      <span className="brn">created by staff {shortId(credential.created_by)}</span>
                    )}
                  </td>
                  <td>{formatDateTime(credential.created_at)}</td>
                  <td>
                    {credential.revoked_at === null ? (
                      <span className="muted">—</span>
                    ) : (
                      formatDateTime(credential.revoked_at)
                    )}
                  </td>
                  <td>
                    {credential.revoked_at === null ? (
                      <>
                        <span className="badge status-active">Active</span>
                        {revoking === credential.id ? (
                          <span className="row-actions">
                            <button
                              type="button"
                              className="danger"
                              disabled={busy}
                              onClick={() =>
                                run(async () => {
                                  await onRevoke(credential.id)
                                  setRevoking(null)
                                })
                              }
                            >
                              {busy ? 'Revoking…' : 'Yes, revoke the feed'}
                            </button>
                            <button
                              type="button"
                              className="secondary"
                              disabled={busy}
                              onClick={() => setRevoking(null)}
                            >
                              Cancel
                            </button>
                          </span>
                        ) : (
                          <span className="row-actions">
                            <button
                              type="button"
                              className="secondary"
                              onClick={() => setRevoking(credential.id)}
                            >
                              Revoke…
                            </button>
                          </span>
                        )}
                        {revoking === credential.id && (
                          <span className="muted">
                            Revoking stops the feed answering for this URL on its next fetch. Events
                            a subscriber already fetched stay in that calendar — nothing is deleted
                            from it.
                          </span>
                        )}
                      </>
                    ) : (
                      <span className="badge status-unrecorded">Revoked</span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}
    </section>
  )
}

function TierMissingNotice() {
  return (
    <div className="notice">
      <p>
        <strong>Calendar sync is a Standard feature.</strong> This deployment&rsquo;s licence tier
        does not carry it, so the feed refuses to serve (
        <code>tier_capability_missing</code> — the same gate reason the service-coverage panel
        shows) rather than serving an empty calendar that would read as &ldquo;no
        deadlines&rdquo;. The in-app compliance calendar is not gated and keeps showing every
        deadline.
      </p>
      <p>
        An existing subscription URL would stop refreshing until the tier covers calendar sync
        again; an already-fetched calendar keeps the events it holds.
      </p>
    </div>
  )
}

function IssuedUrl({
  credential,
  onDismiss,
}: {
  credential: IssuedFeedCredential
  onDismiss: () => void
}) {
  const [copied, setCopied] = useState<'idle' | 'copied' | 'manual'>('idle')

  async function copy() {
    try {
      await navigator.clipboard.writeText(credential.subscription_url)
      setCopied('copied')
    } catch {
      // No clipboard (an insecure context, or a denied permission): the URL
      // is on screen and selectable, which is the fallback.
      setCopied('manual')
    }
  }

  return (
    <div className="notice">
      <p>
        <strong>Copy this URL now — it is shown once and cannot be shown again.</strong> The
        deployment stores only a digest of the token, so there is no read path that can return it.
      </p>
      <p className="issued-url">
        <input type="text" readOnly value={credential.subscription_url} aria-label="Subscription URL" />
        <button type="button" onClick={copy}>
          Copy
        </button>
        <button type="button" className="secondary" onClick={onDismiss}>
          Done — hide it
        </button>
      </p>
      <p className="muted">
        {copied === 'copied'
          ? 'Copied to the clipboard.'
          : copied === 'manual'
            ? 'The clipboard is unavailable here — select the URL and copy it manually.'
            : 'Paste it into your calendar client\u2019s “subscribe from URL” field. A calendar that already fetched the feed keeps the events it holds, even after revocation.'}
      </p>
    </div>
  )
}
