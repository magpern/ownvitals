import type { ReactNode } from 'react'
import type { Resource } from '../../api/useResource'
import { t, useT } from '../../i18n'
import { Button } from './Button'

// The data states: the vocabulary every view uses (docs/design/mockups/src/Main.dc.html, "Data
// states"). One component and one wording for every state, everywhere, so a stream that cannot be
// read is never drawn as a zero and never reads as "normal" (design principle 2).
//
// The pill is the fixed part: its wording and its tone. What it adds is the caller's: a line of
// detail as `children` ("No weigh-in for 14 days. The last value is shown with its date,
// greyed."), and, for the two states that count toward something, `progress` (a bar).

export type DataStateKind =
  | 'up-to-date'
  | 'stale'
  | 'importing'
  | 'incomplete-import'
  | 'insufficient-data'
  | 'not-supported'
  | 'not-shared'
  | 'not-available'
  | 'needs-reconnect'
  | 'withings-plus'

type Tone = 'neutral' | 'ok' | 'attn' | 'urgent' | 'info' | 'off'
type Dot = 'ok' | 'attn' | 'urgent'

interface Definition {
  tone: Tone
  /** A dot in front of the label (the states that say "fine", "look" and "broken"). */
  dot?: Dot
  /** The bar's measure, for the states that have one (a catalogue key). */
  bar?: 'ui.bar.importing' | 'ui.bar.insufficient-data'
}

const STATES: Record<DataStateKind, Definition> = {
  'up-to-date': { tone: 'ok', dot: 'ok' },
  stale: { tone: 'attn', dot: 'attn' },
  // The label carries the percentage: see `dataStateLabel`.
  importing: { tone: 'neutral', bar: 'ui.bar.importing' },
  'incomplete-import': { tone: 'attn' },
  'insufficient-data': { tone: 'neutral', bar: 'ui.bar.insufficient-data' },
  'not-supported': { tone: 'off' },
  'not-shared': { tone: 'off' },
  'not-available': { tone: 'off' },
  'needs-reconnect': { tone: 'urgent', dot: 'urgent' },
  'withings-plus': { tone: 'info' },
}

export const DATA_STATE_KINDS = Object.keys(STATES) as DataStateKind[]

/** 0 to 100, whole numbers: a bar and a label never show 64.3 % or 120 %. */
function percent(progress: number): number {
  return Math.min(100, Math.max(0, Math.round(Number.isFinite(progress) ? progress : 0)))
}

/** The wording of a state: "Up to date", "Importing 64%", "Withings+ needed". */
export function dataStateLabel(kind: DataStateKind, progress?: number): string {
  return kind === 'importing' && progress !== undefined
    ? t('ui.state.importingPercent', { percent: percent(progress) })
    : t(`ui.state.${kind}`)
}

/** A pill: the shape of every state, and of the "Coming later" marker of a screen not built yet. */
export function StatePill({ tone = 'neutral', dot, children }: { tone?: Tone; dot?: Dot; children: ReactNode }) {
  return (
    <span className={tone === 'neutral' ? 'ov-state' : `ov-state ov-state--${tone}`}>
      {dot === undefined ? null : <span className={`ov-dot ov-dot--${dot}`} aria-hidden="true" />}
      {children}
    </span>
  )
}

export type DataStateProps = { children?: ReactNode } & (
  | { kind: 'importing'; progress: number }
  | { kind: 'insufficient-data'; progress?: number }
  | { kind: Exclude<DataStateKind, 'importing' | 'insufficient-data'>; progress?: undefined }
)

export function DataState({ kind, progress, children }: DataStateProps) {
  const t = useT()
  const { tone, dot, bar } = STATES[kind]
  const pill = (
    <StatePill tone={tone} dot={dot}>
      {dataStateLabel(kind, progress)}
    </StatePill>
  )
  const hasBar = bar !== undefined && progress !== undefined
  if (!hasBar && children === undefined) return pill
  return (
    <div className="ov-data-state" data-state={kind}>
      {pill}
      {hasBar ? <progress className="ov-bar" max={100} value={percent(progress)} aria-label={t(bar)} /> : null}
      {children === undefined ? null : <p className="ov-data-state__detail">{children}</p>}
    </div>
  )
}

/**
 * What a screen shows while it reads (`useResource`, src/api/useResource.ts): a line while it
 * loads, the reason and a "Try again" when it fails, and the data once there is some. While a
 * reload runs the last good data stays on screen. The states of the data itself (stale, importing,
 * not supported ...) are `DataState`, chosen by the screen from what the API says.
 */
export function ResourceView<T>({
  resource,
  children,
}: {
  resource: Resource<T>
  children: (data: T) => ReactNode
}) {
  const t = useT()
  if (resource.data !== null) return <>{children(resource.data)}</>
  if (resource.status === 'error' && resource.error !== null) {
    return (
      <div className="ov-resource" role="alert" data-kind={resource.error.kind}>
        <p className="ov-resource__message">{resource.error.message}</p>
        <Button onClick={resource.reload}>{t('common.tryAgain')}</Button>
      </div>
    )
  }
  return (
    <p className="ov-resource" role="status" aria-busy="true">
      {t('common.loadingDots')}
    </p>
  )
}
