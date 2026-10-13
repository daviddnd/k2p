/**
 * The challenge step's fetching route: posts `POST /auth/mfa/verify` and, on
 * success, lifts the session marker (the app gate then renders the ordinary
 * screens). The backend refuses a session that is already verified with
 * 409 `mfa_not_required` — the honest answer to a stale challenge screen (a
 * challenge completed in another tab, a re-render over an old marker), so it
 * is treated as passed: the server is the authority on what the session is.
 * A 422 `totp_invalid` is different — the code was refused, nothing was
 * written, and the session stays in the challenge state.
 */

import { useState } from 'react'
import { ApiError } from '../api/client'
import { verifyMfaCode } from '../api/endpoints'
import { mfaChallengePassed, useSessionState } from '../session'
import { MfaChallengeView } from './MfaChallengeView'

export function MfaChallengeRoute() {
  const { session } = useSessionState()
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<unknown>(null)

  if (session === null) return null

  async function submit(code: string): Promise<void> {
    setBusy(true)
    setError(null)
    try {
      await verifyMfaCode(code)
      mfaChallengePassed()
      // The marker lift notifies the app gate; nothing else to do here.
    } catch (caught) {
      if (caught instanceof ApiError && caught.status === 409) {
        mfaChallengePassed()
        return
      }
      setError(caught)
      setBusy(false)
    }
  }

  return (
    <MfaChallengeView
      displayName={session.user.display_name}
      busy={busy}
      error={error}
      onSubmit={submit}
    />
  )
}
