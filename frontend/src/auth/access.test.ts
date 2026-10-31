import { describe, expect, it } from 'vitest'
import type { SessionState } from '../api/auth'
import { accessOf, isAdmin, pathFor, withReturnTo } from './access'

function state(overrides: Partial<SessionState> = {}, mfa = true): SessionState {
  return {
    authenticated: true,
    user: { id: 'u1', email: 'ann@example.test', role: 'user' },
    session: { expiresAt: '2030-01-01T00:00:00Z', recentAuth: true, mfa, stepup: mfa },
    totpEnabled: true,
    ...overrides,
  }
}

describe('accessOf', () => {
  it('is anonymous before the session is known and for a signed-out visitor', () => {
    expect(accessOf(null)).toBe('anonymous')
    expect(accessOf({ authenticated: false, user: null, session: null, totpEnabled: false })).toBe('anonymous')
  })

  it('asks for enrolment while the account has no authenticator app, whatever the session says', () => {
    expect(accessOf(state({ totpEnabled: false }))).toBe('enrol')
    expect(accessOf(state({ totpEnabled: false }, false))).toBe('enrol')
  })

  it('asks for a code while the session has not passed the second factor', () => {
    expect(accessOf(state({}, false))).toBe('challenge')
  })

  it('is ready only with an authenticator app and a passed second factor', () => {
    expect(accessOf(state())).toBe('ready')
  })
})

describe('pathFor and withReturnTo', () => {
  it('names the page of each step', () => {
    expect(pathFor('anonymous')).toBe('/signin')
    expect(pathFor('enrol')).toBe('/two-factor/setup')
    expect(pathFor('challenge')).toBe('/two-factor')
    expect(pathFor('ready')).toBe('/')
  })

  it('adds an encoded return_to, and none for the home page or nothing', () => {
    expect(withReturnTo('/signin', '/a?b=1#c')).toBe('/signin?return_to=%2Fa%3Fb%3D1%23c')
    expect(withReturnTo('/signin', '/')).toBe('/signin')
    expect(withReturnTo('/signin', null)).toBe('/signin')
  })
})

describe('isAdmin', () => {
  const as = (role: string) => state({ user: { id: 'u1', email: 'ann@example.test', role } })

  it('is true for the admin role only', () => {
    expect(isAdmin(as('admin'))).toBe(true)
    expect(isAdmin(as('user'))).toBe(false)
    expect(isAdmin(as('Admin'))).toBe(false)
    expect(isAdmin(as(''))).toBe(false)
  })

  it('is false before the session is known, for a signed-out visitor and for a session with no user', () => {
    expect(isAdmin(null)).toBe(false)
    expect(isAdmin({ authenticated: false, user: null, session: null, totpEnabled: false })).toBe(false)
    expect(isAdmin(state({ user: null }))).toBe(false)
  })

  it('does not depend on the second factor: it only says what to show, and the guards say who may see it', () => {
    expect(isAdmin(state({ ...as('admin') }, false))).toBe(true)
  })
})
