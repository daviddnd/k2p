/**
 * Load-and-retry state for the two read views: loading / ready / error, with
 * a `reload` that re-runs the loader (the retry affordance).
 *
 * `key` identifies the read (`'dashboard'`, or the calendar range): when it
 * changes, the loader re-runs. The last good payload is kept on screen while
 * a reload is in flight, so pressing Refresh or applying a new calendar range
 * does not blank the view, and a stale response never overwrites a newer one
 * (the effect's cancellation flag).
 *
 * Status is derived at render time from "which read has settled", so nothing
 * sets state synchronously inside the effect.
 */

import { useCallback, useEffect, useRef, useState } from 'react'

export interface Resource<T> {
  status: 'loading' | 'ready' | 'error'
  /** The last payload for this key; null until the first one arrives. */
  data: T | null
  error: unknown
}

export function useResource<T>(
  load: () => Promise<T>,
  key: string,
): [Resource<T>, () => void] {
  const [attempt, setAttempt] = useState(0)
  const [settled, setSettled] = useState<Settled<T> | null>(null)

  const loadRef = useRef(load)
  useEffect(() => {
    loadRef.current = load
  })

  useEffect(() => {
    let cancelled = false
    loadRef.current().then(
      (data) => {
        if (!cancelled) setSettled({ key, attempt, data, error: null })
      },
      (error: unknown) => {
        if (!cancelled) setSettled({ key, attempt, data: null, error })
      },
    )
    return () => {
      cancelled = true
    }
  }, [key, attempt])

  const reload = useCallback(() => setAttempt((count) => count + 1), [])
  return [resourceFor(settled, key, attempt), reload]
}

interface Settled<T> {
  key: string
  attempt: number
  data: T | null
  error: unknown
}

function resourceFor<T>(settled: Settled<T> | null, key: string, attempt: number): Resource<T> {
  if (settled === null || settled.key !== key || settled.attempt !== attempt) {
    // A read is in flight; keep the previous payload for the same key visible.
    return { status: 'loading', data: settled?.key === key ? settled.data : null, error: null }
  }
  if (settled.error !== null) return { status: 'error', data: null, error: settled.error }
  return { status: 'ready', data: settled.data, error: null }
}
