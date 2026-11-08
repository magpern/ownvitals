import manifest from './locales.json'

// The supported languages (ADR 0015 section 1). `locales.json` is the manifest; `Locale` below
// lists the same codes for the type checker, and i18n.test.ts fails when the two differ. A code
// joins the manifest only together with its catalogue (sv/index.ts, `LOADERS` in store.ts) and
// its picker name.

export type Locale = 'en' | 'sv'

export const LOCALES: readonly Locale[] = manifest as Locale[]

export const DEFAULT_LOCALE: Locale = 'en'

/** The BCP 47 tag `Intl` gets for a locale: English as written in Europe (day first, 24 hours). */
export const INTL_TAG: Record<Locale, string> = { en: 'en-GB', sv: 'sv-SE' }

/** What the picker shows for each language: its own name, never translated. */
export const LOCALE_NAMES: Record<Locale, string> = { en: 'English', sv: 'Svenska' }

export function isLocale(value: unknown): value is Locale {
  return typeof value === 'string' && (LOCALES as readonly string[]).includes(value)
}
