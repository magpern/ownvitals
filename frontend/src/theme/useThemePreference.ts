import { useSyncExternalStore } from 'react'
import {
  getThemePreference,
  setThemePreference,
  subscribeThemePreference,
  type ThemePreference,
} from './theme'

/** The theme choice and its setter, for the Settings page (display options). */
export function useThemePreference(): readonly [ThemePreference, (preference: ThemePreference) => void] {
  const preference = useSyncExternalStore(subscribeThemePreference, getThemePreference, getThemePreference)
  return [preference, setThemePreference]
}
