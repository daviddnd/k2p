/**
 * The app shell: sign-in gate, the MFA gate, the pre-use risk-acknowledgment
 * gate, the hash-routed views, and the header.
 *
 * No session → the login view, whatever the hash says. A session that still
 * owes the TOTP step (phase 7, scope §8 — the marker on the stored session)
 * → the challenge or enrollment screen by the marker's `enrollment_state`,
 * before any ordinary view mounts. With a verified session, the shell sits
 * behind the risk-acknowledgment gate (the 2026-10-04 founder rulings): the
 * gate fetches the served acknowledgment state on load and, until it reads
 * `acknowledged`, every role meets the statement interstitial instead of any
 * screen. With the acknowledgment current, the hash picks the view
 * (`#/dashboard`, `#/calendar`, `#/client-actions`,
 * `#/change-events`, `#/settings`, `#/companies/{id}`,
 * `#/companies/{id}/change-events/{eventId}`,
 * `#/companies/{id}/drafts/{draftId}`; anything else is the dashboard), so
 * views are linkable and the back button works. Signing in or out never needs
 * a route change: the session store notifies and this re-renders.
 */

import './App.css'
import { hrefFor, useRoute } from './route'
import { signOut, useSessionState } from './session'
import { CalendarRoute } from './views/CalendarRoute'
import { ChangeEventDetailRoute } from './views/ChangeEventDetailRoute'
import { ChangeEventsQueueRoute } from './views/ChangeEventsQueueRoute'
import { ClientActionsRoute } from './views/ClientActionsRoute'
import { CompanyRoute } from './views/CompanyRoute'
import { DashboardRoute } from './views/DashboardRoute'
import { DraftDetailRoute } from './views/DraftDetailRoute'
import { LoginView } from './views/LoginView'
import { MfaChallengeRoute } from './views/MfaChallengeRoute'
import { MfaEnrollmentRoute } from './views/MfaEnrollmentRoute'
import { MfaSettingsRoute } from './views/MfaSettingsRoute'
import { RiskAcknowledgmentRoute } from './views/RiskAcknowledgmentRoute'

function App() {
  const { session } = useSessionState()
  const route = useRoute()

  if (session === null) return <LoginView />
  if (session.mfa !== null) {
    // The unverified session reaches only the gate's allowlisted MFA
    // surface; the rest of the app stays unmounted until the step passes.
    return session.mfa.enrollment_state === 'pending' ? (
      <MfaEnrollmentRoute />
    ) : (
      <MfaChallengeRoute />
    )
  }

  return (
    <RiskAcknowledgmentRoute>
      <div className="app">
        <header className="app-header">
          <span className="brand">K2P</span>
          <nav className="app-nav">
            <a
              href={hrefFor('dashboard')}
              className={route.view === 'dashboard' ? 'active' : undefined}
            >
              Dashboard
            </a>
            <a
              href={hrefFor('calendar')}
              className={route.view === 'calendar' ? 'active' : undefined}
            >
              Compliance calendar
            </a>
            <a
              href={hrefFor('change-events')}
              className={
                route.view === 'change-events' || route.view === 'change-event' ? 'active' : undefined
              }
            >
              Change events
            </a>
            <a
              href={hrefFor('client-actions')}
              className={route.view === 'client-actions' ? 'active' : undefined}
            >
              Client actions
            </a>
            <a
              href={hrefFor('settings')}
              className={route.view === 'settings' ? 'active' : undefined}
            >
              Security
            </a>
          </nav>
          <span className="session-info">
            <span>{session.user.display_name}</span>
            <span className="muted">
              {session.user.role === 'administrator' ? 'Administrator' : 'General'}
            </span>
            <button type="button" className="secondary" onClick={signOut}>
              Sign out
            </button>
          </span>
        </header>
        <main className="app-main">
          {route.view === 'calendar' ? (
            <CalendarRoute />
          ) : route.view === 'change-events' ? (
            <ChangeEventsQueueRoute />
          ) : route.view === 'settings' ? (
            <MfaSettingsRoute />
          ) : route.view === 'change-event' ? (
            <ChangeEventDetailRoute companyId={route.companyId} eventId={route.eventId} />
          ) : route.view === 'draft' ? (
            <DraftDetailRoute companyId={route.companyId} draftId={route.draftId} />
          ) : route.view === 'client-actions' ? (
            <ClientActionsRoute />
          ) : route.view === 'company' ? (
            <CompanyRoute companyId={route.companyId} />
          ) : (
            <DashboardRoute />
          )}
        </main>
      </div>
    </RiskAcknowledgmentRoute>
  )
}

export default App
