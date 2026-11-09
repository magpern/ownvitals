import { useCallback, useRef, useState } from 'react'
import { decodeProfile, fieldsOf, saveProfile, type Profile, type ProfileFields } from '../../api/profile'
import { errorMessage } from '../../api/messages'
import { useResource, type Resource } from '../../api/useResource'
import { useDisplay } from '../../display/DisplayContext'
import { t as translate } from '../../i18n'

// The signed-in user's profile for the Settings tabs: read once, saved as one document per change.
// The server answer replaces the screen's copy; a refused save shows the server's words and leaves
// the copy as it was. The account's theme is applied by the shell (display/DisplayContext.tsx).

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
  const { apply } = useDisplay()
  // Saves overlap when a person changes two options quickly: only the answer to the newest one counts.
  const newest = useRef(0)

  const save = useCallback(async (patch: Partial<ProfileFields>): Promise<boolean> => {
    if (profile === null) return false
    const mine = ++newest.current
    setBusy(true)
    setNote(null)
    try {
      const next = await saveProfile({ ...fieldsOf(profile), ...patch })
      if (mine === newest.current) {
        setSaved(next)
        apply(next)
      }
      setNote({ kind: 'success', text: translate('settings.saved') })
      return true
    } catch (caught) {
      setNote({ kind: 'error', text: errorMessage(caught) })
      return false
    } finally {
      if (mine === newest.current) setBusy(false)
    }
  }, [profile, apply])

  return { resource, profile, busy, note, save }
}
