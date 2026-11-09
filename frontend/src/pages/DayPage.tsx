import { decodeDay, dayPath, metricUrl, type DayView } from '../api/metrics'
import { useResource } from '../api/useResource'
import { AlsoRecorded, DayValueCard, ReadingsTable } from '../components/metric/DayReadings'
import { useMetricName } from '../components/metric/names'
import { ReadingPanel } from '../components/metric/ReadingPanel'
import { Stack } from '../components/ui/Card'
import { ResourceView } from '../components/ui/DataState'
import { useT } from '../i18n'
import { useFormat } from '../i18n/useFormat'
import { Link, useQuery } from '../router/Router'
import { useMetricParams } from './metricRoute'

// One day of a metric (the "day view"): the day's value, every reading behind it, and, when the
// address names one (`?reading=<id>`), that reading in a drawer beside them.

function DayBody({ view }: { view: DayView }) {
  const t = useT()
  const f = useFormat()
  const name = useMetricName()(view.key)
  const reading = useQuery().get('reading')
  return (
    <Stack>
      <nav className="ov-metric__crumbs" aria-label={t('detail.crumb')}>
        <Link to={metricUrl(view.key)}>{t('detail.day.back', { metric: name })}</Link>
      </nav>
      <header className="ov-metric__head">
        <h2 className="ov-metric__name">{t('detail.day.title', { metric: name, day: f.day(view.day) })}</h2>
      </header>
      <div className={reading === null ? 'ov-metric__day' : 'ov-metric__day ov-metric__day--open'}>
        <Stack>
          <DayValueCard view={view} />
          <ReadingsTable view={view} selected={reading} />
          <AlsoRecorded view={view} />
        </Stack>
        {reading === null ? null : <ReadingPanel id={reading} metric={view.key} day={view.day} />}
      </div>
    </Stack>
  )
}

export function DayPage() {
  const { metric, day } = useMetricParams()
  const resource = useResource(metric === null || day === null ? null : dayPath(metric, day), decodeDay)
  return <ResourceView resource={resource}>{(view) => <DayBody view={view} />}</ResourceView>
}
