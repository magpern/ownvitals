import { ApiError, TWO_FACTOR_REQUIRED, fetchSessionJson, get, post } from './client'
import { count, flag, list, object, optionalText, strings, text } from './decode'

// The auth endpoints (backend/src/ownvitals/auth/*_routes.py, ported from classbeat). Every
// answer is decoded and checked here, so pages only see typed values.

export interface SessionUser {
  id: string
  email: string
  role: string
}

export interface SessionDetails {
  expiresAt: string
  recentAuth: boolean
  /** The second factor was passed in this session (and its window has not lapsed). */
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

/**
 * Whether the signed-in account has TOTP. A session without a second factor may be refused
 * everything but enrolment (`two_factor_required`): that answer means "not set up".
 */
async function hasTotp(signal?: AbortSignal): Promise<boolean> {
  try {
    return (await getMfaStatus(signal)).totpEnabled
  } catch (error) {
    if (error instanceof ApiError && error.status === 403 && error.reason === TWO_FACTOR_REQUIRED) {
      return false
    }
    throw error
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
      expiresAt: text(session, 'expires_at'),
      recentAuth: flag(session, 'recent_auth'),
      mfa: flag(session, 'mfa'),
      stepup: flag(session, 'stepup'),
    },
    totpEnabled: await hasTotp(signal),
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

/** Confirms the enrolment with the first code; the recovery codes come back once. */
export async function confirmTotp(pending: string, code: string): Promise<string[]> {
  const body = object(await post('/api/auth/mfa/totp/confirm', { pending, code }), 'confirm')
  return strings(body, 'recovery_codes')
}

/** The second step after sign-in: a TOTP or recovery code. */
export async function verifyMfa(code: string): Promise<void> {
  await post('/api/auth/mfa/verify', { code })
}
