import type { ReactNode } from 'react'
import { useT } from '../../i18n'
import { Card } from './Card'
import { SourceMark, type Source } from './TierMark'

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
  const t = useT()
  const classes = ['ov-value']
  if (size === 'large') classes.push('ov-value--large')
  if (estimated) classes.push('ov-value--estimate')
  return (
    <span className={classes.join(' ')}>
      {estimated ? <span className="ov-value__est">{t('ui.estimate')} </span> : null}
      <span className="ov-value__num">{value}</span>
      {unit === undefined ? null : <span className="ov-value__unit">{unit}</span>}
    </span>
  )
}

export type ValueSource = 'reported' | 'calculated' | 'estimated'

interface ValueCardProps {
  /** Who produced the number: Withings, ownvitals' own maths, or an ownvitals estimate. */
  source: ValueSource
  /** What follows the dot in the source line: the device, "analysis 1.2". */
  detail?: string
  value: string
  unit?: string
  /** The name of the value: "QTc", "Typical range, 90 days". */
  label: string
  /** One line under it: what it is and how it was made. */
  children?: ReactNode
}

const SOURCE_OF: Record<ValueSource, Source> = {
  reported: 'withings',
  calculated: 'ownvitals',
  estimated: 'estimate',
}

/**
 * The three value styles of docs/design/mockups/src/Main.dc.html ("Who said it"): reported by
 * Withings (the device mark), calculated by ownvitals (the band mark) and estimated by ownvitals
 * (the experimental mark, "est.", italic, dotted, in a dashed box). The source line is always
 * there, so a number never appears without who said it.
 */
export function ValueCard({ source, detail, value, unit, label, children }: ValueCardProps) {
  return (
    <Card experimental={source === 'estimated'}>
      <div className="ov-valuecard">
        <SourceMark source={SOURCE_OF[source]} detail={detail} />
        <Value value={value} unit={unit} size="large" estimated={source === 'estimated'} />
        <p className="ov-valuecard__label">{label}</p>
        {children === undefined ? null : <p className="ov-valuecard__note">{children}</p>}
      </div>
    </Card>
  )
}
