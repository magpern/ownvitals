import type { MetricTile } from '../../api/overview'
import { useT } from '../../i18n'
import { useFormat } from '../../i18n/useFormat'
import type { MessageKey } from '../../i18n/en'
import { Link } from '../../router/Router'
import { RangeBar, rangePosition } from '../charts/RangeBar'
import { Sparkline } from '../charts/Sparkline'
import { Card } from '../ui/Card'
import { DataState, type DataStateKind } from '../ui/DataState'
import { Value } from '../ui/Value'
import { detailKey, digitsOf, showsValue, stateKind, type RangeId } from './overviewModel'

// One tile of the overview (docs/design/mockups/src/Dashboard.dc.html): the metric's name, its
// latest value with the sparkline and the typical range, or, when the stream cannot give a number,
// the state and what it means. A tile never shows a number the API did not send, and never a zero
// for a stream that cannot be read. On a phone the sparkline gives way to the range bar.

interface Props {
  tile: MetricTile
  range: RangeId
  /** A number written by the caller (blood pressure: "118/76"); otherwise the latest value. */
  valueText?: string | null
  /** The tile's name when the overview catalogue has none for the key (the body page). */
  name?: string
}

/** The data state under a tile, with the progress the two counting states carry. */
function State({ kind, tile, children }: { kind: DataStateKind; tile: MetricTile; children: string }) {
  const have = tile.progress?.daysWithData ?? 0
  const need = tile.progress?.daysRequired ?? 0
  const fraction = need > 0 ? (have / need) * 100 : 0
  if (kind === 'importing') return <DataState kind="importing" progress={tile.progressPercent ?? 0}>{children}</DataState>
  if (kind === 'insufficient-data') {
    return <DataState kind="insufficient-data" progress={tile.progressPercent ?? fraction}>{children}</DataState>
  }
  return <DataState kind={kind}>{children}</DataState>
}

export function Tile({ tile, range, valueText, name: given }: Props) {
  const t = useT()
  const f = useFormat()
  const kind = stateKind(tile.state)
  const name = given ?? t(`overview.metric.${tile.key}` as MessageKey)
  const digits = digitsOf(tile.key)
  const latest = showsValue(tile) ? tile.latest : null
  const band = tile.typicalRange
  const withData = tile.sparkline.filter((p) => p.value !== null).length

  let detail = t(detailKey(tile.state))
  if (tile.state === 'insufficient_data' && tile.progress !== null) {
    detail = t('overview.tile.progress', { have: tile.progress.daysWithData, need: tile.progress.daysRequired })
  }

  const rangeText = band === null ? '' : `${f.quantity(band.low, tile.unit, digits)}–${f.quantity(band.high, tile.unit, digits)}`
  const position = latest !== null && band !== null ? rangePosition(latest.value, band.low, band.high) : null

  return (
    <Card level={3} title={<Link to={`/m/${tile.key}?range=${range}`}>{name}</Link>}>
      <div className={tile.state === 'stale' ? 'ov-tile ov-tile--old' : 'ov-tile'} data-state={tile.state}>
        {latest === null ? (
          kind === null ? <p className="ov-muted">{t('overview.tile.noValue')}</p> : null
        ) : (
          <>
            <Value value={valueText ?? f.quantity(latest.value, tile.unit, digits)} unit={valueText ? undefined : f.unit(tile.unit)} />
            {tile.sparkline.length > 0 ? (
              <div className="ov-tile__spark">
                <Sparkline
                  points={tile.sparkline.map((p) => ({ value: p.value, low: band?.low, high: band?.high }))}
                  attention={position !== null && position !== 'within'}
                />
              </div>
            ) : null}
            {band !== null && position !== null ? (
              <>
                <div className="ov-tile__bar" aria-hidden="true">
                  <RangeBar value={latest.value} low={band.low} high={band.high} format={(n) => f.quantity(n, tile.unit, digits)} />
                </div>
                <p className={position === 'within' ? 'ov-small ov-muted' : 'ov-small ov-tile__attn'}>
                  {t(`overview.range.${position}`, { range: rangeText })}
                </p>
              </>
            ) : null}
            <p className="ov-small ov-muted">
              {t('overview.tile.foot', { day: f.day(latest.day, 'short'), count: withData, days: tile.sparkline.length })}
            </p>
          </>
        )}
        {kind === null ? null : (
          <State kind={kind} tile={tile}>
            {detail}
          </State>
        )}
      </div>
    </Card>
  )
}
