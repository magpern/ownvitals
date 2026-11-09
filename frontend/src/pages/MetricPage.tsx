import { DEFAULT_RANGE, decodeMetricDetail, isRange, metricPath, type MetricDetail, type Range } from '../api/metrics'
import { useResource } from '../api/useResource'
import { MethodCard } from '../components/metric/MethodCard'
import { MetricChartCard } from '../components/metric/MetricChartCard'
import { MetricDays } from '../components/metric/MetricDays'
import { MetricSummary } from '../components/metric/MetricSummary'
import { progressPercent } from '../components/metric/metricData'
import { BLOCKED_STATES, dataStateKind, useMetricName } from '../components/metric/names'
import { RangeSwitch } from '../components/metric/RangeSwitch'
import { Stack } from '../components/ui/Card'
import { DataState, ResourceView } from '../components/ui/DataState'
import { useT } from '../i18n'
import { Link, useQuery } from '../router/Router'
import { useMetricParams } from './metricRoute'

// A metric over a range (docs/design/mockups/src/Metric.dc.html): the state of its data, the latest
// value, the typical range, the trend and the data quality, the daily chart, and the days behind it.

function Crumbs({ name }: { name: string }) {
  const t = useT()
  return (
    <nav className="ov-metric__crumbs" aria-label={t('detail.crumb')}>
      <Link to="/">{t('shell.nav.overview')}</Link>
      <span aria-hidden="true">/</span>
      <span aria-current="page">{name}</span>
    </nav>
  )
}

function progressFraction(detail: MetricDetail): number {
  return progressPercent(detail.bandProgress ?? detail.trend.progress)
}

function MetricState({ detail }: { detail: MetricDetail }) {
  const t = useT()
  const kind = dataStateKind(detail.state)
  if (kind === 'importing') return <DataState kind="importing" progress={progressFraction(detail)} />
  if (kind === 'insufficient-data') return null // the cards below carry their own progress
  const note = BLOCKED_STATES.has(detail.state)
    ? t('detail.state.blocked')
    : detail.state === 'stale'
      ? t('detail.state.stale')
      : undefined
  return note === undefined ? <DataState kind={kind} /> : <DataState kind={kind}>{note}</DataState>
}

function MetricBody({ detail, range }: { detail: MetricDetail; range: Range }) {
  const name = useMetricName()(detail.key)
  const blocked = BLOCKED_STATES.has(detail.state)
  return (
    <Stack>
      <Crumbs name={name} />
      <header className="ov-metric__head">
        <h2 className="ov-metric__name">{name}</h2>
        {blocked ? null : <RangeSwitch metric={detail.key} range={range} />}
      </header>
      <MetricState detail={detail} />
      {blocked ? null : (
        <>
          <MetricSummary detail={detail} />
          <MetricChartCard detail={detail} />
          <MetricDays detail={detail} />
          <MethodCard detail={detail} />
        </>
      )}
    </Stack>
  )
}

export function MetricPage() {
  const { metric } = useMetricParams()
  const asked = useQuery().get('range')
  const range = isRange(asked) ? asked : DEFAULT_RANGE
  const resource = useResource(metric === null ? null : metricPath(metric, range), decodeMetricDetail)
  return <ResourceView resource={resource}>{(detail) => <MetricBody detail={detail} range={range} />}</ResourceView>
}
