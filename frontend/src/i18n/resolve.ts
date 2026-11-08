import { DEFAULT_LOCALE, isLocale, type Locale } from './locale'

// Which language the browser uses (ADR 0015 section 2): the signed-in account's own value
// (`users.locale`, from the session), then the choice stored in this browser, then the browser's
// languages, then English. The decision is pure; the two storage helpers are the only place the
// choice touches localStorage.

export const STORAGE_KEY = 'ov-locale'

/** "sv-SE" and "sv" are Swedish. A tag that names no supported language is null. */
export function matchLanguage(tag: string): Locale | null {
  const primary = tag.trim().toLowerCase().split('-')[0]
  return isLocale(primary) ? primary : null
}

/** A code this build knows, else null (an account value the build lacks follows the browser). */
export function asLocale(value: unknown): Locale | null {
  return isLocale(value) ? value : null
}

export function resolveLocale(
  account: string | null,
  stored: string | null,
  languages: readonly string[],
): Locale {
  if (isLocale(account)) return account
  if (isLocale(stored)) return stored
  for (const tag of languages) {
    const match = matchLanguage(tag)
    if (match !== null) return match
  }
  return DEFAULT_LOCALE
}

export function readStoredLocale(): string | null {
  try {
    return window.localStorage.getItem(STORAGE_KEY)
  } catch {
    return null
  }
}

export function writeStoredLocale(locale: Locale): void {
  try {
    window.localStorage.setItem(STORAGE_KEY, locale)
  } catch {
    // Storage can be blocked: the choice then lasts until the page closes.
  }
}
