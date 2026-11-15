import { describe, expect, it } from 'vitest'
import { decodeAuthorizeUrl, decodeWithings } from './connections'

const row = (over: object = {}) => ({
  id: 'c1',
  provider: 'withings',
  status: 'connected',
  last_error: null,
  connected_at: '2026-10-20T08:00:00Z',
  last_used_at: null,
  ...over,
})

describe('decodeWithings', () => {
  it('returns the live Withings row', () => {
    expect(decodeWithings({ connections: [row()] })?.status).toBe('connected')
  })

  it('treats no row, a disconnected row and another provider as not connected', () => {
    expect(decodeWithings({ connections: [] })).toBeNull()
    expect(decodeWithings({ connections: [row({ status: 'disconnected' })] })).toBeNull()
    expect(decodeWithings({ connections: [row({ provider: 'other' })] })).toBeNull()
  })

  it('rejects an unknown status', () => {
    expect(() => decodeWithings({ connections: [row({ status: 'weird' })] })).toThrow()
  })
})

describe('decodeAuthorizeUrl', () => {
  it('accepts https and refuses anything else', () => {
    expect(decodeAuthorizeUrl({ authorize_url: 'https://account.withings.example/a' })).toContain('https://')
    expect(() => decodeAuthorizeUrl({ authorize_url: 'http://x.example/' })).toThrow()
    expect(() => decodeAuthorizeUrl({ authorize_url: '/relative' })).toThrow()
    expect(() => decodeAuthorizeUrl({ authorize_url: 'javascript:alert(1)' })).toThrow()
  })
})
