import type { MetricDetail } from '../../api/metrics'
import { useFormat } from '../../i18n/useFormat'
import { useT } from '../../i18n'
import { Card } from '../ui/Card'
import { SourceMark } from '../ui/TierMark'
import { Value } from '../ui/Value'
import { longestHole, daysWithValue, slopePerMonth } from './metricData'
import { ProgressNote } from './ProgressNote'

// The four cards under the metric's name: the latest value, the typical range, the trend and the
// data quality of the window (docs/design/mockups/src/Metric.dc.html). A card with too little data
// behind it shows its progress toward the minimum and no number.

function LatestCard({ detail }: { detail: MetricDetail }) {
  const t = useT()
  const f = useFormat()
  const { latest, unit } = detail
  return (
    <Card title={t('detail.latest')} level={3}>
      {latest === null ? (
        <p className="ov-muted">{t('detail.latest.none')}</p>
      ) : (
        <>
          <Value size="large" value={f.number(latest.value, 1)} unit={unit} />
          <p className="ov-small ov-muted">{t('detail.latest.on', { day: f.day(latest.day), count: latest.n })}</p>
        </>
      )}
    </Card>
  )
}

function RangeCard({ detail }: { detail: MetricDetail }) {
  const t = useT()
  const f = useFormat()
  const { band } = detail
  return (
    <Card title={t('detail.typical', { days: band?.windowDays ?? detail.method.bandWindowDays })} level={3}>
      {band === null ? (
        <ProgressNote progress={detail.bandProgress}>{t('detail.typical.none')}</ProgressNote>
      ) : (
        <>
          <Value value={`${f.number(band.low, 1)}–${f.number(band.high, 1)}`} unit={detail.unit} />
          <p className="ov-small">
            <SourceMark source="ownvitals" />
          </p>
        </>
      )}
    </Card>
  )
}

function TrendCard({ detail }: { detail: MetricDetail }) {
  const t = useT()
  const f = useFormat()
  const { trend } = detail
  const month = slopePerMonth(trend.slopePerDay)
  const ready = trend.state === 'ok' && month !== null
  const direction = trend.direction === 'increasing' || trend.direction === 'decreasing' ? trend.direction : 'none'
  return (
    <Card title={t('detail.trend')} level={3}>
      {ready ? (
        <>
          <Value
            value={t('detail.trend.perMonth', { value: f.number(month, 2), unit: detail.unit })}
          />
          <p className="ov-small ov-muted">
            {t(`detail.trend.${direction}`)} ·{' '}
            {t(trend.significant === true ? 'detail.trend.significant' : 'detail.trend.notSignificant')}
          </p>
          <p className="ov-small">
            <SourceMark source="ownvitals" />
          </p>
        </>
      ) : (
        <ProgressNote progress={trend.progress}>{t('detail.trend.needsMore')}</ProgressNote>
      )}
    </Card>
  )
}

function QualityCard({ detail }: { detail: MetricDetail }) {
  const t = useT()
  const f = useFormat()
  const quality = detail.dataQuality
  const total = quality?.windowDays ?? detail.series.length
  const have = quality?.nDays ?? daysWithValue(detail.series)
  const unconfirmed = detail.unconfirmed.reduce((sum, day) => sum + day.n, 0)
  return (
    <Card title={t('detail.quality')} level={3}>
      <Value value={t('detail.quality.days', { have: f.number(have), total: f.number(total) })} />
      <p className="ov-small ov-muted">
        {t('detail.quality.detail', {
          coverage: f.number(Math.round((quality?.coverage ?? (total === 0 ? 0 : have / total)) * 100)),
          gap: f.number(quality?.longestGapDays ?? longestHole(detail.series)),
        })}
        {unconfirmed > 0 ? ` · ${t('detail.quality.unconfirmed', { count: f.number(unconfirmed) })}` : ''}
      </p>
    </Card>
  )
}

export function MetricSummary({ detail }: { detail: MetricDetail }) {
  return (
    <div className="ov-metric__summary">
      <LatestCard detail={detail} />
      <RangeCard detail={detail} />
      <TrendCard detail={detail} />
      <QualityCard detail={detail} />
    </div>
  )
}
