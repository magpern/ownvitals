import { EN, type MessageKey, type Messages } from './en'
import { DEFAULT_LOCALE, INTL_TAG, type Locale } from './locale'
import { readStoredLocale, resolveLocale, writeStoredLocale } from './resolve'
import { hasKey, lookup, pluralText, interpolate, type Params } from './translate'

// The language in use and the catalogues loaded so far. English is bundled; every other language
// is its own chunk, listed here as a static import (no dynamic path), so the build can split it
// and the CSP stays 'self'. The language switches only once its catalogue has arrived, so a page
// never shows half of one language.

const LOADERS: Record<Locale, () => Promise<Messages>> = {
  en: () => Promise.resolve(EN),
  sv: () => import('./sv').then((module) => module.SV),
}

const loaded: Partial<Record<Locale, Messages>> = { en: EN }
const listeners = new Set<() => void>()
let active: Locale = DEFAULT_LOCALE
let latest = 0

export function getLocale(): Locale {
  return active
}

export function subscribe(listener: () => void): () => void {
  listeners.add(listener)
  return () => {
    listeners.delete(listener)
  }
}

function apply(locale: Locale): void {
  active = locale
  document.documentElement.lang = locale
  for (const listener of [...listeners]) listener()
}

function load(locale: Locale): Promise<void> | null {
  if (loaded[locale] !== undefined) return null
  return LOADERS[locale]().then((messages) => {
    loaded[locale] = messages
  })
}

function switchTo(locale: Locale, persist: boolean): Promise<void> | null {
  const mine = ++latest
  const pending = load(locale)
  const finish = () => {
    // A later choice wins over a slower earlier one.
    if (mine !== latest) return
    if (persist) writeStoredLocale(locale)
    apply(locale)
  }
  if (pending === null) {
    finish()
    return null
  }
  return pending.then(finish)
}

/** Picks the language at start-up. Null when it is ready at once; else a promise to wait for. */
export function initLocale(): Promise<void> | null {
  const languages = typeof navigator === 'undefined' ? [] : navigator.languages
  return switchTo(resolveLocale(readStoredLocale(), languages), false)
}

/** The user's choice: stored for the next visit and applied without a reload. */
export function setLocale(locale: Locale): Promise<void> {
  return switchTo(locale, true) ?? Promise.resolve()
}

export function t(key: MessageKey, params?: Params): string {
  return interpolate(lookup(loaded[active], EN, key), params)
}

/** `key` is a base name: its forms are `<base>.one`, `<base>.other` ... in the catalogue. */
export type PluralKey = MessageKey extends infer K
  ? K extends `${infer Base}.other`
    ? Base
    : never
  : never

export function plural(key: PluralKey, count: number, params?: Params): string {
  const shown = new Intl.NumberFormat(INTL_TAG[active]).format(count)
  return pluralText(loaded[active], EN, active, key, count, shown, params)
}

/** Whether `key` is a message: for keys built from a server code, which may be unknown. */
export function isMessageKey(key: string): key is MessageKey {
  return hasKey(EN, key)
}
