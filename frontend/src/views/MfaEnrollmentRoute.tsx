/**
 * The enrollment screen's fetching route. Re-checks `GET /auth/mfa/status`
 * on mount — the server is the authority on what the session owes, and the
 * answers cover the races honestly:
 *
 *   - already verified            → lift the marker (a challenge completed
 *     elsewhere; the app gate renders the ordinary screens);
 *   - enrolled but not verified   → move the marker to the challenge screen
 *     (enrollment was sealed but the code step never ran);
 *   - pending                     → this screen, holding the begin answer in
 *     component state. Completing seals the enrollment and moves the marker
 *     to the challenge — the session owes the challenge next, and the brief's
 *     shape for complete answers `enrollment_state: "enrolled"`.
 *
 * The plaintext secret lives only in this component's state between begin
 * and complete — never sessionStorage, never a fixture, never the URL.
 */

import { useEffect, useState } from 'react'
import { ApiError } from '../api/client'
import { beginMfaEnrollment, completeMfaEnrollment, fetchMfaStatus } from '../api/endpoints'
import type { MfaEnrollmentBegin } from '../api/types'
import {
  mfaChallengePassed,
  mfaEnrollmentSealed,
  mfaRequiredFromServer,
  useSessionState,
} from '../session'
import { MfaEnrollmentView } from './MfaEnrollmentView'

export function MfaEnrollmentRoute() {
  const { session } = useSessionState()
  const [checked, setChecked] = useState(false)
  const [begin, setBegin] = useState<MfaEnrollmentBegin | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<unknown>(null)

  useEffect(() => {
    if (checked || session === null) return
    let cancelled = false
    fetchMfaStatus().then(
      (status) => {
        if (cancelled) return
        setChecked(true)
        if (status.mfa_verified) mfaChallengePassed()
        else if (status.enrollment_state === 'enrolled') mfaEnrollmentSealed()
        else mfaRequiredFromServer('pending')
      },
      () => {
        // A dead token is the client's own 401 → sign-in; a dead network or
        // any other refusal shows below through the retry.
        if (!cancelled) setChecked(true)
      },
    )
    return () => {
      cancelled = true
    }
  }, [checked, session])

  if (session === null) return null
  if (!checked) {
    return (
      <main className="login">
        <p className="panel muted">Checking this session's sign-in step…</p>
      </main>
    )
  }

  async function start(): Promise<void> {
    setBusy(true)
    setError(null)
    try {
      setBegin(await beginMfaEnrollment())
    } catch (caught) {
      setError(caught)
    } finally {
      setBusy(false)
    }
  }

  async function submit(code: string): Promise<void> {
    if (begin === null) return
    setBusy(true)
    setError(null)
    try {
      await completeMfaEnrollment(code, begin.secret)
      setBegin(null)
      mfaEnrollmentSealed()
    } catch (caught) {
      if (caught instanceof ApiError && caught.status === 409) {
        // Already enrolled (e.g. a completed enrollment in another tab): the
        // challenge is what this session owes next.
        setBegin(null)
        mfaEnrollmentSealed()
        return
      }
      setError(caught)
      setBusy(false)
    }
  }

  return (
    <MfaEnrollmentView
      begin={begin}
      busy={busy}
      error={error}
      onBegin={start}
      onSubmit={submit}
    />
  )
}
