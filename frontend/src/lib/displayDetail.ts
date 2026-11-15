import type { MetricDetail } from '../api/metrics'
import { convertQuantity, type DisplaySettings } from './display'

/**
 * A metric in the unit the person chose (issue 305). Values are converted; slopes and shifts are
 * differences, so they only scale (a Fahrenheit offset does not apply to them). The flags and the
 * analysis are the API's and are not touched. Pure.
 */
export function convertDetail(detail: MetricDetail, settings: DisplaySettings): MetricDetail {
  const unit = convertQuantity(settings, 0, detail.unit).unit
  if (unit === detail.unit) return detail
  const value = (v: number) => convertQuantity(settings, v, detail.unit).value
  const scale = value(1) - value(0)
  const { band, latest, trend, changePoints } = detail
  return {
    ...detail,
    unit,
    latest: latest === null ? null : { ...latest, value: value(latest.value) },
    series: detail.series.map((p) => ({ ...p, value: p.value === null ? null : value(p.value) })),
    band: band === null ? null : { ...band, median: value(band.median), low: value(band.low), high: value(band.high) },
    trend: { ...trend, slopePerDay: trend.slopePerDay === null ? null : trend.slopePerDay * scale },
    changePoints: { ...changePoints, points: changePoints.points.map((p) => ({ ...p, shift: p.shift * scale })) },
  }
}
