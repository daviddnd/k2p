/**
 * The calendar push panel (scope §2.4; ADR-0013 item 3 and its 2026-10-03
 * amendment; slice M8) — the feed panel's better half: instead of the
 * TCSP's calendar client polling an `.ics` URL, this deployment pushes the
 * portfolio's deadlines into the TCSP's own Microsoft 365 or Google
 * calendar through the TCSP's **own** app registration, updating events in
 * place. The feed stays as the fallback every tier keeps.
 *
 * The states are the point of this screen, and every one of them is derived
 * from the reads, never held locally (ruling R7):
 *
 *   - **below Standard** — the same §5.1 gate derivation the feed panel
 *     uses (`src/coverage.ts`): the panel explains the refusal with the
 *     gate's own `tier_capability_missing` vocabulary and offers no connect
 *     button, instead of letting the reviewer meet a 403;
 *   - **no credential** — what the push is, the custody note (the client
 *     secret and refresh token live in a mode-0600 file on this deployment,
 *     never the database), and the deployment-flag honesty line: the push
 *     loop is switched on by this deployment's operator
 *     (`AICS_CALENDAR_PUSH_ENABLED`), not by this screen;
 *   - **mid-guide** — the provider choice (Microsoft / Google) fetches the
 *     static registration walk (ruling R6) and renders it as numbered
 *     steps, one step per registration action, from the guide payload —
 *     never hard-coded client-side;
 *   - **credential saved, unprobed** — an unprobed credential never pushes:
 *     the screen says the tenant is still on the feed and offers the probe;
 *   - **probe run** — ruling R5's honest probe reports each leg's own
 *     outcome (pass / fail / skipped with the leg's bounded detail), so a
 *     failing credential shows exactly where it broke and stays retryable;
 *   - **probe passed** — the derived completion: a credential exists AND
 *     `probe_passed` is true. Nothing on this screen can check a "done"
 *     box; the two reads are the only authority;
 *   - **per company** — the push-status read classifies each company
 *     `pushing`, on the `feed`, or `refused` with the gate's own reason.
 *
 * Companies are named from the dashboard's company cards; a company the
 * cards do not list falls back to its short id, never a guess.
 */

import { useState, type ReactNode } from 'react'
import type {
  CalendarProbeResult,
  CalendarProviderName,
  CompanyCard,
  GatingReason,
  OnboardingGuide,
  ProviderCredential,
  ProviderCredentialRequest,
  PushStatus,
} from '../api/types'
import type { CalendarSyncCoverage } from '../coverage'
import { RefusalNotice } from '../components/RefusalNotice'
import { formatDateTime, gatingReasonLabel, shortId } from '../format'

const PROVIDER_LABELS: Record<CalendarProviderName, string> = {
  microsoft: 'Microsoft 365 / Entra',
  google: 'Google Workspace',
}

const PUSH_STATE_LABELS: Record<CompanyPushStatusState, string> = {
  pushing: 'Pushing',
  feed: 'Feed',
  refused: 'Refused',
}

type CompanyPushStatusState = PushStatus['companies'][number]['state']

