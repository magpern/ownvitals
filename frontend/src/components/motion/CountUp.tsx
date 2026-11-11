import { useLayoutEffect, useRef } from 'react'
import { countValue } from './motionMath'
import { startJob } from './countUpLoop'
import { useReducedMotion } from './useReducedMotion'
import { useInView } from './useInView'

interface Props {
  value: number
  /** How the number is written; the default is `String`. Use the app's formatters. */
  format?: (value: number) => string
  /** Estimates (experimental tier) do not count up (motion.md rule 5). */
  still?: boolean
  /** Milliseconds; defaults to the token's 700. */
  durationMs?: number
}

const DEFAULT_MS = 700

/**
 * A number that counts up from its previous value (or from 0 on first mount) to `value`. The final
 * text is in the DOM from the first render (a hidden sizer fixes the width, a screen-reader span
 * says it); the animated digits are aria-hidden, so nothing is re-announced per frame. With
 * reduced motion, `still`, a hidden tab or a first paint off-screen, it shows the final value at once.
 */
export function CountUp({ value, format = String, still = false, durationMs = DEFAULT_MS }: Props) {
  const reduced = useReducedMotion()
  const rootRef = useRef<HTMLSpanElement>(null)
  const liveRef = useRef<HTMLSpanElement>(null)
  const shown = useRef<number | null>(null)
  const inView = useInView(rootRef)
  const final = format(value)
  const animate = !reduced && !still
  const formatRef = useRef(format)
  useLayoutEffect(() => {
    formatRef.current = format
  })

  useLayoutEffect(() => {
    const live = liveRef.current
    if (live === null) return
    const write = (n: number) => {
      shown.current = n
      live.textContent = formatRef.current(n)
    }
    if (!inView) {
      // Off-screen: the final text stays; the count runs when it first scrolls into view.
      live.textContent = formatRef.current(value)
      return
    }
    if (!animate || document.hidden) {
      write(value)
      return
    }
    const from = shown.current ?? 0
    if (from === value) {
      write(value)
      return
    }
    write(from)
    return startJob((elapsed) => {
      write(countValue(from, value, elapsed, durationMs))
      if (elapsed < durationMs) return false
      write(value)
      return true
    })
  }, [value, animate, inView, durationMs])

  return (
    <span className="ov-countup" ref={rootRef}>
      <span className="ov-sr">{final}</span>
      <span className="ov-countup__final" aria-hidden="true">
        {final}
      </span>
      <span className="ov-countup__live" aria-hidden="true" ref={liveRef}>
        {final}
      </span>
    </span>
  )
}
