import { afterEach, describe, expect, it, vi } from 'vitest'
import { matchLanguage, readStoredLocale, resolveLocale, writeStoredLocale } from './resolve'

describe('resolveLocale', () => {
  it.each([
    ['sv', ['en'], 'sv'],
    [null, ['sv-SE', 'en'], 'sv'],
    [null, ['de-DE', 'sv', 'en'], 'sv'],
    [null, ['de'], 'en'],
    ['xx', ['sv'], 'sv'],
    ['', [], 'en'],
  ])('stored %j and browser %j give %s', (stored, languages, expected) => {
    expect(resolveLocale(stored, languages)).toBe(expected)
  })

  it('matches a language tag by its first part, ignoring case', () => {
    expect(matchLanguage('SV-se')).toBe('sv')
    expect(matchLanguage('en')).toBe('en')
    expect(matchLanguage('svenska')).toBeNull()
    expect(matchLanguage('')).toBeNull()
  })
})

describe('the stored choice', () => {
  afterEach(() => {
    window.localStorage.clear()
  })

  it('is written and read back under ov-locale', () => {
    writeStoredLocale('sv')
    expect(window.localStorage.getItem('ov-locale')).toBe('sv')
    expect(readStoredLocale()).toBe('sv')
  })

  it('is not an error when storage is blocked', () => {
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new Error('blocked')
    })
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new Error('blocked')
    })
    expect(readStoredLocale()).toBeNull()
    expect(() => {
      writeStoredLocale('sv')
    }).not.toThrow()
  })
})