export function CalendarPushView({
  coverage,
  credential,
  pushStatus,
  guide,
  probeResult,
  companies,
  onSelectProvider,
  onSaveCredential,
  onDeleteCredential,
  onRunProbe,
}: {
  coverage: CalendarSyncCoverage
  credential: ProviderCredential
  pushStatus: PushStatus
  guide: OnboardingGuide | null
  probeResult: CalendarProbeResult | null
  companies: CompanyCard[]
  onSelectProvider: (provider: CalendarProviderName) => Promise<void>
  onSaveCredential: (payload: ProviderCredentialRequest) => Promise<void>
  onDeleteCredential: () => Promise<void>
  onRunProbe: () => Promise<void>
}) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<unknown>(null)
  /** The credential form opens for a new connect, or to replace one. */
  const [formOpen, setFormOpen] = useState(false)
  const [disconnecting, setDisconnecting] = useState(false)

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

  // R7: the only completion authority is the credential read plus the
  // push-status read — a stored credential whose probe has passed.
  const pushing = credential.connected && pushStatus.probe_passed && pushStatus.mode === 'push'

  return (
    <section className="panel">
      <h2>Calendar push (API sync)</h2>
      <p className="muted">
        The better path beside the feed: deadlines land directly in a calendar this deployment
        pushes to — the TCSP&rsquo;s own Microsoft 365 or Google calendar, through the TCSP&rsquo;s own app
        registration — updated in place, with no subscribe action and no poll lag. The{' '}
        <code>.ics</code> feed above stays the fallback: a tenant with no credential, or one whose
        probe has not passed, stays on the feed, which every tier keeps.
      </p>

      {coverage.kind === 'tier_missing' ? (
        <TierMissingPushNotice />
      ) : coverage.kind === 'no_companies' ? (
        <p className="muted">
          This portfolio has no companies yet, so there is nothing for the push to carry.
        </p>
      ) : (
        <>
          <p className="muted">
            <strong>The push loop is a deployment flag.</strong> This screen sets up the credential
            and runs the probe; it cannot switch the loop on. The operator enables the periodic
            push on this deployment with the <code>AICS_CALENDAR_PUSH_ENABLED</code> setting — until
            then, a probed credential pushes on calendar-relevant writes but nothing reconciles in
            the background.
          </p>

          {credential.connected ? (
            <ConnectedCredential
              credential={credential}
              pushing={pushing}
              busy={busy}
              disconnecting={disconnecting}
              onRunProbe={() => run(onRunProbe)}
              onReplace={() => {
                setFormOpen(true)
                setDisconnecting(false)
              }}
              onDisconnect={() => setDisconnecting(true)}
              onCancelDisconnect={() => setDisconnecting(false)}
              onConfirmDisconnect={() =>
                run(async () => {
                  await onDeleteCredential()
                  setDisconnecting(false)
                  setFormOpen(false)
                })
              }
            />
          ) : (
            <p className="muted">No provider credential is connected.</p>
          )}

          {credential.connected && !formOpen ? null : guide === null ? (
            <ProviderChoice
              busy={busy}
              replacing={credential.connected}
              onSelect={(provider) =>
                run(async () => {
                  await onSelectProvider(provider)
                  setFormOpen(true)
                })
              }
            />
          ) : (
            <ConnectWizard
              guide={guide}
              replacing={credential.connected}
              busy={busy}
              onSubmit={(payload) =>
                run(async () => {
                  await onSaveCredential(payload)
                  setFormOpen(false)
                })
              }
            />
          )}

          {probeResult !== null && <ProbeReport result={probeResult} />}

          {error !== null && <RefusalNotice error={error} />}

          <PushStatusTable pushStatus={pushStatus} companies={companies} />
        </>
      )}
    </section>
  )
}

function TierMissingPushNotice() {
  return (
    <div className="notice">
      <p>
        <strong>Calendar push is a Standard feature.</strong> This deployment&rsquo;s licence tier
        does not carry calendar sync, so the §5.1 gate would refuse every push per company (
        <code>tier_capability_missing</code> — the same gate reason the feed panel shows) and there
        is nothing to connect. The <code>.ics</code> feed above refuses the same way; the in-app
        compliance calendar is not gated and keeps showing every deadline.
      </p>
    </div>
  )
}

