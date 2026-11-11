import { useDisplay } from '../display/DisplayContext'
import { metricsPath } from '../api/metrics'
import { decodeFindings, decodeMetrics } from '../api/overview'
import { useResource } from '../api/useResource'
import { AreaSection } from '../components/overview/AreaSection'
import { NeedsLook } from '../components/overview/NeedsLook'
import { BODY_KEYS, HEART_KEYS, NERVE_KEY, bloodPressure, parseRange } from '../components/overview/overviewModel'
import { RangeSelector } from '../components/overview/RangeSelector'
import { StreamTile } from '../components/overview/StreamTile'
import { Tile } from '../components/overview/Tile'
import { useStatus } from '../components/shell/statusContext'
import { Card, Stack } from '../components/ui/Card'
import { DataState, ResourceView } from '../components/ui/DataState'
import { useT } from '../i18n'
import { Link, useQuery } from '../router/Router'

// The overview (docs/design/mockups/src/Dashboard.dc.html): what needs a look, then the tiles by
// area. A tile that cannot show a number says why (the data states); the page is quiet by
// default: no streaks, no rings, no nudges.

export function OverviewPage() {
  const t = useT()
  const asked = useQuery().get('range')
  const { chartRange, settled } = useDisplay()
  const range = parseRange(asked, chartRange)
  // With no range in the address the saved one is used, so the read waits for the profile.
  const metrics = useResource(asked !== null || settled ? metricsPath(range) : null, decodeMetrics)
  const findings = useResource('/api/findings?tab=live&limit=20', decodeFindings)
  const status = useStatus()
  return (
    <Stack gap="lg">
      <RangeSelector path="/overview" range={range} />
      <ResourceView resource={findings}>{(data) => <NeedsLook findings={data} />}</ResourceView>
      <ResourceView resource={metrics}>
        {(tiles) => {
          const bp = bloodPressure(tiles)
          const by = (key: string) => tiles.find((x) => x.key === key)
          const nerve = by(NERVE_KEY)
          return (
            <>
              <AreaSection title={t('overview.section.body')} link={<Link to="/body">{t('overview.link.bodyComposition')}</Link>}>
                {BODY_KEYS.map((key, i) => {
                  const tile = by(key)
                  return tile === undefined ? null : <Tile key={key} tile={tile} range={range} index={i} />
                })}
              </AreaSection>
              <AreaSection title={t('overview.section.heart')} link={<Link to="/heart/ecg">{t('overview.link.ecg')}</Link>}>
                {HEART_KEYS.map((key, i) => {
                  if (key === 'blood_pressure') return bp === null ? null : <Tile key={key} tile={bp.tile} range={range} valueText={bp.value} index={i} />
                  const tile = by(key)
                  return tile === undefined ? null : <Tile key={key} tile={tile} range={range} index={i} />
                })}
              </AreaSection>
              <AreaSection title={t('overview.section.more')}>
                {nerve === undefined ? null : <Tile tile={nerve} range={range} />}
                <ResourceView resource={status}>
                  {(s) => (
                    <>
                      <StreamTile name="overview.metric.sleep" status={s} stream="sleep_summary" />
                      <StreamTile name="overview.metric.activity" status={s} stream="activity_daily" />
                    </>
                  )}
                </ResourceView>
                <Card level={3} title={t('overview.metric.healthScore')}>
                  <DataState kind="not-available">{t('overview.score.detail')}</DataState>
                </Card>
              </AreaSection>
            </>
          )
        }}
      </ResourceView>
    </Stack>
  )
}
