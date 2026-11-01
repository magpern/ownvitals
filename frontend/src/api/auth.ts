import { ApiError, del, fetchSessionJson, get, post } from './client'
import { DecodeError, count, flag, list, object, optionalText, strings, text } from './decode'

// The auth endpoints (backend/src/ownvitals/auth/*_routes.py, ported from classbeat). Every
// answer is decoded and checked here, so pages only see typed values.

export interface SessionUser {
  id: string
  email: string
  role: string
}

export interface SessionDetails {
  id: string
  expiresAt: string
  recentAuth: boolean
  /** The session passed its second factor (TOTP is mandatory, ADR 0003 section 2). */
  mfa: boolean
  stepup: boolean
}

export interface SessionState {
  authenticated: boolean
  user: SessionUser | null
  session: SessionDetails | null
  /** The account has an authenticator app set up. Always false when signed out. */
  totpEnabled: boolean
}

export interface MfaStatus {
  totpEnabled: boolean
  recoveryCodesLeft: number
}

export async function getMfaStatus(signal?: AbortSignal): Promise<MfaStatus> {
  const body = object(await get('/api/auth/mfa', signal), 'mfa')
  return {
    totpEnabled: flag(body, 'totp_enabled'),
    recoveryCodesLeft: count(body, 'recovery_codes_left'),
  }
}

export async function getSession(signal?: AbortSignal): Promise<SessionState> {
  const body = await fetchSessionJson(signal)
  const authenticated = flag(body, 'authenticated')
  if (!authenticated) return { authenticated, user: null, session: null, totpEnabled: false }
  const user = object(body.user, 'user')
  const session = object(body.session, 'session')
  return {
    authenticated,
    user: { id: text(user, 'id'), email: text(user, 'email'), role: text(user, 'role') },
    session: {
      id: text(session, 'id'),
      expiresAt: text(session, 'expires_at'),
      recentAuth: flag(session, 'recent_auth'),
      mfa: flag(session, 'mfa'),
      stepup: flag(session, 'stepup'),
    },
    // In the session answer itself (#11), so a session without its second factor, which the
    // server refuses everything else, is routed without another call.
    totpEnabled: flag(session, 'totp_enabled'),
  }
}

// --- sign-up, sign-in, sign-out ----------------------------------------------------------

export async function signUp(input: {
  email: string
  password: string
  inviteCode: string
}): Promise<void> {
  await post('/api/auth/signup', {
    email: input.email,
    password: input.password,
    invite_code: input.inviteCode === '' ? null : input.inviteCode,
  })
}

export async function confirmSignUp(token: string, password: string | null): Promise<void> {
  await post('/api/auth/signup/confirm', { token, password })
}

export async function login(email: string, password: string): Promise<void> {
  await post('/api/auth/login', { email, password })
}

export async function logout(): Promise<void> {
  await post('/api/auth/logout')
}

// --- password reset ----------------------------------------------------------------------

export async function requestPasswordReset(email: string): Promise<void> {
  await post('/api/auth/password/reset/request', { email })
}

export interface LinkedMethod {
  id: string
  provider: string
}

export async function resetInfo(token: string): Promise<LinkedMethod[]> {
  const body = object(await post('/api/auth/password/reset/info', { token }), 'reset info')
  return list(body, 'methods').map((row) => {
    const item = object(row, 'method')
    return { id: text(item, 'id'), provider: text(item, 'provider') }
  })
}

export async function completeReset(
  token: string,
  password: string,
  removeMethods: string[],
): Promise<void> {
  await post('/api/auth/password/reset/complete', {
    token,
    password,
    remove_methods: removeMethods,
  })
}

// --- OAuth (Google is the only provider) -------------------------------------------------

export async function getOAuthProviders(signal?: AbortSignal): Promise<string[]> {
  const body = object(await get('/api/auth/oauth/providers', signal), 'providers')
  return strings(body, 'providers')
}

/** The provider's authorization URL for a sign-in; the caller sends the browser there. */
export async function startOAuth(
  provider: string,
  extra: { inviteCode?: string; returnTo?: string } = {},
): Promise<string> {
  const body = object(
    await post(`/api/auth/oauth/${encodeURIComponent(provider)}/start`, {
      purpose: 'sign_in',
      invite_code: extra.inviteCode ? extra.inviteCode : null,
      return_to: extra.returnTo ?? null,
    }),
    'oauth start',
  )
  return text(body, 'authorization_url')
}

export interface PendingSignup {
  pending: boolean
  provider: string | null
  awaitingEmail: boolean
}

