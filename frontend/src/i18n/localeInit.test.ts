import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { ROOT } from '../test/sourceScan'
import { LOCALES } from './locale'

// public/locale-init.js sets <html lang> before the first paint (never an inline script: the CSP
// is script-src 'self'). It cannot import the manifest, so this test keeps the two in step and runs
// the file the way the browser does.

const SOURCE = readFileSync(join(ROOT, 'public/locale-init.js'), 'utf8')

function run(stored: string | null, languages: string[]): string | null {
  document.documentElement.removeAttribute('lang')
  window.localStorage.clear()
  if (stored !== null) window.localStorage.setItem('ov-locale', stored)
  vi.stubGlobal('navigator', { languages })
  window.eval(SOURCE)
  return document.documentElement.getAttribute('lang')
}

beforeEach(() => {
  window.localStorage.clear()
})

describe('public/locale-init.js', () => {
  it('lists the languages of the manifest', () => {
    const listed = /var SUPPORTED = (\[[^\]]*\])/.exec(SOURCE)?.[1] ?? '[]'
    expect(JSON.parse(listed.replace(/'/g, '"')) as string[]).toEqual([...LOCALES])
  })

  it.each([
    [null, ['sv-SE', 'en'], 'sv'],
    [null, ['de', 'en-US'], 'en'],
    [null, ['de'], 'en'],
    [null, [], 'en'],
    ['sv', ['en'], 'sv'],
    ['en', ['sv'], 'en'],
  ])('with stored %s and browser %j sets lang %s', (stored, languages, expected) => {
    expect(run(stored, languages)).toBe(expected)
  })

  it('never writes a code outside the manifest', () => {
    expect(run('"><script>', ['en'])).toBe('en')
    expect(run('xx', ['sv'])).toBe('sv')
    expect(run('SV', ['en'])).toBe('en')
  })

  it('survives storage that throws', () => {
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new Error('blocked')
    })
    vi.stubGlobal('navigator', { languages: ['sv'] })
    window.eval(SOURCE)
    expect(document.documentElement.getAttribute('lang')).toBe('sv')
  })
})
