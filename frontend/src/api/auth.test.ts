import { describe, expect, it } from 'vitest'
import { SIGNED_IN, SIGNED_IN_NO_CODE, SIGNED_IN_NO_TOTP, fakeApi, json, reason } from '../test/fakeApi'
import { DecodeError } from './decode'
import { confirmTotp, getPendingSignup, getSession, startOAuth, startTotp } from './auth'

describe('getSession', () => {
  it('reads a signed-out answer without asking about two-factor', async () => {
    const api = fakeApi({})
    expect(await getSession()).toEqual({ authenticated: false, user: null, session: null, totpEnabled: false })
    expect(api.to('GET', '/api/auth/mfa')).toHaveLength(0)
  })

  it('reads the user, the session flags and whether the account has TOTP from the one answer', async () => {
    const api = fakeApi({ 'GET /api/auth/session': SIGNED_IN_NO_CODE })
    expect(await getSession()).toEqual({
      authenticated: true,
      user: { id: 'u1', email: 'ann@example.test', role: 'user' },
      session: { id: 's1', expiresAt: '2030-01-01T00:00:00Z', recentAuth: true, mfa: false, stepup: false },
      totpEnabled: true,
    })
    // #11: `totp_enabled` is in the session answer; no second call that the gate would refuse.
    expect(api.to('GET', '/api/auth/mfa')).toHaveLength(0)
  })

  it('knows an account without TOTP', async () => {
    fakeApi({ 'GET /api/auth/session': SIGNED_IN_NO_TOTP })
    expect((await getSession()).totpEnabled).toBe(false)
  })

  it('refuses an answer that is not the shape the pages rely on', async () => {
    fakeApi({ 'GET /api/auth/session': { authenticated: true, csrf_token: 'x' } })
    await expect(getSession()).rejects.toBeInstanceOf(DecodeError)
    fakeApi({ 'GET /api/auth/session': { ...SIGNED_IN, session: { ...SIGNED_IN.session, mfa: 'yes' } } })
    await expect(getSession()).rejects.toBeInstanceOf(DecodeError)
    const withoutTotp: Record<string, unknown> = { ...SIGNED_IN.session }
    delete withoutTotp.totp_enabled
    fakeApi({ 'GET /api/auth/session': { ...SIGNED_IN, session: withoutTotp } })
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
    expect(await confirmTotp('p', '123456')).toEqual({ recoveryCodes: ['a', 'b'], signedOut: false })
    fakeApi({ 'POST /api/auth/mfa/totp/confirm': { recovery_codes: ['a', 2] } })
    await expect(confirmTotp('p', '123456')).rejects.toBeInstanceOf(DecodeError)
  })

  it('keeps the recovery codes of a confirmation answered 401, and fails a bare 401', async () => {
    const lost = { detail: { reason: 'not_authenticated' }, enrolled: true, recovery_codes: ['a', 'b'] }
    fakeApi({ 'POST /api/auth/mfa/totp/confirm': json(lost, 401) })
    expect(await confirmTotp('p', '123456')).toEqual({ recoveryCodes: ['a', 'b'], signedOut: true })
    fakeApi({ 'POST /api/auth/mfa/totp/confirm': reason(401, 'not_authenticated') })
    await expect(confirmTotp('p', '123456')).rejects.toMatchObject({ status: 401 })
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
