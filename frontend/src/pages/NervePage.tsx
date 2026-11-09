import { decodeMetrics } from '../api/overview'
import { decodeFindingsPage, findingsListPath } from '../api/findings'
import { decodeNerveSummary, NERVE_SUMMARY_PATH } from '../api/nerve'
import { useResource } from '../api/useResource'
import { useMetricName } from '../components/metric/names'
import { FootCard } from '../components/nerve/FootCard'
import { NerveFindings } from '../components/nerve/NerveFindings'
import { NerveMonths } from '../components/nerve/NerveMonths'
import { NERVE_KEYS } from '../components/nerve/nerveModel'
import { MissingTile } from '../components/body/MissingTile'
import { AreaSection } from '../components/overview/AreaSection'
import { parseRange } from '../components/overview/overviewModel'
import { RangeSelector } from '../components/overview/RangeSelector'
import { Tile } from '../components/overview/Tile'
import { Stack } from '../components/ui/Card'
import { ResourceView } from '../components/ui/DataState'
import { useT } from '../i18n'
import { useQuery } from '../router/Router'
import '../styles/findings.css'
import '../styles/nerve.css'

// Nerve health (docs/design/README.md, "Nerve health"): the three nerve tiles of the overview, the
// per-foot card, the monthly summary and the nerve findings. Quiet by default: no good or bad
// colour, only the numbers and the states. No ESC literature bands (N4 is not in v1).

const FINDINGS = findingsListPath({ tab: 'live', tier: null, area: 'nerve' }, 0)

export function NervePage() {
  const t = useT()
  const name = useMetricName()
  const range = parseRange(useQuery().get('range'))
  const metrics = useResource('/api/metrics', decodeMetrics)
  const summary = useResource(NERVE_SUMMARY_PATH, decodeNerveSummary)
  const findings = useResource(FINDINGS, decodeFindingsPage)
  return (
    <Stack gap="lg">
      <RangeSelector path="/nerve" range={range} />
      <ResourceView resource={metrics}>
        {(tiles) => (
          <AreaSection title={t('nerve.section.measures')}>
            {NERVE_KEYS.map((key) => {
              const tile = tiles.find((x) => x.key === key)
              return tile === undefined ? <MissingTile key={key} name={name(key)} /> : <Tile key={key} tile={tile} range={range} name={name(key)} />
            })}
            <FootCard />
          </AreaSection>
        )}
      </ResourceView>
      <section className="ov-area" aria-labelledby="nerve-months">
        <h2 id="nerve-months" className="ov-area__title">
          {t('nerve.section.months')}
        </h2>
        <ResourceView resource={summary}>{(months) => <NerveMonths months={months} />}</ResourceView>
      </section>
      <section className="ov-area" aria-labelledby="nerve-findings">
        <h2 id="nerve-findings" className="ov-area__title">
          {t('nerve.section.findings')}
        </h2>
        <ResourceView resource={findings}>{(data) => <NerveFindings findings={data.findings} />}</ResourceView>
      </section>
    </Stack>
  )
}