function ConnectedCredential({
  credential,
  pushing,
  busy,
  disconnecting,
  onRunProbe,
  onReplace,
  onDisconnect,
  onCancelDisconnect,
  onConfirmDisconnect,
}: {
  credential: ProviderCredential
  pushing: boolean
  busy: boolean
  disconnecting: boolean
  onRunProbe: () => void
  onReplace: () => void
  onDisconnect: () => void
  onCancelDisconnect: () => void
  onConfirmDisconnect: () => void
}) {
  return (
    <div className="notice">
      <p>
        <strong>
          Connected to {credential.provider !== null ? PROVIDER_LABELS[credential.provider] : 'the provider'}
          {' — '}calendar <code>{credential.calendar_id}</code>.
        </strong>{' '}
        {pushing ? (
          <>
            The probe has passed: covered companies&rsquo; deadlines are pushed to this calendar,
            and the feed stays as their fallback if the push stops working.
          </>
        ) : (
          <>
            The probe has not passed, so this tenant stays on the <code>.ics</code> feed — an
            unprobed credential never pushes (the fail-safe default).
          </>
        )}
      </p>
      <p className="row-actions">
        {!pushing && (
          <button type="button" onClick={onRunProbe} disabled={busy}>
            {busy ? 'Working…' : 'Run probe'}
          </button>
        )}
        <button type="button" className="secondary" onClick={onReplace} disabled={busy}>
          Replace credential…
        </button>
        {disconnecting ? (
          <>
            <button type="button" className="danger" onClick={onConfirmDisconnect} disabled={busy}>
              {busy ? 'Disconnecting…' : 'Yes, disconnect the push'}
            </button>
            <button type="button" className="secondary" onClick={onCancelDisconnect} disabled={busy}>
              Cancel
            </button>
          </>
        ) : (
          <button type="button" className="secondary" onClick={onDisconnect} disabled={busy}>
            Disconnect…
          </button>
        )}
      </p>
      {disconnecting && (
        <p className="muted">
          Disconnecting forgets the credential: the tenant falls back to the feed and the sweep
          stops seeing the credential. Events already pushed to the calendar are not deleted from
          it (ADR-0012) — remove them in the calendar itself if they are not wanted.
        </p>
      )}
      <p className="muted">
        Connected {credential.created_at !== null ? formatDateTime(credential.created_at) : ''} ·
        credential {credential.credential_id !== null ? shortId(credential.credential_id) : '—'} ·
        last updated{' '}
        {credential.updated_at !== null ? formatDateTime(credential.updated_at) : '—'}. The client
        secret and refresh token are stored in a mode-0600 file on this deployment (ADR-0014
        custody) and cannot be shown again — replacing the credential resets the probe.
      </p>
    </div>
  )
}

function ProviderChoice({
  busy,
  replacing,
  onSelect,
}: {
  busy: boolean
  replacing: boolean
  onSelect: (provider: CalendarProviderName) => void
}) {
  return (
    <>
      <p className="muted">
        {replacing
          ? 'Choose the provider to re-register against. The walk below is the provider half; the credential form and the probe are the same whichever provider is chosen.'
          : 'Choose the provider to register against. The push authenticates as the TCSP\u2019s own app — never a shared vendor registration — so the TCSP\u2019s own Microsoft 365 or Google Cloud administrator performs the registration walk once.'}
      </p>
      <p className="row-actions">
        <button type="button" onClick={() => onSelect('microsoft')} disabled={busy}>
          Microsoft 365 / Entra
        </button>
        <button type="button" onClick={() => onSelect('google')} disabled={busy}>
          Google Workspace
        </button>
      </p>
    </>
  )
}

