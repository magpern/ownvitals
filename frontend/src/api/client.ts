import { object, type JsonObject } from './decode'

/** An error answer from the API: HTTP status plus the machine-readable `detail.reason`. */
export class ApiError extends Error {
  readonly status: number
  readonly reason: string | null
  /** Seconds from `Retry-After` (429 and 503), when the server sent one. */
  readonly retryAfter: number | null
  readonly body: unknown

  constructor(status: number, reason: string | null, retryAfter: number | null, body: unknown) {
    super(reason ?? `Request failed (${String(status)})`)
    this.name = 'ApiError'
    this.status = status
    this.reason = reason
    this.retryAfter = retryAfter
    this.body = body
  }
}

const SAFE_METHODS = new Set(['GET', 'HEAD', 'OPTIONS'])
const CSRF_HEADER = 'X-CSRF-Token'

/** The refusal reason of a session that has not completed two-factor authentication. */
export const TWO_FACTOR_REQUIRED = 'two_factor_required'

let onTwoFactorRequired: (() => void) | null = null

/**
 * Registers what runs when any API call is refused with `two_factor_required` (the session lost
 * or never had its second factor): the session provider reads the session again, and the route
 * guards send the user to enrolment or the code prompt. Returns the function that unregisters.
 */
export function setTwoFactorRequiredHandler(handler: (() => void) | null): () => void {
  onTwoFactorRequired = handler
  return () => {
    if (onTwoFactorRequired === handler) onTwoFactorRequired = null
  }
}

// The token that applies to the next unsafe request: the HMAC of the session id, or the
// anonymous cookie's value. It changes whenever the session rotates, so it
// is dropped after every successful unsafe request and fetched again on demand.
let csrfToken: string | null = null

export function rememberCsrfToken(token: string | null): void {
  csrfToken = token
}

async function readBody(response: Response): Promise<unknown> {
  const raw = await response.text()
  if (raw === '') return undefined
  try {
    return JSON.parse(raw) as unknown
  } catch {
    return undefined
  }
}

function reasonOf(body: unknown): string | null {
  if (typeof body !== 'object' || body === null || !('detail' in body)) return null
  const detail = (body as JsonObject).detail
  if (typeof detail === 'object' && detail !== null && 'reason' in detail) {
    const reason = (detail as JsonObject).reason
    return typeof reason === 'string' ? reason : null
  }
  return null
}

function retryAfterOf(response: Response): number | null {
  const value = Number(response.headers.get('Retry-After'))
  return Number.isFinite(value) && value > 0 ? value : null
}

/** GET /api/auth/session raw; also refreshes the remembered CSRF token. */
export async function fetchSessionJson(signal?: AbortSignal): Promise<JsonObject> {
  const response = await fetch('/api/auth/session', {
    headers: { Accept: 'application/json' },
    signal,
  })
  const body = await readBody(response)
  if (!response.ok) throw new ApiError(response.status, reasonOf(body), null, body)
  const session = object(body, 'session')
  csrfToken = typeof session.csrf_token === 'string' ? session.csrf_token : null
  return session
}

async function currentCsrfToken(): Promise<string> {
  if (csrfToken === null) await fetchSessionJson()
  if (csrfToken === null) throw new ApiError(500, 'no_csrf_token', null, undefined)
  return csrfToken
}

export interface RequestOptions {
  body?: unknown
  /** Extra request headers (an `Idempotency-Key`); never CSRF or content type. */
  headers?: Record<string, string>
  signal?: AbortSignal
}

/**
 * One API call. Unsafe methods carry `X-CSRF-Token`; a 403 that blames the token is retried
 * once with a fresh one (the session may have rotated). A 401 (a request in flight while the
 * session rotated) is repeated once after refetching the session, for safe
 * methods only: some 401s of unsafe requests come after the server acted (second-factor routes,
 * /reauth), and an unsafe request across a rotation gets the CSRF 403 handled above. Returns the parsed JSON, or `undefined` for an
 * empty answer. Anything but 2xx throws an `ApiError`.
 */
export async function request(
  method: string,
  path: string,
  options: RequestOptions = {},
): Promise<unknown> {
  const unsafe = !SAFE_METHODS.has(method)
  let csrfRetried = false
  let sessionRetried = false
  for (;;) {
    const headers: Record<string, string> = { Accept: 'application/json', ...options.headers }
    if (options.body !== undefined) headers['Content-Type'] = 'application/json'
    if (unsafe) headers[CSRF_HEADER] = await currentCsrfToken()
    const response = await fetch(path, {
      method,
      headers,
      body: options.body === undefined ? undefined : JSON.stringify(options.body),
      signal: options.signal,
    })
    const body = await readBody(response)
    if (response.ok) {
      if (unsafe) csrfToken = null
      return body
    }
    const reason = reasonOf(body)
    if (unsafe && !csrfRetried && response.status === 403 && reason?.startsWith('csrf_token')) {
      csrfRetried = true
      csrfToken = null
      continue
    }
    if (
      response.status === 401 &&
      !unsafe &&
      !sessionRetried &&
      !options.signal?.aborted
    ) {
      sessionRetried = true
      try {
        await fetchSessionJson(options.signal)
        continue
      } catch {
        // The session cannot be read: report the original 401.
      }
    }
    if (response.status === 403 && reason === TWO_FACTOR_REQUIRED) onTwoFactorRequired?.()
    throw new ApiError(response.status, reason, retryAfterOf(response), body)
  }
}

export function get(path: string, signal?: AbortSignal): Promise<unknown> {
  return request('GET', path, { signal })
}

export function post(
  path: string,
  body?: unknown,
  headers?: Record<string, string>,
): Promise<unknown> {
  return request('POST', path, { body: body ?? {}, headers })
}

export function del(path: string): Promise<unknown> {
  return request('DELETE', path)
}
