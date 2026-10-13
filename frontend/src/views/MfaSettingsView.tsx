/**
 * The security settings screen — pure presentation (slice ①, 2026-10-11).
 * Serves a verified session: shows whether two-step sign-in is on, and when
 * it is off, walks the voluntary enrollment (the fresh secret and its
 * otpauth URI once, then the confirming code). The copy is the opt-in
 * wording — the forced-enrollment screen's "must set up before anything
 * else" belongs to the gate flow, not here.
 */

import { useState } from 'react'
import type { FormEvent } from 'react'
import { ApiError } from '../api/client'
import type { MfaEnrollmentBegin } from '../api/types'
import { RefusalNotice } from '../components/RefusalNotice'
import { describeRefusal } from '../renewals'

export function MfaSettingsView({
  checked,
  enrolled,
  begin,
  busy,
  error,
  onBegin,
  onSubmit,
  onRetry,
}: {
  /** False until the status read answered (either way). */
  checked: boolean
  /** The caller's enrollment state per the status read. */
  enrolled: boolean
  /** The enrollment-begin answer while it is held; null before the start. */
  begin: MfaEnrollmentBegin | null
  busy: boolean
  error: unknown
  onBegin: () => Promise<void>
  onSubmit: (code: string) => Promise<void>
  onRetry: () => void
}) {
  const [code, setCode] = useState('')

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (begin === null) return
    await onSubmit(code.trim())
  }

  return (
    <main className="app-main">
      <section className="panel">
        <h1>Security</h1>
        {!checked ? (
          <p className="muted">Loading this account's sign-in settings…</p>
        ) : enrolled ? (
          <>
            <h2>Two-step sign-in</h2>
            <p>
              <span className="badge status-active">On</span> This account signs
              in with a code from an authenticator app.
            </p>
            <p className="muted">
              A lost device is recovered by an operator clearing the
              enrollment — ask your administrator.
            </p>
          </>
        ) : begin === null ? (
          <>
            <h2>Two-step sign-in</h2>
            <p>
              <span className="badge">Off</span> Add a code from an
              authenticator app to every sign-in.
            </p>
            <p className="muted">
              Recommended for anyone signing in from outside the office. You
              will scan a key once, then confirm with a 6-digit code.
            </p>
            <button type="button" disabled={busy} onClick={() => void onBegin()}>
              {busy ? 'Starting…' : 'Start setup'}
            </button>
          </>
        ) : (
          <form onSubmit={submit}>
            <h2>Set up two-step sign-in</h2>
            <p className="muted">
              Add this key to your authenticator app, then enter the 6-digit
              code it shows to confirm. The key is shown only this once.
            </p>
            <label>
              Key (manual entry)
              <input name="mfa_secret" readOnly value={begin.secret} />
            </label>
            <p className="muted">Scanning instead? The app can read this URI:</p>
            <code className="mfa-uri">{begin.uri}</code>
            <label>
              Confirming code
              <input
                name="mfa_code"
                inputMode="numeric"
                autoComplete="one-time-code"
                pattern="[0-9]*"
                maxLength={6}
                value={code}
                onChange={(event) => setCode(event.target.value)}
                required
              />
            </label>
            <button type="submit" disabled={busy}>
              {busy ? 'Confirming…' : 'Confirm setup'}
            </button>
          </form>
        )}
        {error !== null &&
          (error instanceof ApiError && error.status === 422 ? (
            <RefusalNotice error={error} />
          ) : (
            <p className="error" role="alert">
              {describeRefusal(error).message}
              {!enrolled && begin === null && (
                <>
                  {' '}
                  <button type="button" className="link" onClick={onRetry}>
                    Try again
                  </button>
                </>
              )}
            </p>
          ))}
      </section>
    </main>
  )
}
