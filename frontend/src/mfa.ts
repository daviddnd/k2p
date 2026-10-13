/**
 * The MFA routing guard (phase 7, scope §8).
 *
 * The gate's refusal is a **403**, not a 401, so `client.ts` — the single
 * 401→sign-in point — stays untouched: it expires the session only when the
 * credential is dead. An `mfa_verification_required` 403 instead means the
 * credential is fine but the TOTP step is owed. The refusal's structured body
 * (E3's shape, `{code, enrollment_state}`) does not survive into `ApiError`,
 * so this guard settles the question the honest way: it re-reads
 * `GET /auth/mfa/status` (allowlisted for exactly this session state) and
 * trusts the server's own `mfa_verified` flag —
 *
 *   - `mfa_verified: false` → the session is genuinely unverified: pin the
 *     session marker to the state's `enrollment_state` and the app gate swaps
 *     to the challenge or enrollment screen;
 *   - `mfa_verified: true`  → the 403 was an ordinary authorization refusal,
 *     the error surface keeps it.
 *
 * `useMfaRefusalGuard` is the route-layer wiring: a read route passes its
 * resource error and the hook does the rest. It runs in an effect (so SSR
 * verification never executes it) and only while a session is held.
 */

import { useEffect } from 'react'
import { ApiError } from './api/client'
import { fetchMfaStatus } from './api/endpoints'
import { getSessionState, mfaRequiredFromServer } from './session'

/** A 403 this guard can answer; anything else is left to the error surface. */
function isForbidden(error: unknown): error is ApiError {
  return error instanceof ApiError && error.status === 403
}

export function useMfaRefusalGuard(error: unknown): void {
  useEffect(() => {
    if (!isForbidden(error)) return
    if (getSessionState().session === null) return
    let cancelled = false
    fetchMfaStatus().then(
      (status) => {
        if (cancelled || status.mfa_verified) return
        mfaRequiredFromServer(
          status.enrollment_state === 'pending' ? 'pending' : 'enrolled',
        )
      },
      () => {
        // The status read itself refused (401 → the client expired the
        // session; anything else → the route's error surface stands).
      },
    )
    return () => {
      cancelled = true
    }
  }, [error])
}
