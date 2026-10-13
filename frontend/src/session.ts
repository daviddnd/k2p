/**
 * The staff session: the bearer token and the signed-in user, held in
 * `sessionStorage` and subscribed to by React.
 *
 * **Where the token lives, and the caveat.** `sessionStorage` (not
 * `localStorage`, not a cookie) so a page refresh keeps the session and
 * closing the tab ends it. The caveat: any script running on this origin can
 * read it — an XSS hole is a stolen token. The hardening is a cookie the
 * page cannot read (`HttpOnly`) plus CSRF protection, which the backend does
 * not have yet (it authenticates with an `Authorization: Bearer` header and
 * has no CSRF seam). This is a deliberate v1 choice; revisit with the Phase 7
 * security hardening, not as an oversight.
 *
 * The store is framework-thin on purpose: `getSessionState` is the whole
 * state, `subscribe` is the whole change notification, and the API client
 * (the only writer besides login/logout) calls `expireSession` on a 401.
 *
 * The MFA marker (phase 7, scope §8; slice ① opened enrollment to General
 * staff, 2026-10-11): a session that has not passed the TOTP challenge (an
 * Administrator's, an enrolled staff member's — or the forced-enrollment
 * step an unverified Administrator owes) is stored with a marker naming
 * which step it owes — the app gate renders the challenge or enrollment
 * screen instead of the ordinary views. It carries no secret; the TOTP
 * secret never touches this store.
 */

import { useSyncExternalStore } from 'react'
import type { LoginResponse, SessionUser } from './api/types'

const TOKEN_KEY = 'aics.session.token'
const USER_KEY = 'aics.session.user'
/** The pending-MFA step (`enrolled` → challenge, `pending` → enrollment) for
 * a session whose token is stored but which has not passed the TOTP step.
 * Absent (or `null`) means the session is verified — the ordinary case. */
const MFA_KEY = 'aics.session.mfa'

export interface Session {
  token: string
  user: SessionUser
  /** Non-null only while the session owes the TOTP challenge or enrollment. */
  mfa: MfaMarker | null
}

export interface MfaMarker {
  enrollment_state: 'enrolled' | 'pending'
}

export interface SessionState {
  session: Session | null
  /** `'expired'` when a 401 forced the sign-out, so login can say why. */
  notice: 'expired' | null
}

function storage(): Storage | null {
  // Absent outside a browser (the SSR verification entry imports this
  // module); there a session simply does not survive.
  return typeof sessionStorage === 'undefined' ? null : sessionStorage
}

function readSession(): Session | null {
  const store = storage()
  if (store === null) return null
  try {
    const token = store.getItem(TOKEN_KEY)
    const user = store.getItem(USER_KEY)
    if (token === null || user === null) return null
    const mfaRaw = store.getItem(MFA_KEY)
    return {
      token,
      user: JSON.parse(user) as SessionUser,
      mfa: mfaRaw === null ? null : (JSON.parse(mfaRaw) as MfaMarker),
    }
  } catch {
    // Corrupt or unreadable storage: fail closed and ask for a sign-in.
    return null
  }
}

function writeSession(session: Session | null): void {
  const store = storage()
  if (store === null) return
  try {
    if (session === null) {
      store.removeItem(TOKEN_KEY)
      store.removeItem(USER_KEY)
      store.removeItem(MFA_KEY)
    } else {
      store.setItem(TOKEN_KEY, session.token)
      store.setItem(USER_KEY, JSON.stringify(session.user))
      if (session.mfa === null) store.removeItem(MFA_KEY)
      else store.setItem(MFA_KEY, JSON.stringify(session.mfa))
    }
  } catch {
    // Storage full or blocked: the in-memory session still works.
  }
}

let state: SessionState = { session: readSession(), notice: null }
const listeners = new Set<() => void>()

function setState(next: SessionState): void {
  state = next
  for (const listener of listeners) listener()
}

export function subscribeSession(listener: () => void): () => void {
  listeners.add(listener)
  return () => {
    listeners.delete(listener)
  }
}

export function getSessionState(): SessionState {
  return state
}

export function getSessionToken(): string | null {
  return state.session?.token ?? null
}

export function startSession(response: LoginResponse): void {
  const session: Session = {
    token: response.access_token,
    user: response.user,
    // The token is stored even while the TOTP step is owed: it reaches only
    // the gate's allowlisted MFA surface until the challenge passes. The
    // marker is what keeps the app on the challenge/enrollment screens.
    mfa: response.mfa_required
      ? { enrollment_state: response.enrollment_state === 'pending' ? 'pending' : 'enrolled' }
      : null,
  }
  writeSession(session)
  setState({ session, notice: null })
}

/** The challenge passed (or the server says the session is already verified):
 * the marker lifts and the app proceeds to the ordinary screens. */
export function mfaChallengePassed(): void {
  const current = state.session
  if (current === null || current.mfa === null) return
  const session: Session = { ...current, mfa: null }
  writeSession(session)
  setState({ session, notice: null })
}

/** Enrollment sealed but the session still owes the challenge: move the
 * marker from the enrollment screen to the challenge screen. */
export function mfaEnrollmentSealed(): void {
  const current = state.session
  if (current === null) return
  const session: Session = { ...current, mfa: { enrollment_state: 'enrolled' } }
  writeSession(session)
  setState({ session, notice: null })
}

/** The server refused a fetch with the session unverified (the guard in
 * `mfa.ts` checked `GET /auth/mfa/status`): pin the marker to the state the
 * server named and let the app gate swap screens. */
export function mfaRequiredFromServer(enrollment_state: 'enrolled' | 'pending'): void {
  const current = state.session
  if (current === null) return
  const session: Session = { ...current, mfa: { enrollment_state } }
  writeSession(session)
  setState({ session, notice: null })
}

export function signOut(): void {
  writeSession(null)
  setState({ session: null, notice: null })
}

export function expireSession(): void {
  writeSession(null)
  setState({ session: null, notice: 'expired' })
}

export function useSessionState(): SessionState {
  // The third argument is required for server rendering (the SSR
  // verification entry); here the module state is the same on both sides.
  return useSyncExternalStore(subscribeSession, getSessionState, getSessionState)
}
