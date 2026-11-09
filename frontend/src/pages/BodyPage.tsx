import { decodeMetrics } from '../api/overview'
import { decodeFindingsPage, findingsListPath } from '../api/findings'
import { useResource } from '../api/useResource'
import { BodyFindings } from '../components/body/BodyFindings'
import { BODY_KEYS } from '../components/body/bodyModel'
import { MissingTile } from '../components/body/MissingTile'
import { SegmentalCard } from '../components/body/SegmentalCard'
import { useMetricName } from '../components/metric/names'
import { AreaSection } from '../components/overview/AreaSection'
import { parseRange } from '../components/overview/overviewModel'
import { RangeSelector } from '../components/overview/RangeSelector'
import { Tile } from '../components/overview/Tile'
import { Stack } from '../components/ui/Card'
import { ResourceView } from '../components/ui/DataState'
import { useT } from '../i18n'
import { useQuery } from '../router/Router'
import '../styles/body.css'
import '../styles/findings.css'

// Body composition (docs/design/README.md, "Body composition"): the body tiles of the overview, the
// segmental arms and legs, and the body findings. The page is quiet by default: no colour for a
// side that differs, only the numbers and the states.

const FINDINGS = findingsListPath({ tab: 'live', tier: null, area: 'body' }, 0)

export function BodyPage() {
  const t = useT()
  const name = useMetricName()
  const range = parseRange(useQuery().get('range'))
  const metrics = useResource('/api/metrics', decodeMetrics)
  const findings = useResource(FINDINGS, decodeFindingsPage)
  return (
    <Stack gap="lg">
      <RangeSelector path="/body" range={range} />
      <ResourceView resource={metrics}>
        {(tiles) => (
          <>
            <AreaSection title={t('body.section.composition')}>
              {BODY_KEYS.map((key) => {
                const tile = tiles.find((x) => x.key === key)
                return tile === undefined ? <MissingTile key={key} name={name(key)} /> : <Tile key={key} tile={tile} range={range} name={name(key)} />
              })}
            </AreaSection>
            <AreaSection title={t('body.section.segments')}>
              <SegmentalCard tiles={tiles} />
            </AreaSection>
          </>
        )}
      </ResourceView>
      <section className="ov-area" aria-labelledby="body-findings">
        <h2 id="body-findings" className="ov-area__title">
          {t('body.section.findings')}
        </h2>
        <ResourceView resource={findings}>{(data) => <BodyFindings findings={data.findings} />}</ResourceView>
      </section>
    </Stack>
  )
}
