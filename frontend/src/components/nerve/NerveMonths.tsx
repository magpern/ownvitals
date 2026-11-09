import type { NerveMetricMonth, NerveMonth } from '../../api/nerve'
import { useT } from '../../i18n'
import { useFormat } from '../../i18n/useFormat'
import { useMetricName } from '../metric/names'
import { Card } from '../ui/Card'
import { hasReadings, monthMetric, NERVE_KEYS } from './nerveModel'

// Month by month (docs/design/README.md, "Nerve health"): newest first, for each metric the days
// with data, the median and the lowest and highest daily value. A month with no data reads "No
// readings", never zero. Descriptive only: no good or bad colour.

function Line({ name, row }: { name: string; row: NerveMetricMonth | null }) {
  const t = useT()
  const f = useFormat()
  if (row === null || row.daysWithData === 0 || row.median === null || row.minimum === null || row.maximum === null) {
    return null
  }
  return (
    <p className="ov-small">
      <span className="ov-muted">{name}</span>{' '}
      {t('nerve.month.line', {
        median: f.number(row.median, 0),
        low: f.number(row.minimum, 0),
        high: f.number(row.maximum, 0),
        days: row.daysWithData,
      })}
    </p>
  )
}

export function NerveMonths({ months }: { months: readonly NerveMonth[] }) {
  const t = useT()
  const name = useMetricName()
  return (
    <Card level={3} title={t('nerve.month.title')}>
      <ul className="ov-nmonths" aria-label={t('nerve.month.title')}>
        {months.map((m) => (
          <li key={m.month} className="ov-nmonth" data-month={m.month}>
            <span className="ov-nmonth__name">{m.month}</span>
            {hasReadings(m) ? (
              NERVE_KEYS.map((k) => <Line key={k} name={name(k)} row={monthMetric(m, k)} />)
            ) : (
              <p className="ov-small ov-muted">{t('nerve.month.none')}</p>
            )}
          </li>
        ))}
      </ul>
    </Card>
  )
}
