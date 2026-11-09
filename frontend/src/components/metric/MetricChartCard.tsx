import { useMemo } from 'react'
import type { MetricDetail } from '../../api/metrics'
import { useFormat } from '../../i18n/useFormat'
import { useT } from '../../i18n'
import { ChartLegend, type LegendKey } from '../charts/ChartLegend'
import { buildChartData } from '../charts/seriesData'
import { TimeSeriesChart } from '../charts/TimeSeriesChart'
import { Card } from '../ui/Card'
import { chartPoints, daySeconds, daysWithValue } from './metricData'
import { useMetricName } from './names'

// The daily chart. Only confirmed daily values are drawn: an unconfirmed reading is never part of
// the line (the API sends the daily values without them), and the days that have one are listed
// under the chart with a link to the day, where each reading is marked and left out.

export function MetricChartCard({ detail }: { detail: MetricDetail }) {
  const t = useT()
  const f = useFormat()
  const name = useMetricName()(detail.key)
  const days = daysWithValue(detail.series)
  const data = useMemo(() => {
    const changePoints = detail.changePoints.points.map((point) => ({
      t: daySeconds(point.day),
      label: t('detail.chart.change', { day: f.day(point.day, 'short') }),
    }))
    return buildChartData(chartPoints(detail), { changePoints })
    // The language changes the labels of the change points.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [detail, f.locale])
  const legend: LegendKey[] = ['smoothed', 'daily', 'gap']
  if (detail.band !== null) legend.unshift('band')
  if (detail.changePoints.points.length > 0) legend.push('change')
  return (
    <Card title={t('detail.chart.title', { metric: name })} level={3}>
      {days === 0 ? (
        <p className="ov-muted">{t('detail.chart.none')}</p>
      ) : (
        <>
          <TimeSeriesChart
            data={data}
            unit={detail.unit}
            format={(value) => f.number(value, 1)}
            label={t('detail.chart.label', { metric: name, days, unit: detail.unit })}
          />
          <ChartLegend items={legend} />
        </>
      )}
    </Card>
  )
}
