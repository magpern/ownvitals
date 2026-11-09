import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from 'react'
import { decodeProfile, type ChartRange, type Profile } from '../api/profile'
import { useResource } from '../api/useResource'
import { DEFAULT_DISPLAY, type DisplaySettings } from '../lib/display'
import { getThemePreference, setThemePreference } from '../theme/theme'

// The signed-in user's saved display settings for every screen of the app frame: units, date
// format, the range the charts open on, and the theme. The shell reads the profile once; Settings
// hands a saved profile to `apply`. An answer that was requested before a save but arrives after it
// is older than the save and is ignored (#305), so it cannot bring an old theme back.

export interface DisplayState {
  settings: DisplaySettings
  chartRange: ChartRange | null
  /** The profile has been read (or could not be): a screen with no range of its own may choose one now. */
  settled: boolean
  apply: (profile: Profile) => void
}

const NONE: DisplayState = { settings: DEFAULT_DISPLAY, chartRange: null, settled: true, apply: () => undefined }
const DisplayContext = createContext<DisplayState>(NONE)

export function useDisplay(): DisplayState {
  return useContext(DisplayContext)
}

export function DisplayProvider({ children }: { children: ReactNode }) {
  const resource = useResource('/api/profile', decodeProfile)
  const read = resource.data
  const [applied, setApplied] = useState<Profile | null>(null)
  const profile = applied ?? read

  const apply = useCallback((next: Profile) => {
    setApplied(next)
  }, [])

  const theme = profile?.theme
  useEffect(() => {
    if (theme !== undefined && theme !== getThemePreference()) setThemePreference(theme)
  }, [theme])

  const value = useMemo<DisplayState>(
    () => ({
      settings:
        profile === null
          ? DEFAULT_DISPLAY
          : { weightUnit: profile.weight_unit, temperatureUnit: profile.temperature_unit, datetimeFormat: profile.datetime_format },
      chartRange: profile?.chart_range ?? null,
      settled: profile !== null || resource.status !== 'loading',
      apply,
    }),
    [profile, resource.status, apply],
  )
  return <DisplayContext.Provider value={value}>{children}</DisplayContext.Provider>
}
