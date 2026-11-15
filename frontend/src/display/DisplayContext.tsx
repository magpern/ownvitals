import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { decodeProfile, type ChartRange, type Profile } from '../api/profile'
import { useResource } from '../api/useResource'
import { DEFAULT_DISPLAY, type DisplaySettings } from '../lib/display'
import { getThemePreference, setThemePreference, subscribeThemePreference } from '../theme/theme'

// The signed-in user's saved display settings for every screen of the app frame: units, date
// format, the range the charts open on, and the theme. The shell reads the profile once; Settings
// hands a saved profile to `apply`. An answer that was requested before a save but arrives after it
// is older than the save and is ignored (issue 305), so it cannot bring an old theme back. The same
// goes for a theme chosen on this device after the shell mounted: the profile read that lands later
// is older than that choice and must not undo it (issue 441).

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

  // True once someone other than this provider changed the theme after mount (a click in Settings,
  // another tab). A profile that was only read, never saved, no longer speaks for the device then.
  const chosenHere = useRef(false)
  const ownChange = useRef(false)
  useEffect(
    () =>
      subscribeThemePreference(() => {
        if (!ownChange.current) chosenHere.current = true
      }),
    [],
  )

  const theme = profile?.theme
  const fromSave = applied !== null
  useEffect(() => {
    if (theme === undefined || theme === getThemePreference()) return
    if (!fromSave && chosenHere.current) return
    ownChange.current = true
    try {
      setThemePreference(theme)
    } finally {
      ownChange.current = false
    }
  }, [theme, fromSave])

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
