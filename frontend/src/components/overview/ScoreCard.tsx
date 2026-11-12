import type { HisScore } from '../../api/scores'
import { HIS_AREAS } from '../../api/scores'
import type { MessageKey } from '../../i18n/en'
import { useT } from '../../i18n'
import { Card } from '../ui/Card'
import { DataState } from '../ui/DataState'
import { TierGlyph } from '../ui/TierMark'
import { ScoreReadout } from './ScoreReadout'

// The health score card of the overview (docs/design/mockups/src/Dashboard.dc.html): the title with
// the "ownvitals 1.0" source tag, the value, and the areas on one line, "n/a" where an area has no
// score. The card is the mock-up's solid card; the backend lists the score only for accounts with
// the owner-only switch. With no score, or `not_set`, there is no card.

const AREA_LABEL: Record<(typeof HIS_AREAS)[number], MessageKey> = {
  body: 'overview.score.area.body',
  heart: 'overview.score.area.heart',
  activity: 'overview.score.area.activity',
  sleep: 'overview.score.area.sleep',
}

/** "his/1" is shown as "ownvitals 1.0": the number after the slash, with a minor part. */
function sourceTag(algoVersion: string): string {
  const version = algoVersion.split('/')[1] ?? ''
  return `ownvitals ${/^\d+$/.test(version) ? `${version}.0` : version}`.trim()
}

export function ScoreCard({ score }: { score: HisScore | null }) {
  const t = useT()
  if (score === null || score.status === 'not_set') return null
  const source = (
    <span className="ov-src">
      <TierGlyph tier="trend" />
      {sourceTag(score.algoVersion)}
    </span>
  )
  const areas = HIS_AREAS.map((area) => {
    const value = score.areas[area]
    return value === null
      ? t('overview.score.areaNa', { area: t(AREA_LABEL[area]) })
      : t('overview.score.areaValue', { area: t(AREA_LABEL[area]), value: String(value) })
  }).join(' · ')
  return (
    <Card level={3} title={t('overview.metric.healthScore')} aside={source}>
      {score.status === 'computed' && score.value !== null ? (
        <>
          <p>
            <ScoreReadout value={score.value} /> <span className="ov-small ov-muted">{t('overview.score.outOf')}</span>
          </p>
          <p className="ov-small ov-muted">{areas}</p>
        </>
      ) : (
        <DataState kind="not-available">{t('overview.score.detail')}</DataState>
      )}
    </Card>
  )
}
