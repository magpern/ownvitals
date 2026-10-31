import { describe, expect, it } from 'vitest'
import { MFA_OFF, MFA_ON, SIGNED_IN, SIGNED_IN_NO_CODE, fakeApi, reason } from '../test/fakeApi'
import { DecodeError } from './decode'
import { confirmTotp, getPendingSignup, getSession, startOAuth, startTotp } from './auth'

describe('getSession', () => {
  it('reads a signed-out answer without asking about two-factor', async () => {
    const api = fakeApi({})
    expect(await getSession()).toEqual({ authenticated: false, user: null, session: null, totpEnabled: false })
    expect(api.to('GET', '/api/auth/mfa')).toHaveLength(0)
  })

  it('reads the user, the session flags and whether the account has TOTP', async () => {
    fakeApi({ 'GET /api/auth/session': SIGNED_IN_NO_CODE, 'GET /api/auth/mfa': MFA_ON })
    expect(await getSession()).toEqual({
      authenticated: true,
      user: { id: 'u1', email: 'ann@example.test', role: 'user' },
      session: { expiresAt: '2030-01-01T00:00:00Z', recentAuth: true, mfa: false, stepup: false },
      totpEnabled: true,
    })
  })

  it('knows an account without TOTP', async () => {
    fakeApi({ 'GET /api/auth/session': SIGNED_IN, 'GET /api/auth/mfa': MFA_OFF })
    expect((await getSession()).totpEnabled).toBe(false)
  })

  it('treats two_factor_required on the status call as no TOTP, and any other refusal as an error', async () => {
    fakeApi({ 'GET /api/auth/session': SIGNED_IN, 'GET /api/auth/mfa': reason(403, 'two_factor_required') })
    expect((await getSession()).totpEnabled).toBe(false)
    fakeApi({ 'GET /api/auth/session': SIGNED_IN, 'GET /api/auth/mfa': reason(403, 'forbidden') })
    await expect(getSession()).rejects.toMatchObject({ status: 403, reason: 'forbidden' })
    fakeApi({ 'GET /api/auth/session': SIGNED_IN, 'GET /api/auth/mfa': reason(500, 'boom') })
    await expect(getSession()).rejects.toMatchObject({ status: 500 })
  })

  it('refuses an answer that is not the shape the pages rely on', async () => {
    fakeApi({ 'GET /api/auth/session': { authenticated: true, csrf_token: 'x' } })
    await expect(getSession()).rejects.toBeInstanceOf(DecodeError)
    fakeApi({ 'GET /api/auth/session': { ...SIGNED_IN, session: { ...SIGNED_IN.session, mfa: 'yes' } } })
    await expect(getSession()).rejects.toBeInstanceOf(DecodeError)
    fakeApi({ 'GET /api/auth/session': SIGNED_IN, 'GET /api/auth/mfa': { totp_enabled: 1 } })
    await expect(getSession()).rejects.toBeInstanceOf(DecodeError)
  })
})

describe('the two-factor calls', () => {
  it('reads an enrolment, and refuses one without a QR code', async () => {
    fakeApi({
      'POST /api/auth/mfa/totp/start': { secret: 'KEY', qr_svg: '<svg/>', pending: 'p', otpauth_uri: 'otpauth://x' },
    })
    expect(await startTotp()).toEqual({ secret: 'KEY', qrSvg: '<svg/>', pending: 'p' })
    fakeApi({ 'POST /api/auth/mfa/totp/start': { secret: 'KEY', pending: 'p' } })
    await expect(startTotp()).rejects.toBeInstanceOf(DecodeError)
  })

  it('returns the recovery codes of a confirmation, and refuses a malformed list', async () => {
    fakeApi({ 'POST /api/auth/mfa/totp/confirm': { recovery_codes: ['a', 'b'] } })
    expect(await confirmTotp('p', '123456')).toEqual(['a', 'b'])
    fakeApi({ 'POST /api/auth/mfa/totp/confirm': { recovery_codes: ['a', 2] } })
    await expect(confirmTotp('p', '123456')).rejects.toBeInstanceOf(DecodeError)
  })
})

describe('the OAuth calls', () => {
  it('starts a sign-in for a provider named in the path, encoded', async () => {
    const api = fakeApi({ 'POST /api/auth/oauth/a%2Fb/start': { authorization_url: 'https://idp.example.test/auth' } })
    expect(await startOAuth('a/b')).toBe('https://idp.example.test/auth')
    expect(api.calls.at(-1)?.body).toEqual({ purpose: 'sign_in', invite_code: null, return_to: null })
  })

  it('reads a pending sign-up, with the optional fields missing', async () => {
    fakeApi({ 'GET /api/auth/oauth/pending': { pending: false } })
    expect(await getPendingSignup()).toEqual({ pending: false, provider: null, awaitingEmail: false })
  })
})
