import { useState } from 'react'
import { useReducedMotion } from './useReducedMotion'

/**
 * True while `id` (the day of the latest reading) is a reading that arrived after the first one
 * this component saw: a new reading came in while the page was open. False on first mount, with
 * reduced motion and while the tab is hidden (docs/design/motion.md, item 9). The caller keys the
 * pulse by `id` and the CSS runs one iteration, so it plays once per reading and never loops.
 */
export function useNewReading(id: string | null): boolean {
  const reduced = useReducedMotion()
  const [state, setState] = useState<{ seen: string | null; fresh: string | null }>({ seen: id, fresh: null })
  if (state.seen !== id) {
    // Derived state: set during render, React re-renders at once with the new value.
    setState({ seen: id, fresh: state.seen !== null && id !== null ? id : null })
  }
  const visible = typeof document === 'undefined' || document.visibilityState !== 'hidden'
  return !reduced && visible && id !== null && state.fresh === id
}
