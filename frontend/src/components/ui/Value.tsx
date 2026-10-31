import type { ReactNode } from 'react'

// The value styles of the design (docs/design/mockups/src/Main.dc.html, "Who said it", and
// "Type"): a number with tabular figures and its unit, a raw value in mono, and the estimate,
// which never looks like a certified value (italic, dotted, "est." in front).

/** Tabular figures for a number in running text. */
export function Num({ children }: { children: ReactNode }) {
  return <span className="ov-num">{children}</span>
}

/** Ids, versions, hashes and raw values. */
export function Mono({ children }: { children: ReactNode }) {
  return <span className="ov-mono">{children}</span>
}

interface ValueProps {
  /** The number as it is to be read, already formatted (`78.4`, `77.6–79.3`). */
  value: string
  unit?: string
  /** `large` is the 40 px of a headline value; the default is the 30 px of a tile. */
  size?: 'default' | 'large'
  /** An estimate by ownvitals (owner flag only): italic, dotted, "est." in front. */
  estimated?: boolean
}

export function Value({ value, unit, size = 'default', estimated = false }: ValueProps) {
  const classes = ['ov-value']
  if (size === 'large') classes.push('ov-value--large')
  if (estimated) classes.push('ov-value--estimate')
  return (
    <span className={classes.join(' ')}>
      {estimated ? <span className="ov-value__est">est. </span> : null}
      <span className="ov-value__num">{value}</span>
      {unit === undefined ? null : <span className="ov-value__unit">{unit}</span>}
    </span>
  )
}
