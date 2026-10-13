/**
 * The one HTTP seam. Every call goes through `request`, which:
 *
 *   - attaches the bearer token when a session is held (`auth: false`
 *     skips it, for the login call itself);
 *   - turns a network failure into an `ApiError` with status 0, so the
 *     views can tell "the server is unreachable" from "the server said no";
 *   - turns a **401 on an authenticated request** into "return to the login
 *     screen" — the single place that happens: the dead token is dropped
 *     from the session store and every subscriber re-renders. A 401 while
 *     signing in is an ordinary error (wrong credentials), not a session
 *     event;
 *   - resolves a **204** as `undefined`: the delete routes answer it, and
 *     parsing the empty body as JSON would be an error about success.
 *
 * A refusal's `detail` is kept as the backend sent it, and a **list** detail —
 * the domain's own violation list (a 422's `milestones.…` fields), or
 * Pydantic's error objects for a malformed body — travels as
 * `ApiError.violations` so a panel can show every violation rather than one
 * joined line. `detail` carries the same list joined, for the renderers that
 * want a single sentence. A `tenant_config_invalid` refusal is the other shape:
 * the API sends it as one joined string, and `renewals.describeRefusal` splits
 * it back into the violation list it was.
 *
 * A body may instead be **raw** (`rawBody`, the signed-draft upload's shape:
 * the file's bytes with its own declared `contentType` — never a JSON
 * wrapper); the 401/204/refusal handling is identical either way.
 *
 * Paths are relative so the same build works behind nginx (which serves
 * `/api/`) and through the Vite dev proxy.
 */

import { expireSession, getSessionToken } from '../session'

export class ApiError extends Error {
  /** HTTP status, or 0 when the request never reached the server. */
  readonly status: number
  readonly detail: string
  /** The backend's listed violations, when it answered a list; else empty. */
  readonly violations: string[]
  /** The server's `Retry-After` hint in seconds, when it sent one (429s). */
  readonly retryAfterSeconds: number | null

  constructor(
    status: number,
    detail: string,
    violations: string[] = [],
    retryAfterSeconds: number | null = null,
  ) {
    super(status === 0 ? detail : `HTTP ${status}: ${detail}`)
    this.name = 'ApiError'
    this.status = status
    this.detail = detail
    this.violations = violations
    this.retryAfterSeconds = retryAfterSeconds
  }
}

export interface RequestOptions {
  method?: 'GET' | 'POST' | 'PATCH' | 'PUT' | 'DELETE'
  body?: unknown
  /**
   * A body that is not JSON: sent as-is with `contentType` as its declared
   * type (the signed-draft upload). When set, `body` must be absent.
   */
  rawBody?: BodyInit
  contentType?: string
  auth?: boolean
}

export async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const token = options.auth === false ? null : getSessionToken()
  const headers: Record<string, string> = { Accept: 'application/json' }
  if (token !== null) headers.Authorization = `Bearer ${token}`
  const raw = options.rawBody !== undefined
  if (raw) headers['Content-Type'] = options.contentType ?? 'application/octet-stream'
  else if (options.body !== undefined) headers['Content-Type'] = 'application/json'

  let response: Response
  try {
    response = await fetch(path, {
      method: options.method ?? 'GET',
      headers,
      body: raw ? options.rawBody : options.body === undefined ? undefined : JSON.stringify(options.body),
    })
  } catch {
    throw new ApiError(0, 'network request failed')
  }

  if (response.status === 401 && token !== null) {
    expireSession()
    throw new ApiError(401, 'session expired')
  }
  if (!response.ok) {
    const refusal = await readRefusal(response)
    // The login throttle's 429 carries a Retry-After hint; keep it so the
    // sign-in screen can say how long to wait instead of "a moment".
    const retryAfter = Number.parseInt(response.headers.get('retry-after') ?? '', 10)
    throw new ApiError(
      response.status,
      refusal.detail,
      refusal.violations,
      Number.isFinite(retryAfter) && retryAfter > 0 ? retryAfter : null,
    )
  }
  // A 204 carries no body, and `json()` on one would throw: the delete routes
  // answer 204, and "it worked" is the whole payload.
  if (response.status === 204) return undefined as T
  return (await response.json()) as T
}

interface Refusal {
  detail: string
  violations: string[]
}

async function readRefusal(response: Response): Promise<Refusal> {
  try {
    const payload = (await response.json()) as { detail?: unknown }
    const violations = detailList(payload.detail)
    if (violations.length > 0) return { detail: violations.join('; '), violations }
    if (typeof payload.detail === 'string') return { detail: payload.detail, violations: [] }
  } catch {
    // Not JSON; the status text below is all there is.
  }
  return { detail: response.statusText || 'request failed', violations: [] }
}

/**
 * The messages in a `detail` list: FastAPI's two shapes. The domain's own
 * violations are strings (`milestones.client_reply_days_before: …`);
 * Pydantic's body errors are objects, and their `msg` reads as a sentence
 * about one field, so the field is prefixed from `loc` (which carries the
 * parameter kind — `body`, `path`, `query` — first).
 */
function detailList(detail: unknown): string[] {
  if (!Array.isArray(detail)) return []
  return detail
    .map((item) => (typeof item === 'string' ? item : pydanticMessage(item)))
    .filter((item): item is string => item !== null)
}

function pydanticMessage(item: unknown): string | null {
  if (typeof item !== 'object' || item === null) return null
  const { msg, loc } = item as { msg?: unknown; loc?: unknown }
  if (typeof msg !== 'string') return null
  const parts = Array.isArray(loc) ? loc.filter((part) => typeof part === 'string') : []
  const field = parts.length > 1 ? parts[parts.length - 1] : null
  return field === null ? msg : `${field}: ${msg}`
}

/** User-facing copy for a failed request; unknown errors get a plain line. */
export function errorText(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 0) {
      return 'Could not reach the server. Check that the backend is running, then retry.'
    }
    if (error.status === 401) return 'Your session has ended. Sign in again.'
    if (error.status === 403) {
      return 'Not permitted: this account is inactive or has no access to this data.'
    }
    return `Request failed (HTTP ${error.status}): ${error.detail}`
  }
  return 'Something went wrong.'
}