function ConnectWizard({
  guide,
  replacing,
  busy,
  onSubmit,
}: {
  guide: OnboardingGuide
  replacing: boolean
  busy: boolean
  onSubmit: (payload: ProviderCredentialRequest) => void
}) {
  const [clientId, setClientId] = useState('')
  const [clientSecret, setClientSecret] = useState('')
  const [refreshToken, setRefreshToken] = useState('')
  const [calendarId, setCalendarId] = useState('')
  const [directoryTenantId, setDirectoryTenantId] = useState('')
  const microsoft = guide.provider === 'microsoft'

  return (
    <>
      <GuideSteps markdown={guide.guide} />
      <form
        className="panel"
        onSubmit={(event) => {
          event.preventDefault()
          onSubmit({
            provider: guide.provider,
            client_id: clientId.trim(),
            client_secret: clientSecret,
            refresh_token: refreshToken,
            calendar_id: calendarId.trim(),
            ...(microsoft ? { directory_tenant_id: directoryTenantId.trim() } : {}),
          })
        }}
      >
        <h4>{replacing ? 'Replace the credential' : 'Connect the credential'}</h4>
        <p className="muted">
          The values the registration walk ends with, for{' '}
          <strong>{PROVIDER_LABELS[guide.provider]}</strong>. Saving stores the client secret and
          refresh token in the ADR-0014 file store and resets the probe —{' '}
          {replacing ? 'the new credential pushes only after a fresh probe passes.' : 'the credential pushes only after the probe passes.'}
        </p>
        {microsoft && (
          <label>
            Directory (tenant) ID — the Entra directory the app is registered in
            <input
              name="directory_tenant_id"
              value={directoryTenantId}
              onChange={(event) => setDirectoryTenantId(event.target.value)}
              required
              autoComplete="off"
            />
          </label>
        )}
        <label>
          {microsoft ? 'Application (client) ID' : 'Client ID'}
          <input
            name="client_id"
            value={clientId}
            onChange={(event) => setClientId(event.target.value)}
            required
            autoComplete="off"
          />
        </label>
        <label>
          Client secret — shown once by the provider at creation
          <input
            name="client_secret"
            type="password"
            value={clientSecret}
            onChange={(event) => setClientSecret(event.target.value)}
            required
            autoComplete="new-password"
          />
        </label>
        <label>
          Refresh token — from the one manual OAuth consent
          <input
            name="refresh_token"
            type="password"
            value={refreshToken}
            onChange={(event) => setRefreshToken(event.target.value)}
            required
            autoComplete="new-password"
          />
        </label>
        <label>
          {microsoft
            ? 'Calendar id — the calendar the deadlines land in'
            : 'Calendar ID — primary, or the calendar\u2019s id from its settings page'}
          <input
            name="calendar_id"
            value={calendarId}
            onChange={(event) => setCalendarId(event.target.value)}
            required
            autoComplete="off"
          />
        </label>
        <p className="row-actions">
          <button type="submit" disabled={busy}>
            {busy ? 'Saving…' : replacing ? 'Save the new credential' : 'Save the credential'}
          </button>
        </p>
      </form>
    </>
  )
}

/**
 * The static guide as numbered steps (ruling R6): the payload is Markdown
 * from the one backend source — a title, paragraphs, and one numbered list
 * per section — so this renders exactly that subset. The steps are the
 * guide's own; nothing is hard-coded per provider here.
 */
function GuideSteps({ markdown }: { markdown: string }) {
  return (
    <div className="guide">
      {markdown
        .split(/\n{2,}/)
        .filter((block) => block.trim().length > 0)
        .map((block, index) => {
          const lines = block.split('\n')
          const first = lines[0]
          if (first.startsWith('## ')) {
            return <h4 key={index}>{renderInline(first.slice(3))}</h4>
          }
          if (first.startsWith('# ')) {
            return <h3 key={index}>{renderInline(first.slice(2))}</h3>
          }
          if (/^\d+\.\s/.test(first)) {
            const items: string[] = []
            for (const line of lines) {
              const item = /^(\d+)\.\s(.*)$/.exec(line)
              if (item !== null) {
                items.push(item[2])
              } else if (items.length > 0) {
                items[items.length - 1] += ` ${line.trim()}`
              }
            }
            return (
              <ol key={index}>
                {items.map((text, itemIndex) => (
                  <li key={itemIndex}>{renderInline(text)}</li>
                ))}
              </ol>
            )
          }
          return (
            <p key={index} className="muted">
              {renderInline(lines.map((line) => line.trim()).join(' '))}
            </p>
          )
        })}
    </div>
  )
}

