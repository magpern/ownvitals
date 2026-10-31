import { useId, type ReactNode } from 'react'
import { StatePill } from './DataState'

// The card and the stack of the design (docs/design/mockups/src/ov.css: `.card`, `.stack`), and
// the "Coming later" row of a screen that is not built yet (as in the Security mock-up).
//
// `Card` here is the design's card. The narrow, centred card of the sign-in pages is `Card` in
// ../forms.tsx; #10 and #11 move those pages over to this one.

interface CardProps {
  /** A heading for the card. Without it the card is a plain box. */
  title?: ReactNode
  /** `3` for a card inside a section that already has a level 2 heading. */
  level?: 2 | 3
  /** Beside the title: a state, a link, a control. */
  aside?: ReactNode
  flat?: boolean
  /** An experimental estimate: a dashed box that says "not certified". */
  experimental?: boolean
  children?: ReactNode
}

export function Card({ title, level = 2, aside, flat = false, experimental = false, children }: CardProps) {
  const id = useId()
  const classes = ['ov-card']
  if (flat) classes.push('ov-card--flat')
  if (experimental) classes.push('ov-card--experimental')
  if (title === undefined) return <div className={classes.join(' ')}>{children}</div>
  const Heading = level === 2 ? 'h2' : 'h3'
  return (
    <section className={classes.join(' ')} aria-labelledby={id}>
      <div className="ov-card__head">
        <Heading id={id} className={level === 2 ? 'ov-card__title' : 'ov-card__title ov-card__title--small'}>
          {title}
        </Heading>
        {aside}
      </div>
      {children}
    </section>
  )
}

/** A column of things with an even gap: `sm` 8 px, the default 12 px, `lg` 20 px. */
export function Stack({ gap, children }: { gap?: 'sm' | 'lg'; children: ReactNode }) {
  return (
    <div className="ov-stack" data-gap={gap}>
      {children}
    </div>
  )
}

/** What a screen is, and the "Coming later" marker: the pattern of every screen not built yet. */
export function ComingLater({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="ov-setting">
      <div className="ov-setting__what">
        <span className="ov-setting__label">{label}</span>
        <p>{children}</p>
      </div>
      <StatePill tone="off">Coming later</StatePill>
    </div>
  )
}
