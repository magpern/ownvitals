import { useT } from '../../i18n'
import { isMessageKey } from '../../i18n'

/** The sentence for `?withings=<outcome>` on the way back from Withings (nothing for `ok`). */
export function useOutcome(search: string): string | null {
  const t = useT()
  const code = new URLSearchParams(search).get('withings')
  if (code === null || code === 'ok' || code === 'connected') return null
  const key = `connections.outcome.${code}`
  return t(isMessageKey(key) ? key : 'connections.outcome.failed')
}
