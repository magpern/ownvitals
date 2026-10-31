// The colour theme: System (the default), Light or Dark.
//
// Light and dark are both in styles/tokens.css. The device's `prefers-color-scheme` picks one
// unless <html> has a `data-theme="light|dark"` attribute, which wins either way. "System" is
// the absence of the attribute. The choice is kept per browser in localStorage (every access in
// try/catch: it can throw or come back empty in a private window or with site data blocked, and
// the page must work without it). Whether it should also follow the account is a question for the
// Settings issue.
//
// `initTheme()` runs from main.tsx before React renders, so the attribute is on <html> before the
// first paint of the app. There is no inline script (the CSP is 'self'): this is an ordinary
// module of the bundle.

export type ThemePreference = 'system' | 'light' | 'dark'

export const THEME_PREFERENCES: readonly ThemePreference[] = ['system', 'light', 'dark']
export const THEME_STORAGE_KEY = 'ownvitals.theme'
export const THEME_ATTRIBUTE = 'data-theme'

export function isThemePreference(value: unknown): value is ThemePreference {
  return typeof value === 'string' && (THEME_PREFERENCES as readonly string[]).includes(value)
}

/** The stored choice; System when nothing, or anything unknown, is stored or storage is unusable. */
export function readStoredPreference(): ThemePreference {
  try {
    const stored = window.localStorage.getItem(THEME_STORAGE_KEY)
    return isThemePreference(stored) ? stored : 'system'
  } catch {
    return 'system'
  }
}

function storePreference(preference: ThemePreference): void {
  try {
    // System is the default: it is stored as no value at all.
    if (preference === 'system') window.localStorage.removeItem(THEME_STORAGE_KEY)
    else window.localStorage.setItem(THEME_STORAGE_KEY, preference)
  } catch {
    // The choice then lasts until the page is closed.
  }
}

/** Puts the choice on the root element: an attribute for Light and Dark, none for System. */
export function applyTheme(preference: ThemePreference): void {
  const root = document.documentElement
  if (preference === 'system') root.removeAttribute(THEME_ATTRIBUTE)
  else root.setAttribute(THEME_ATTRIBUTE, preference)
}

let current: ThemePreference = 'system'
const listeners = new Set<() => void>()
let listening = false

function publish(preference: ThemePreference): void {
  if (preference === current) return
  current = preference
  for (const listener of listeners) listener()
}

// Another tab of this browser changed the choice.
function onStorage(event: StorageEvent): void {
  if (event.key !== null && event.key !== THEME_STORAGE_KEY) return
  const preference = readStoredPreference()
  applyTheme(preference)
  publish(preference)
}

/** The choice in force. */
export function getThemePreference(): ThemePreference {
  return current
}

/** Changes the choice: applies it at once and keeps it in this browser. */
export function setThemePreference(preference: ThemePreference): void {
  if (!isThemePreference(preference)) return
  storePreference(preference)
  applyTheme(preference)
  publish(preference)
}

/** Calls `listener` whenever the choice changes (for `useSyncExternalStore`). */
export function subscribeThemePreference(listener: () => void): () => void {
  listeners.add(listener)
  return () => {
    listeners.delete(listener)
  }
}

/**
 * Reads the stored choice and applies it. Call it once, before the first render.
 * Also follows a change made in another tab.
 */
export function initTheme(): ThemePreference {
  const preference = readStoredPreference()
  applyTheme(preference)
  current = preference
  for (const listener of listeners) listener()
  if (!listening) {
    listening = true
    window.addEventListener('storage', onStorage)
  }
  return preference
}
