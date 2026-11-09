import { useMemo } from 'react'
import { decodeMetricDetail, metricPath, type MetricDetail } from '../../api/metrics'
import { useResource } from '../../api/useResource'
import { useT } from '../../i18n'
import { useFormat } from '../../i18n/useFormat'
import { ChartLegend, type LegendKey } from '../charts/ChartLegend'
import { buildChartData } from '../charts/seriesData'
import { TimeSeriesChart } from '../charts/TimeSeriesChart'
import { chartPoints } from '../metric/metricData'
import { useMetricName } from '../metric/names'
import { ResourceView } from '../ui/DataState'
import { chartRange, inStretch } from './model'

// The evidence: the metric's daily series zoomed to the days around the finding, with the
// typical range. The days are the metric screen's own series, so the chart never shows a value
// the metric page does not.

export function EvidenceChart({ metric, windowStart, windowEnd, today }: { metric: string; windowStart: string; windowEnd: string; today: string }) {
  const t = useT()
  const f = useFormat()
  const name = useMetricName()(metric)
  const resource = useResource(metricPath(metric, chartRange(windowStart, today)), decodeMetricDetail)
  return (
    <ResourceView resource={resource}>
      {(detail) => <Drawn detail={detail} name={name} windowStart={windowStart} windowEnd={windowEnd} format={(v) => f.number(v, 1)} none={t('findings.detail.chart.none')} />}
    </ResourceView>
  )
}

function Drawn({
  detail,
  name,
  windowStart,
  windowEnd,
  format,
  none,
}: {
  detail: MetricDetail
  name: string
  windowStart: string
  windowEnd: string
  format: (value: number) => string
  none: string
}) {
  const t = useT()
  const series = useMemo(() => inStretch(detail.series, windowStart, windowEnd), [detail, windowStart, windowEnd])
  const data = useMemo(() => buildChartData(chartPoints({ series, band: detail.band })), [series, detail.band])
  const days = series.filter((p) => p.value !== null).length
  if (days === 0) return <p className="ov-muted">{none}</p>
  const legend: LegendKey[] = detail.band === null ? ['smoothed', 'daily', 'gap'] : ['band', 'smoothed', 'daily', 'gap']
  return (
    <div className="ov-fchart">
      <TimeSeriesChart data={data} unit={detail.unit} format={format} height={220} label={t('findings.detail.chart.label', { metric: name, days })} />
      <ChartLegend items={legend} />
    </div>
  )
}
