import { decodeProfile } from '../../api/profile'
import { useResource } from '../../api/useResource'
import { getThemePreference, setThemePreference } from '../../theme/theme'
import { useEffect } from 'react'

// The theme follows the account: the shell reads the stored choice once and applies it, so a
// second device shows it on every page, not only after Settings is opened. The page paints with
// this browser's own copy first (initTheme), so a changed choice shows after one load (#98).
export function useAccountTheme(): void {
  const theme = useResource('/api/profile', decodeProfile).data?.theme
  useEffect(() => {
    if (theme !== undefined && theme !== getThemePreference()) setThemePreference(theme)
  }, [theme])
}
