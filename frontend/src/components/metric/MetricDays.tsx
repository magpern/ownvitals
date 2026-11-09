import { dayUrl, type MetricDetail } from '../../api/metrics'
import { plural, useT } from '../../i18n'
import { useFormat } from '../../i18n/useFormat'
import { Link } from '../../router/Router'
import { Card } from '../ui/Card'
import { Num } from '../ui/Value'
import { recentValues } from './metricData'

// Where a number can be traced: the newest days of the window, each a link to the day's readings,
// and the days that have unconfirmed readings (shown on the day, left out of the line).

const SHOWN = 14

export function MetricDays({ detail }: { detail: MetricDetail }) {
  const t = useT()
  const f = useFormat()
  const days = recentValues(detail.series, SHOWN)
  const unconfirmed = [...detail.unconfirmed].reverse()
  return (
    <Card title={t('detail.days.title')} level={3}>
      <p className="ov-small ov-muted">{t('detail.days.hint')}</p>
      {days.length === 0 ? null : (
        <div className="ov-tbl-wrap">
          <table className="ov-tbl">
            <thead>
              <tr>
                <th scope="col">{t('detail.days.day')}</th>
                <th scope="col" className="ov-r">
                  {t('detail.days.value')}
                </th>
                <th scope="col" className="ov-r">
                  {t('detail.days.readings')}
                </th>
                <th scope="col">
                  <span className="ov-sr">{t('detail.days.open')}</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {days.map((point) => (
                <tr key={point.day}>
                  <td>{f.day(point.day)}</td>
                  <td className="ov-r">
                    <Num>{f.number(point.value, 1)}</Num> {detail.unit}
                  </td>
                  <td className="ov-r">
                    <Num>{f.number(point.n)}</Num>
                  </td>
                  <td>
                    <Link to={dayUrl(detail.key, point.day)} aria-label={`${t('detail.days.open')}: ${f.day(point.day)}`}>
                      {t('detail.days.open')}
                    </Link>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {unconfirmed.length === 0 ? null : (
        <section className="ov-metric__unconfirmed" aria-label={t('detail.days.unconfirmedTitle')}>
          <h4 className="ov-card__title ov-card__title--small">{t('detail.days.unconfirmedTitle')}</h4>
          <p className="ov-small ov-muted">{t('detail.days.unconfirmedNote')}</p>
          <ul className="ov-metric__list">
            {unconfirmed.map((day) => (
              <li key={day.day}>
                <Link to={dayUrl(detail.key, day.day)}>{f.day(day.day)}</Link>{' '}
                <span className="ov-muted">{plural('detail.days.unconfirmedCount', day.n)}</span>
              </li>
            ))}
          </ul>
        </section>
      )}
    </Card>
  )
}
