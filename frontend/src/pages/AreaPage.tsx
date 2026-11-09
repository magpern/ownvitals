import { decodeMetrics } from '../api/overview'
import { useResource } from '../api/useResource'
import { AreaSection } from '../components/overview/AreaSection'
import { HEART_KEYS, bloodPressure, parseRange } from '../components/overview/overviewModel'
import { RangeSelector } from '../components/overview/RangeSelector'
import { StreamTile } from '../components/overview/StreamTile'
import { Tile } from '../components/overview/Tile'
import { useStatus } from '../components/shell/statusContext'
import { Stack } from '../components/ui/Card'
import { ResourceView } from '../components/ui/DataState'
import { useT } from '../i18n'
import { Link, useQuery } from '../router/Router'

// The area pages of the design's information architecture (/heart, /sleep, /activity): the
// overview's tiles for one area. Body and Nerve are their own pages.

export function HeartPage() {
  const t = useT()
  const range = parseRange(useQuery().get('range'))
  const metrics = useResource('/api/metrics', decodeMetrics)
  return (
    <Stack gap="lg">
      <RangeSelector path="/heart" range={range} />
      <ResourceView resource={metrics}>
        {(tiles) => {
          const bp = bloodPressure(tiles)
          return (
            <AreaSection title={t('overview.section.heart')} link={<Link to="/heart/ecg">{t('overview.link.ecg')}</Link>}>
              {HEART_KEYS.map((key) => {
                if (key === 'blood_pressure') return bp === null ? null : <Tile key={key} tile={bp.tile} range={range} valueText={bp.value} />
                const tile = tiles.find((x) => x.key === key)
                return tile === undefined ? null : <Tile key={key} tile={tile} range={range} />
              })}
            </AreaSection>
          )
        }}
      </ResourceView>
    </Stack>
  )
}

export function SleepPage() {
  const t = useT()
  return (
    <ResourceView resource={useStatus()}>
      {(status) => (
        <AreaSection title={t('overview.section.sleep')}>
          <StreamTile name="overview.stream.sleep_summary" status={status} stream="sleep_summary" />
          <StreamTile name="overview.stream.sleep_series" status={status} stream="sleep_series" />
        </AreaSection>
      )}
    </ResourceView>
  )
}

export function ActivityPage() {
  const t = useT()
  return (
    <ResourceView resource={useStatus()}>
      {(status) => (
        <AreaSection title={t('overview.section.activity')}>
          <StreamTile name="overview.stream.activity_daily" status={status} stream="activity_daily" />
          <StreamTile name="overview.stream.workouts" status={status} stream="workouts" />
        </AreaSection>
      )}
    </ResourceView>
  )
}
