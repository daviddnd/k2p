/**
 * Staff sign-in. Posts to `POST /api/v1/auth/login`; the session store keeps
 * the token. A 401 here means wrong credentials (the backend deliberately
 * does not say which part), not an expired session; anything else is shown
 * through `errorText`, and a network failure offers a retry by simply
 * submitting again.
 */

import { useState } from 'react'
import type { FormEvent } from 'react'
import { ApiError, errorText } from '../api/client'
import { login } from '../api/endpoints'
import { startSession, useSessionState } from '../session'

export function LoginView() {
  const { notice } = useSessionState()
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState<unknown>(null)
  const [busy, setBusy] = useState(false)

  async function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    setBusy(true)
    setError(null)
    try {
      startSession(await login(email, password))
      // On success the session store notifies and the app swaps this view
      // out; there is no state left to update here.
    } catch (caught) {
      setError(caught)
      setBusy(false)
    }
  }

  return (
    <main className="login">
      <form className="panel login-card" onSubmit={onSubmit}>
        <h1>Kairos Pinax</h1>
        <p className="muted">Sign in with your staff account.</p>
        {notice === 'expired' && (
          <p className="notice" role="status">
            Your session has ended — sign in again to continue.
          </p>
        )}
        <label>
          Email
          <input
            type="email"
            value={email}
            onChange={(event) => setEmail(event.target.value)}
            autoComplete="username"
            required
          />
        </label>
        <label>
          Password
          <input
            type="password"
            value={password}
            onChange={(event) => setPassword(event.target.value)}
            autoComplete="current-password"
            required
          />
        </label>
        {error !== null && (
          <p className="error" role="alert">
            {error instanceof ApiError && error.status === 401 ? (
              'Email or password is incorrect.'
            ) : error instanceof ApiError && error.status === 429 ? (
              error.retryAfterSeconds !== null ? (
                <>
                  Too many sign-in attempts — try again in about{' '}
                  {error.retryAfterSeconds} seconds.
                </>
              ) : (
                'Too many sign-in attempts — wait a moment and try again.'
              )
            ) : (
              errorText(error)
            )}
          </p>
        )}
        <button type="submit" disabled={busy}>
          {busy ? 'Signing in…' : 'Sign in'}
        </button>
      </form>
    </main>
  )
}
