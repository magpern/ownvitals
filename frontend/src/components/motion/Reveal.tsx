import { useLayoutEffect, useRef, type ElementType, type ReactNode } from 'react'
import { staggerIndex } from './motionMath'
import { useInView } from './useInView'
import { useReducedMotion } from './useReducedMotion'

interface Props {
  children: ReactNode
  /** Position among siblings; the delay is index x --ov-stagger, capped by --ov-stagger-steps. */
  index?: number
  as?: ElementType
  className?: string
}

/**
 * Fades and rises its content once, when it first scrolls into view, staggered by `index`. Only
 * opacity and transform change, so the layout never shifts. With reduced motion (or no
 * IntersectionObserver) it renders in its final state from the first paint.
 */
export function Reveal({ children, index = 0, as: Tag = 'div', className }: Props) {
  const reduced = useReducedMotion()
  const ref = useRef<HTMLElement>(null)
  const seen = useInView(ref)
  const shown = reduced || seen

  useLayoutEffect(() => {
    // The property API, not a style attribute: the CSP allows it (docs/dev/frontend.md, Motion).
    ref.current?.style.setProperty('--ov-reveal-i', String(staggerIndex(index)))
  }, [index])

  const classes = ['ov-reveal', shown ? 'ov-reveal--in' : '', className ?? ''].filter(Boolean).join(' ')
  return (
    <Tag ref={ref} className={classes}>
      {children}
    </Tag>
  )
}
