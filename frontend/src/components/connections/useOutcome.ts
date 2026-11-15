import { useEffect, useState } from 'react'
import { isMessageKey, useT } from '../../i18n'

/** How long the outcome sentence stays up (issue 304); it is a notice, not a state of the page. */
export const OUTCOME_SHOWN_MS = 10_000

/**
 * The sentence for `?withings=<outcome>` on the way back from Withings (nothing for `ok`). It
 * goes away after `OUTCOME_SHOWN_MS`, and a different outcome in the URL shows it afresh.
 */
export function useOutcome(search: string): string | null {
  const t = useT()
  const code = new URLSearchParams(search).get('withings')
  const [dismissed, setDismissed] = useState<string | null>(null)
  const shown = code !== null && code !== 'ok' && code !== 'connected' && dismissed !== search
  useEffect(() => {
    if (!shown) return
    const timer = window.setTimeout(() => {
      setDismissed(search)
    }, OUTCOME_SHOWN_MS)
    return () => {
      window.clearTimeout(timer)
    }
  }, [shown, search])
  if (!shown) return null
  const key = `connections.outcome.${code}`
  return t(isMessageKey(key) ? key : 'connections.outcome.failed')
}
