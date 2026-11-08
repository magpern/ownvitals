import { useSyncExternalStore } from 'react'
import type { Locale } from './locale'
import { getLocale, plural, subscribe, t } from './store'

/** The current language; the component re-renders when it changes. */
export function useLocale(): Locale {
  return useSyncExternalStore(subscribe, getLocale, getLocale)
}

/** `t` and `plural` for the current language. Call it in every component that shows a message. */
export function useI18n() {
  const locale = useLocale()
  return { locale, t, plural }
}

export function useT(): typeof t {
  useLocale()
  return t
}
