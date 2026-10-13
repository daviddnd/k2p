/**
 * Forced MFA enrollment (phase 7, scope §8) — pure presentation. The
 * Administrator's first sign-in must prove a TOTP device before anything
 * else: this screen shows the fresh secret and its otpauth URI once (the
 * only moment they exist), notes the manual-entry path for an app that
 * cannot scan, and takes the one confirming code. The secret lives in the
 * route's component state between begin and complete — never sessionStorage,
 * never a fixture.
 */

import { useState } from 'react'
import type { FormEvent } from 'react'
import { ApiError } from '../api/client'
import type { MfaEnrollmentBegin } from '../api/types'
import { RefusalNotice } from '../components/RefusalNotice'
import { describeRefusal } from '../renewals'

export function MfaEnrollmentView({
  begin,
  busy,
  error,
  onBegin,
  onSubmit,
}: {
  /** The enrollment-begin answer while it is held; null before the first
   * begin (or when the secret was dismissed). */
  begin: MfaEnrollmentBegin | null
  busy: boolean
  error: unknown
  onBegin: () => Promise<void>
  onSubmit: (code: string) => Promise<void>
}) {
  const [code, setCode] = useState('')

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (begin === null) return
    await onSubmit(code.trim())
  }

  return (
    <main className="login">
      <form className="panel login-card" onSubmit={submit}>
        <h1>Set up two-step sign-in</h1>
        {begin === null ? (
          <>
            <p className="muted">
              This account must set up two-step sign-in before anything else.
              Start to get a fresh key for your authenticator app.
            </p>
            <button type="button" disabled={busy} onClick={() => void onBegin()}>
              {busy ? 'Starting…' : 'Start enrollment'}
            </button>
          </>
        ) : (
          <>
            <p className="muted">
              Add this key to your authenticator app, then enter the 6-digit
              code it shows to confirm. The key is shown only this once.
            </p>
            <label>
              Key (manual entry)
              <input name="mfa_secret" readOnly value={begin.secret} />
            </label>
            <p className="muted">
              Scanning instead? The app can read this URI:
            </p>
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
          </>
        )}
        {error !== null &&
          (error instanceof ApiError && error.status === 422 ? (
            <RefusalNotice error={error} />
          ) : (
            <p className="error" role="alert">
              {describeRefusal(error).message}
            </p>
          ))}
        {begin !== null && (
          <button type="submit" disabled={busy}>
            {busy ? 'Confirming…' : 'Confirm enrollment'}
          </button>
        )}
      </form>
    </main>
  )
}
