import { describe, expect, it } from 'vitest'
import { EN_AREAS } from './en'
import { INTL_TAG, LOCALES, type Locale } from './locale'
import { pluralCategories } from './translate'
import { SV_AREAS } from './sv'
import { UNTRANSLATED } from './untranslated'

// The catalogue guards (ADR 0015 section 9). Types already fail on a missing or extra key
// (`satisfies`); these tests keep the rest: the same keys at run time, the same placeholders,
// no markup, the plural forms each language needs, and the untranslated list.

const AREAS: Record<Locale, Record<string, Record<string, string>>> = { en: EN_AREAS, sv: SV_AREAS }
const NAMES = Object.keys(EN_AREAS)

function entries(locale: Locale): [string, string, string][] {
  return Object.entries(AREAS[locale]).flatMap(([area, messages]) =>
    Object.entries(messages).map(([key, text]): [string, string, string] => [area, key, text]),
  )
}

function listed(area: string, key: string): boolean {
  return UNTRANSLATED.includes(area) || UNTRANSLATED.includes(`${area}.${key}`)
}

describe('the manifest', () => {
  it('lists exactly the languages the code knows', () => {
    expect([...LOCALES].sort()).toEqual(Object.keys(INTL_TAG).sort())
    expect([...LOCALES].sort()).toEqual(Object.keys(AREAS).sort())
    expect(LOCALES).toContain('en')
  })
})

describe('key parity', () => {
  it('has the same areas in every language', () => {
    for (const locale of LOCALES) expect(Object.keys(AREAS[locale]), locale).toEqual(NAMES)
  })

  it.each(NAMES)('has the same keys in the %s area of every language', (area) => {
    const source = Object.keys(EN_AREAS[area as keyof typeof EN_AREAS]).sort()
    for (const locale of LOCALES) expect(Object.keys(AREAS[locale][area] ?? {}).sort(), locale).toEqual(source)
  })

  it('has no empty message', () => {
    for (const locale of LOCALES) {
      for (const [area, key, text] of entries(locale)) expect(text.trim(), `${locale} ${area}.${key}`).not.toBe('')
    }
  })
})

describe('the untranslated list', () => {
  it('names only areas and keys that exist', () => {
    for (const entry of UNTRANSLATED) {
      const [area = '', ...rest] = entry.split('.')
      expect(NAMES, entry).toContain(area)
      if (rest.length > 0) expect(Object.keys(EN_AREAS[area as keyof typeof EN_AREAS]), entry).toContain(rest.join('.'))
    }
  })

  it('lists a key exactly while its Swedish is the English text', () => {
    for (const [area, key, sv] of entries('sv')) {
      const en = (EN_AREAS[area as keyof typeof EN_AREAS] as Record<string, string>)[key]
      const same = sv === en
      expect(same, `${area}.${key} ${same ? 'is English: list it or translate it' : 'is translated: take it off the list'}`).toBe(
        listed(area, key),
      )
    }
  })
})

describe('placeholders', () => {
  const NAMED = /\{([^{}]*)\}/g
  const BARE = /^[A-Za-z][A-Za-z0-9]*$/

  function names(text: string): string[] {
    return [...text.matchAll(NAMED)].map((match) => match[1] ?? '')
  }

  it('are bare names: no format spec, index or conversion', () => {
    for (const locale of LOCALES) {
      for (const [area, key, text] of entries(locale)) {
        for (const name of names(text)) expect(name, `${locale} ${area}.${key}`).toMatch(BARE)
        expect(text.replace(NAMED, ''), `${locale} ${area}.${key}`).not.toMatch(/[{}]/)
      }
    }
  })

  it('are the same in Swedish as in English', () => {
    for (const [area, key, sv] of entries('sv')) {
      const en = (EN_AREAS[area as keyof typeof EN_AREAS] as Record<string, string>)[key] ?? ''
      expect(names(sv).sort(), `${area}.${key}`).toEqual(names(en).sort())
    }
  })
})

describe('markup', () => {
  it('is not in any string: no tags, no entities', () => {
    for (const locale of LOCALES) {
      for (const [area, key, text] of entries(locale)) {
        expect(text, `${locale} ${area}.${key}`).not.toMatch(/<\/?[A-Za-z][^>]*>|&[A-Za-z]+;|&#\d+;/)
      }
    }
  })
})

describe('plural forms', () => {
  const FORMS = ['zero', 'one', 'two', 'few', 'many', 'other']

  function families(locale: Locale): Map<string, Set<string>> {
    const map = new Map<string, Set<string>>()
    for (const [area, key] of entries(locale)) {
      const form = key.split('.').pop() ?? ''
      if (!FORMS.includes(form) || !key.includes('.')) continue
      const base = `${area}.${key.slice(0, key.lastIndexOf('.'))}`
      map.set(base, (map.get(base) ?? new Set()).add(form))
    }
    return map
  }

  it.each([...LOCALES])('has every category %s needs, for each plural key', (locale) => {
    const needed = pluralCategories(locale)
    expect(families(locale).size).toBeGreaterThan(0)
    for (const [base, forms] of families(locale)) {
      for (const category of needed) expect(forms.has(category), `${locale} ${base}.${category}`).toBe(true)
    }
  })

  it('has the same plural families in every language', () => {
    expect([...families('sv').keys()].sort()).toEqual([...families('en').keys()].sort())
  })
})