export async function getPendingSignup(signal?: AbortSignal): Promise<PendingSignup> {
  const body = object(await get('/api/auth/oauth/pending', signal), 'pending')
  return {
    pending: flag(body, 'pending'),
    provider: optionalText(body, 'provider'),
    awaitingEmail: body.awaiting_email === true,
  }
}

export async function submitPendingEmail(email: string): Promise<void> {
  await post('/api/auth/oauth/pending/email', { email })
}

// --- two-factor (TOTP) -------------------------------------------------------------------

export interface Enrollment {
  /** The key to type by hand (base32). */
  secret: string
  /** The server-drawn QR code as an SVG document. */
  qrSvg: string
  /** Sent back with the first code to confirm the enrolment. */
  pending: string
}

export async function startTotp(): Promise<Enrollment> {
  const body = object(await post('/api/auth/mfa/totp/start'), 'enrollment')
  return {
    secret: text(body, 'secret'),
    qrSvg: text(body, 'qr_svg'),
    pending: text(body, 'pending'),
  }
}

export interface Confirmed {
  /** Shown once: the server never shows them again. */
  recoveryCodes: string[]
  /**
   * The enrolment is saved, but the session was signed out meanwhile (401): the codes still
   * came back, and the user signs in again after saving them.
   */
  signedOut: boolean
}

/** Confirms the enrolment with the first code; the recovery codes come back once. */
export async function confirmTotp(pending: string, code: string): Promise<Confirmed> {
  try {
    const body = object(await post('/api/auth/mfa/totp/confirm', { pending, code }), 'confirm')
    return { recoveryCodes: strings(body, 'recovery_codes'), signedOut: false }
  } catch (error) {
    // A 401 that still carries the codes: the enrolment went through. Never lose them.
    if (error instanceof ApiError && error.status === 401) {
      const body = error.body
      if (typeof body === 'object' && body !== null && !Array.isArray(body)) {
        const codes = (body as Record<string, unknown>).recovery_codes
        if (Array.isArray(codes) && codes.length > 0 && codes.every((c) => typeof c === 'string')) {
          return { recoveryCodes: codes, signedOut: true }
        }
      }
    }
    throw error
  }
}

/** The second step after sign-in: a TOTP or recovery code. */
export async function verifyMfa(code: string): Promise<void> {
  await post('/api/auth/mfa/verify', { code })
}

/** Step-up: a TOTP or recovery code, good for 10 minutes of protected actions. */
export async function stepUp(code: string): Promise<void> {
  await post('/api/auth/mfa/stepup', { code })
}

/** Re-authentication with the password, for the actions that also need it. */
export async function reauthenticate(password: string): Promise<void> {
  await post('/api/auth/reauth', { password })
}

/** The authorization URL of a Google re-authentication; the browser comes back to `returnTo`. */
export async function startOAuthReauth(provider: string, returnTo: string): Promise<string> {
  const body = object(
    await post(`/api/auth/oauth/${encodeURIComponent(provider)}/start`, {
      purpose: 'reauth',
      invite_code: null,
      return_to: returnTo,
    }),
    'oauth start',
  )
  return text(body, 'authorization_url')
}

/** The providers linked to the signed-in account (`google`), and whether it has a password. */
export async function getLinkedProviders(signal?: AbortSignal): Promise<string[]> {
  const body = object(await get('/api/auth/identities', signal), 'identities')
  return list(body, 'identities').map((row) => text(object(row, 'identity'), 'provider'))
}

// --- sessions ------------------------------------------------------------------------------

export interface SessionItem {
  id: string
  createdAt: string
  lastSeenAt: string
  userAgent: string | null
  ip: string | null
  current: boolean
}

export async function listSessions(signal?: AbortSignal): Promise<SessionItem[]> {
  const rows = await get('/api/auth/sessions', signal)
  if (!Array.isArray(rows)) throw new DecodeError('sessions')
  return rows.map((row) => {
    const item = object(row, 'session')
    return {
      id: text(item, 'id'),
      createdAt: text(item, 'created_at'),
      lastSeenAt: text(item, 'last_seen_at'),
      userAgent: optionalText(item, 'user_agent'),
      ip: optionalText(item, 'ip'),
      current: flag(item, 'current'),
    }
  })
}

/** Signs out one of the user's own sessions. */
export async function revokeSession(id: string): Promise<void> {
  await del(`/api/auth/sessions/${encodeURIComponent(id)}`)
}

/** Signs out every session of the user, this one included (step-up first). */
export async function logoutEverywhere(): Promise<void> {
  await post('/api/auth/logout-all')
}
