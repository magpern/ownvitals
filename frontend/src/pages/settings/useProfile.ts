import { useCallback, useEffect, useState } from 'react'
import { decodeProfile, fieldsOf, saveProfile, type Profile, type ProfileFields } from '../../api/profile'
import { errorMessage } from '../../api/messages'
import { useResource, type Resource } from '../../api/useResource'
import { t as translate } from '../../i18n'
import { getThemePreference, setThemePreference } from '../../theme/theme'

// The signed-in user's profile for the Settings tabs: read once, saved as one document per change.
// The server answer replaces the screen's copy; a refused save shows the server's words and leaves
// the copy as it was. The theme follows the account: what the server holds is applied on load (so
// a second device shows it), and it is also kept in this browser by `setThemePreference`.

export interface ProfileState {
  resource: Resource<Profile>
  profile: Profile | null
  busy: boolean
  note: { kind: 'success' | 'error'; text: string } | null
  /** Saves the profile with `patch` applied; resolves to whether the server accepted it. */
  save: (patch: Partial<ProfileFields>) => Promise<boolean>
}

export function useProfile(): ProfileState {
  const resource = useResource('/api/profile', decodeProfile)
  const [saved, setSaved] = useState<Profile | null>(null)
  const [busy, setBusy] = useState(false)
  const [note, setNote] = useState<ProfileState['note']>(null)
  const profile = saved ?? resource.data

  const loadedTheme = resource.data?.theme
  useEffect(() => {
    if (loadedTheme !== undefined && loadedTheme !== getThemePreference()) setThemePreference(loadedTheme)
  }, [loadedTheme])

  const save = useCallback(async (patch: Partial<ProfileFields>): Promise<boolean> => {
    if (profile === null) return false
    setBusy(true)
    setNote(null)
    try {
      const next = await saveProfile({ ...fieldsOf(profile), ...patch })
      setSaved(next)
      setNote({ kind: 'success', text: translate('settings.saved') })
      return true
    } catch (caught) {
      setNote({ kind: 'error', text: errorMessage(caught) })
      return false
    } finally {
      setBusy(false)
    }
  }, [profile])

  return { resource, profile, busy, note, save }
}
