import { useLayoutEffect, useRef, useSyncExternalStore, type ElementType, type ReactNode } from 'react'
import { staggerIndex } from './motionMath'
import { useInView } from './useInView'
import { useReducedMotion } from './useReducedMotion'

const subscribeNever = () => () => {}

interface Props {
  children: ReactNode
  /** Position among siblings; the delay is index x --ov-stagger, capped by --ov-stagger-steps. */
  index?: number
  as?: ElementType
  className?: string
}

/**
 * Fades and rises its content once, when it first scrolls into view, staggered by `index`. Only
 * opacity and transform change, so the layout never shifts. Until mounted, and with reduced
 * motion (or no IntersectionObserver), it is in its final state.
 */
export function Reveal({ children, index = 0, as: Tag = 'div', className }: Props) {
  const reduced = useReducedMotion()
  const ref = useRef<HTMLElement>(null)
  const seen = useInView(ref)
  const shown = reduced || seen
  // Visible by default: the hidden class is added only after mount, so prerendered markup (whose
  // useReducedMotion server snapshot is "reduced") is never hidden and then re-hidden on hydration.
  const mounted = useSyncExternalStore(subscribeNever, () => true, () => false)

  useLayoutEffect(() => {
    // The property API, not a style attribute: the CSP allows it (docs/dev/frontend.md, Motion).
    ref.current?.style.setProperty('--ov-reveal-i', String(staggerIndex(index)))
  }, [index])

  const classes = ['ov-reveal', shown ? 'ov-reveal--in' : mounted ? 'ov-reveal--pre' : '', className ?? ''].filter(Boolean).join(' ')
  return (
    <Tag ref={ref} className={classes}>
      {children}
    </Tag>
  )
}
