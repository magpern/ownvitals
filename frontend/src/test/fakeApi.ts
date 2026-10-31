import { vi } from 'vitest'
import { rememberCsrfToken } from '../api/client'

// Everything here is synthetic: no network, no real account, no real health data.

export type Handler = (body: unknown, headers: Headers) => unknown
export interface Call {
  method: string
  path: string
  body: unknown
  headers: Headers
}

export function json(body: unknown, status = 200, headers: Record<string, string> = {}): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json', ...headers },
  })
}

export function reason(status: number, why: string): Response {
  return json({ detail: { reason: why } }, status)
}

export const ANONYMOUS = { authenticated: false, csrf_token: 'csrf-anon' }

/** Signed in, and the session has passed its second factor. */
export const SIGNED_IN = {
  authenticated: true,
  csrf_token: 'csrf-session',
  user: { id: 'u1', email: 'ann@example.test', role: 'user' },
  session: { id: 's1', expires_at: '2030-01-01T00:00:00Z', recent_auth: true, mfa: true, stepup: true },
}

/** Signed in, but this session has not passed its second factor yet. */
export const SIGNED_IN_NO_CODE = {
  ...SIGNED_IN,
  session: { ...SIGNED_IN.session, mfa: false, stepup: false },
}

export const MFA_ON = { totp_enabled: true, recovery_codes_left: 8 }
export const MFA_OFF = { totp_enabled: false, recovery_codes_left: 0 }

/** A QR code as the server draws it (a made-up payload, not a real secret). */
export const QR_SVG =
  '<svg xmlns="http://www.w3.org/2000/svg" width="8" height="8"><path d="M0 0h8v8H0z" fill="#fff"/><path d="M1 1h2v2H1z" fill="#000"/></svg>'

/**
 * Stubs `fetch` with a table of `"METHOD /path"` handlers. A handler returns a Response or any
 * JSON value (answered 200); a missing route fails the test loudly. By default the session is
 * anonymous, the account has two-factor on and no OAuth provider is enabled.
 */
export function fakeApi(routes: Record<string, Handler | object | string>) {
  rememberCsrfToken(null)
  const calls: Call[] = []
  const table: Record<string, Handler | object | string> = {
    'GET /api/auth/session': ANONYMOUS,
    'GET /api/auth/mfa': MFA_ON,
    'GET /api/auth/oauth/providers': { providers: [] },
    ...routes,
  }
  const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const path = typeof input === 'string' ? input : input instanceof URL ? input.pathname : input.url
    const method = init?.method ?? 'GET'
    const headers = new Headers(init?.headers)
    const body = typeof init?.body === 'string' ? (JSON.parse(init.body) as unknown) : undefined
    calls.push({ method, path, body, headers })
    const route = table[`${method} ${path}`]
    if (route === undefined) {
      return Promise.reject(new Error(`unexpected request: ${method} ${path}`))
    }
    const answer = typeof route === 'function' ? (route as Handler)(body, headers) : route
    return Promise.resolve(answer instanceof Response ? answer.clone() : json(answer))
  })
  vi.stubGlobal('fetch', fetchMock)
  return {
    calls,
    to: (method: string, path: string) =>
      calls.filter((call) => call.method === method && call.path === path),
  }
}