/** The guide's inline Markdown: `**bold**` and `` `code` `` — the only two
 *  marks the static source uses. */
function renderInline(text: string): ReactNode[] {
  return text.split(/(\*\*[^*]+\*\*|`[^`]+`)/g).map((part, index) => {
    if (part.startsWith('**') && part.endsWith('**') && part.length > 4) {
      return <strong key={index}>{part.slice(2, -2)}</strong>
    }
    if (part.startsWith('`') && part.endsWith('`') && part.length > 2) {
      return <code key={index}>{part.slice(1, -1)}</code>
    }
    return part
  })
}

function ProbeReport({ result }: { result: CalendarProbeResult }) {
  return (
    <div className={result.passed ? 'notice' : 'error'} role={result.passed ? undefined : 'alert'}>
      <p>
        <strong>
          {result.passed
            ? 'Probe passed — every leg succeeded.'
            : 'Probe failed — the credential went back to the feed.'}
        </strong>{' '}
        The probe refreshes the token, lists the target calendar, writes a probe event and deletes
        it (ruling R5) — a read-only check could not prove write permission. Each leg reports its
        own outcome; a leg after a failed one is reported as <code>skipped</code>, naming what the
        probe did not do. A failed probe can simply be run again once the credential is fixed.
      </p>
      <table className="table">
        <thead>
          <tr>
            <th scope="col">Leg</th>
            <th scope="col">Outcome</th>
            <th scope="col">Detail</th>
          </tr>
        </thead>
        <tbody>
          {result.legs.map((leg) => (
            <tr key={leg.name}>
              <td>
                <code>{leg.name}</code>
              </td>
              <td>
                {leg.ok ? (
                  <span className="badge status-active">Passed</span>
                ) : (
                  <span className="badge status-unrecorded">
                    {leg.detail === 'skipped' ? 'Skipped' : 'Failed'}
                  </span>
                )}
              </td>
              <td>
                {leg.ok ? (
                  <span className="muted">—</span>
                ) : (
                  leg.detail ?? <span className="muted">—</span>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function PushStatusTable({
  pushStatus,
  companies,
}: {
  pushStatus: PushStatus
  companies: CompanyCard[]
}) {
  const names = new Map(companies.map((card) => [card.company_id, card]))
  return (
    <>
      <h3>How each company&rsquo;s deadlines reach its calendar</h3>
      <p className="muted">
        The classification is the sweep&rsquo;s own: covered companies are <code>pushing</code> when
        the tenant pushes and on the <code>feed</code> otherwise; a company the §5.1 gate denies is{' '}
        <code>refused</code> with the gate&rsquo;s own reason. This read never calls the provider.
      </p>
      <table className="table">
        <thead>
          <tr>
            <th scope="col">Company</th>
            <th scope="col">Delivery</th>
            <th scope="col">Reason</th>
          </tr>
        </thead>
        <tbody>
          {pushStatus.companies.map((company) => {
            const card = names.get(company.company_id)
            return (
              <tr key={company.company_id}>
                <td>
                  {card !== undefined
                    ? `${card.company_name ?? `BRN ${card.br_number}`} (BRN ${card.br_number})`
                    : `company ${shortId(company.company_id)}`}
                </td>
                <td>
                  <span
                    className={
                      company.state === 'pushing'
                        ? 'badge status-active'
                        : company.state === 'feed'
                          ? 'badge status-unrecorded'
                          : 'badge alert'
                    }
                  >
                    {PUSH_STATE_LABELS[company.state] ?? company.state}
                  </span>{' '}
                  <code>{company.state}</code>
                </td>
                <td>
                  {company.state === 'refused' && company.reason !== null ? (
                    <>
                      <code>{company.reason}</code> — {gatingReasonLabel(company.reason as GatingReason)}
                    </>
                  ) : (
                    <span className="muted">—</span>
                  )}
                </td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </>
  )
}
