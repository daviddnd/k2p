/**
 * Hash routing, hand-rolled: the views are linkable (`#/dashboard`,
 * `#/calendar`, `#/client-actions`, `#/change-events`, `#/settings`,
 * `#/companies/{id}`, `#/companies/{id}/change-events/{eventId}`,
 * `#/companies/{id}/drafts/{draftId}`) and the browser's back button works,
 * with no router dependency. The login view is not a route — the app shows it
 * whenever no session is held, whatever the hash says; an unknown or empty
 * hash falls back to the dashboard.
 *
 * The company route and the two detail routes carry ids, so `parseRoute`
 * returns a union the app switches on rather than a bare name. The detail
 * patterns are matched before the plain company prefix: they begin with the
 * same `#/companies/` text, and the company parse would otherwise take the
 * whole tail as one id.
 */

import { useSyncExternalStore } from 'react'

export type Route =
  | { view: 'dashboard' }
  | { view: 'calendar' }
  | { view: 'client-actions' }
  | { view: 'change-events' }
  | { view: 'settings' }
  | { view: 'company'; companyId: string }
  | { view: 'change-event'; companyId: string; eventId: string }
  | { view: 'draft'; companyId: string; draftId: string }

const COMPANY_PREFIX = '#/companies/'
const CHANGE_EVENT_PREFIX = '/change-events/'
const DRAFT_PREFIX = '/drafts/'
const EVENT_IN_COMPANY = /^#\/companies\/([^/]+)\/change-events\/([^/]+)$/
const DRAFT_IN_COMPANY = /^#\/companies\/([^/]+)\/drafts\/([^/]+)$/

export function parseRoute(hash: string): Route {
  if (hash === '#/calendar') return { view: 'calendar' }
  if (hash === '#/client-actions') return { view: 'client-actions' }
  if (hash === '#/change-events') return { view: 'change-events' }
  if (hash === '#/settings') return { view: 'settings' }
  const event = EVENT_IN_COMPANY.exec(hash)
  if (event !== null) {
    return { view: 'change-event', companyId: event[1], eventId: event[2] }
  }
  const draft = DRAFT_IN_COMPANY.exec(hash)
  if (draft !== null) {
    return { view: 'draft', companyId: draft[1], draftId: draft[2] }
  }
  if (hash.startsWith(COMPANY_PREFIX)) {
    const companyId = hash.slice(COMPANY_PREFIX.length)
    // A malformed id is the API's 422/404 to report, not a routing decision
    // to make here: an empty one is not a company view at all.
    if (companyId !== '') return { view: 'company', companyId }
  }
  return { view: 'dashboard' }
}

function subscribe(listener: () => void): () => void {
  window.addEventListener('hashchange', listener)
  return () => window.removeEventListener('hashchange', listener)
}

function getSnapshot(): string {
  return window.location.hash
}

export function useRoute(): Route {
  return parseRoute(useSyncExternalStore(subscribe, getSnapshot))
}

export function hrefFor(
  route: 'dashboard' | 'calendar' | 'client-actions' | 'change-events' | 'settings',
): string {
  if (route === 'calendar') return '#/calendar'
  if (route === 'client-actions') return '#/client-actions'
  if (route === 'change-events') return '#/change-events'
  if (route === 'settings') return '#/settings'
  return '#/dashboard'
}

export function hrefForCompany(companyId: string): string {
  return `${COMPANY_PREFIX}${companyId}`
}

/** The per-event detail: the company is in the path because the API has no
 * single-event read — the detail is found in the company's own event list. */
export function hrefForChangeEvent(companyId: string, eventId: string): string {
  return `${COMPANY_PREFIX}${companyId}${CHANGE_EVENT_PREFIX}${eventId}`
}

/** The per-draft detail: the company is navigation context — the draft reads
 * are id-scoped (`GET /drafts/{id}`), and the path says where the reader came
 * from. */
export function hrefForDraft(companyId: string, draftId: string): string {
  return `${COMPANY_PREFIX}${companyId}${DRAFT_PREFIX}${draftId}`
}
