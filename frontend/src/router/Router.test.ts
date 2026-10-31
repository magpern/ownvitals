import { describe, expect, it } from 'vitest'
import { safeReturnTo } from './Router'

describe('safeReturnTo', () => {
  it('keeps relative paths with query and fragment', () => {
    expect(safeReturnTo('/account')).toBe('/account')
    expect(safeReturnTo('/account?a=1&b=2#x')).toBe('/account?a=1&b=2#x')
    expect(safeReturnTo('/a%20b')).toBe('/a%20b')
  })

  it.each([
    null,
    '',
    'account',
    'https://evil.example/',
    'HTTP://evil.example',
    '//evil.example',
    '///evil.example',
    '/\\evil.example',
    '/\\/evil.example',
    '/ok\\path',
    'javascript:alert(1)',
    '/redirect?to=https://evil.example',
    '/a\nb',
    '/a\tb',
    '/a\u007fb',
    '/%2F%2Fevil.example',
    '/%5Cevil.example',
    '/%0d%0aSet-Cookie:x',
    '/%09x',
    '/%E0%A4%A',
  ])('refuses %j', (value) => {
    expect(safeReturnTo(value)).toBeNull()
  })
})
