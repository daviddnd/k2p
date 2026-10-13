/**
 * The security settings screen's fetching route (slice ①, 2026-10-11) — the
 * staff surface that binds an authenticator. Unlike the forced-enrollment
 * screen (which runs before anything else mounts, for an unverified
 * session), this screen serves a *verified* session: a General staff member
 * opts in from here, and an Administrator reaches it already enrolled.
 *
 * The server is the authority on enrollment: the route reads
 * `GET /auth/mfa/status` on mount and renders from it. "Start setup" calls
 * enrollment begin and holds the returned secret in component state — never
 * sessionStorage — until the confirming code seals the enrollment. A 409
 * `mfa_already_enrolled` (a race with another tab) simply re-reads the
 * status. The session marker is deliberately NOT touched: completing
 * enrollment from a live session leaves it verified — the challenge is owed
 * from the *next* sign-in, and the login response's `mfa_required` is what
 * moves the app gate then.
 */

import { useEffect, useState } from 'react'
import { ApiError } from '../api/client'
import { beginMfaEnrollment, completeMfaEnrollment, fetchMfaStatus } from '../api/endpoints'
import type { MfaEnrollmentBegin } from '../api/types'
import { MfaSettingsView } from './MfaSettingsView'

export function MfaSettingsRoute() {
  const [checked, setChecked] = useState(false)
  const [enrolled, setEnrolled] = useState(false)
  const [begin, setBegin] = useState<MfaEnrollmentBegin | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<unknown>(null)

  useEffect(() => {
    if (checked) return
    let cancelled = false
    fetchMfaStatus().then(
      (status) => {
        if (cancelled) return
        setEnrolled(status.enrollment_state === 'enrolled')
        setChecked(true)
      },
      (caught) => {
        // A 401 is the client's own sign-out path; anything else renders
        // through the retry surface below.
        if (!cancelled) {
          setError(caught)
          setChecked(true)
        }
      },
    )
    return () => {
      cancelled = true
    }
  }, [checked])

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
      setEnrolled(true)
    } catch (caught) {
      if (caught instanceof ApiError && caught.status === 409) {
        // Sealed elsewhere (another tab): the status read is the truth.
        setBegin(null)
        setEnrolled(true)
        return
      }
      setError(caught)
      setBusy(false)
    }
  }

  return (
    <MfaSettingsView
      checked={checked}
      enrolled={enrolled}
      begin={begin}
      busy={busy}
      error={error}
      onBegin={start}
      onSubmit={submit}
      onRetry={() => {
        setError(null)
        setChecked(false)
      }}
    />
  )
}
