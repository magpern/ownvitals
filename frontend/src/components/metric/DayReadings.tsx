import { dayUrl } from '../../api/metrics'
import type { DayReading, DayView } from '../../api/metricDay'
import { isMessageKey, plural, useT } from '../../i18n'
import { useFormat } from '../../i18n/useFormat'
import { Link } from '../../router/Router'
import { Notice } from '../forms'
import { Card } from '../ui/Card'
import { StatePill } from '../ui/DataState'
import { Num, Value } from '../ui/Value'
import { useMetricName } from './names'

// One day of one metric: the day's value and every reading behind it. A reading the analysis did
// not use (unconfirmed, implausible, duplicate) is listed and marked, never hidden and never part
// of the day's value.

export const isUnconfirmed = (reading: DayReading): boolean => reading.qualityFlags.includes('unconfirmed')

function Status({ reading }: { reading: DayReading }) {
  const t = useT()
  if (reading.used) return <StatePill tone="ok">{t('detail.day.used')}</StatePill>
  if (isUnconfirmed(reading)) return <StatePill tone="attn">{t('detail.day.unconfirmed')}</StatePill>
  return <StatePill tone="off">{t('detail.day.excluded')}</StatePill>
}

function flagText(t: ReturnType<typeof useT>, flag: string): string {
  const key = `detail.flag.${flag}`
  return isMessageKey(key) ? t(key) : flag
}

export function DayValueCard({ view }: { view: DayView }) {
  const t = useT()
  const f = useFormat()
  const { daily } = view
  return (
    <Card title={t('detail.day.daily')} level={3}>
      {daily === null ? (
        <p className="ov-muted">{t('detail.day.dailyNone')}</p>
      ) : (
        <>
          <Value size="large" value={f.number(daily.value, 1)} unit={view.unit} />
          <p className="ov-small ov-muted">
            {`${t(daily.method === 'morning' ? 'detail.day.method.morning' : 'detail.day.method.median')} · ${t('detail.day.spread', { min: f.number(daily.minimum, 1), max: f.number(daily.maximum, 1) })}`}
          </p>
        </>
      )}
    </Card>
  )
}

export function ReadingsTable({ view, selected }: { view: DayView; selected: string | null }) {
  const t = useT()
  const f = useFormat()
  const hasUnconfirmed = view.readings.some(isUnconfirmed)
  return (
    <Card title={t('detail.day.readings')} level={3}>
      {view.readings.length === 0 ? (
        <p className="ov-muted">{t('detail.day.none')}</p>
      ) : (
        <div className="ov-tbl-wrap">
          <table className="ov-tbl">
            <thead>
              <tr>
                <th scope="col">{t('detail.day.time')}</th>
                <th scope="col" className="ov-r">
                  {t('detail.reading.value')}
                </th>
                <th scope="col">{t('detail.day.status')}</th>
                <th scope="col">
                  <span className="ov-sr">{t('detail.day.reading')}</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {view.readings.map((reading) => (
                <tr
                  key={reading.id}
                  className={reading.id === selected ? 'ov-tbl__sel' : undefined}
                  data-used={reading.used ? 'true' : 'false'}
                  data-unconfirmed={isUnconfirmed(reading) ? 'true' : undefined}
                >
                  <td>
                    <Num>{reading.localTime}</Num>
                  </td>
                  <td className="ov-r">
                    <Num>{f.number(reading.value, 1)}</Num> {view.unit}
                  </td>
                  <td>
                    <Status reading={reading} />
                    {reading.used || isUnconfirmed(reading) || reading.qualityFlags.length === 0 ? null : (
                      <span className="ov-small ov-muted"> {reading.qualityFlags.map((flag) => flagText(t, flag)).join(', ')}</span>
                    )}
                  </td>
                  <td>
                    <Link
                      to={dayUrl(view.key, view.day, reading.id)}
                      aria-label={`${t('detail.day.reading')} ${reading.localTime}`}
                    >
                      {t('detail.day.open')}
                    </Link>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {hasUnconfirmed ? <Notice>{t('detail.day.unconfirmedNote')}</Notice> : null}
    </Card>
  )
}

export function AlsoRecorded({ view }: { view: DayView }) {
  const t = useT()
  const name = useMetricName()
  if (view.alsoRecorded.length === 0) return null
  return (
    <Card title={t('detail.day.also')} level={3}>
      <ul className="ov-metric__list">
        {view.alsoRecorded.map((other) => (
          <li key={other.metricKey}>
            <Link to={dayUrl(other.metricKey, view.day)}>{name(other.metricKey)}</Link>{' '}
            <span className="ov-muted">{plural('detail.day.alsoCount', other.nReadings)}</span>
          </li>
        ))}
      </ul>
    </Card>
  )
}
