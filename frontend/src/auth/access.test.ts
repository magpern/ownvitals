import { describe, expect, it } from 'vitest'
import type { SessionState } from '../api/auth'
import { accessOf, isAdmin, mayAddTotp, pathFor, withReturnTo } from './access'

const SIGNED_OUT: SessionState = {
  authenticated: false,
  user: null,
  session: null,
  totpEnabled: false,
  totpRequired: false,
  turnstile: null,
}

function state(overrides: Partial<SessionState> = {}, mfa = true): SessionState {
  return {
    authenticated: true,
    user: { id: 'u1', email: 'ann@example.test', role: 'user', locale: null },
    session: { id: 's1', expiresAt: '2030-01-01T00:00:00Z', recentAuth: true, mfa, stepup: mfa },
    totpEnabled: true,
    totpRequired: false,
    turnstile: null,
    ...overrides,
  }
}

describe('accessOf', () => {
  it('is anonymous before the session is known and for a signed-out visitor', () => {
    expect(accessOf(null)).toBe('anonymous')
    expect(accessOf(SIGNED_OUT)).toBe('anonymous')
  })

  it('asks an account that must have an authenticator app and has none to enrol', () => {
    expect(accessOf(state({ totpEnabled: false, totpRequired: true }, false))).toBe('enrol')
  })

  it('lets a user without an authenticator app in once the server says the session owes nothing (issue 475)', () => {
    expect(accessOf(state({ totpEnabled: false }))).toBe('ready')
  })

  it('asks for a code while the session has not passed the second factor', () => {
    expect(accessOf(state({}, false))).toBe('challenge')
  })

  it('is ready only with an authenticator app and a passed second factor', () => {
    expect(accessOf(state())).toBe('ready')
  })
})

describe('mayAddTotp', () => {
  it('is true only for a ready user without an authenticator app', () => {
    expect(mayAddTotp(state({ totpEnabled: false }))).toBe(true)
    expect(mayAddTotp(state())).toBe(false)
    expect(mayAddTotp(state({ totpEnabled: false, totpRequired: true }, false))).toBe(false)
    expect(mayAddTotp(state({ totpEnabled: false, totpRequired: true }))).toBe(false)
    expect(mayAddTotp(SIGNED_OUT)).toBe(false)
    expect(mayAddTotp(null)).toBe(false)
  })
})

describe('pathFor and withReturnTo', () => {
  it('names the page of each step', () => {
    expect(pathFor('anonymous')).toBe('/signin')
    expect(pathFor('enrol')).toBe('/two-factor/setup')
    expect(pathFor('challenge')).toBe('/two-factor')
    expect(pathFor('ready')).toBe('/overview')
  })

  it('adds an encoded return_to, and none for the home page or nothing', () => {
    expect(withReturnTo('/signin', '/a?b=1#c')).toBe('/signin?return_to=%2Fa%3Fb%3D1%23c')
    expect(withReturnTo('/signin', '/overview')).toBe('/signin')
    expect(withReturnTo('/signin', null)).toBe('/signin')
  })
})

describe('isAdmin', () => {
  const as = (role: string) => state({ user: { id: 'u1', email: 'ann@example.test', role, locale: null } })

  it('is true for the admin role only', () => {
    expect(isAdmin(as('admin'))).toBe(true)
    expect(isAdmin(as('user'))).toBe(false)
    expect(isAdmin(as('Admin'))).toBe(false)
    expect(isAdmin(as(''))).toBe(false)
  })

  it('is false before the session is known, for a signed-out visitor and for a session with no user', () => {
    expect(isAdmin(null)).toBe(false)
    expect(isAdmin(SIGNED_OUT)).toBe(false)
    expect(isAdmin(state({ user: null }))).toBe(false)
  })

  it('does not depend on the second factor: it only says what to show, and the guards say who may see it', () => {
    expect(isAdmin(state({ ...as('admin') }, false))).toBe(true)
  })
})
