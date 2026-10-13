/**
 * The TOTP challenge step (phase 7, scope §8) — pure presentation. Shown by
 * the app gate when the session owes the challenge: one 6-digit code from the
 * authenticator app. The route posts it; a refusal keeps the session in the
 * challenge state and the named code is shown with its remedy
 * (`totp_invalid` — wrong, malformed and replayed are one refusal, so the
 * screen says nothing about which it was).
 */

import { useState } from 'react'
import type { FormEvent } from 'react'
import { ApiError } from '../api/client'
import { RefusalNotice } from '../components/RefusalNotice'
import { describeRefusal } from '../renewals'

export function MfaChallengeView({
  displayName,
  busy,
  error,
  onSubmit,
}: {
  displayName: string
  busy: boolean
  error: unknown
  onSubmit: (code: string) => Promise<void>
}) {
  const [code, setCode] = useState('')

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    await onSubmit(code.trim())
  }

  return (
    <main className="login">
      <form className="panel login-card" onSubmit={submit}>
        <h1>Two-step sign-in</h1>
        <p className="muted">
          Enter the 6-digit code from your authenticator app, {displayName}.
        </p>
        <label>
          Code
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
        {error !== null &&
          (error instanceof ApiError && error.status === 422 ? (
            <RefusalNotice error={error} />
          ) : (
            <p className="error" role="alert">
              {describeRefusal(error).message}
            </p>
          ))}
        <button type="submit" disabled={busy}>
          {busy ? 'Verifying…' : 'Verify and continue'}
        </button>
      </form>
    </main>
  )
}
