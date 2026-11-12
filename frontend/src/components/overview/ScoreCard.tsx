import type { HisScore } from '../../api/scores'
import { HIS_AREAS } from '../../api/scores'
import type { MessageKey } from '../../i18n/en'
import { isMessageKey, useT } from '../../i18n'
import { Card } from '../ui/Card'
import { DataState } from '../ui/DataState'
import { TierGlyph } from '../ui/TierMark'
import { ScoreReadout } from './ScoreReadout'

// The health score card of the overview (docs/design/mockups/src/Dashboard.dc.html): the title with
// the "ownvitals 1.0" source tag, the value, and the areas on one line, "n/a" where an area has no
// score. The score is experimental tier (ADR 0012 §1; README tier marks: dashed diamond, dashed
// borders), so the card is dashed and the tag carries the experimental glyph. With no score, or `not_set`, there is no card.

const AREA_LABEL: Record<(typeof HIS_AREAS)[number], MessageKey> = {
  body: 'overview.score.area.body',
  heart: 'overview.score.area.heart',
  activity: 'overview.score.area.activity',
  sleep: 'overview.score.area.sleep',
}

/** How an algorithm version is shown in the source tag; an unlisted one is shown as sent. */
const VERSION_LABEL: Record<string, string> = { 'his/1': '1.0' }

export function sourceTag(algoVersion: string): string {
  return `ownvitals ${VERSION_LABEL[algoVersion] ?? algoVersion}`
}

/** The wording of an area's reason code; a code the catalogue lacks reads generically. */
export function reasonKey(code: string): MessageKey {
  const key = `overview.score.reason.${code}`
  return isMessageKey(key) ? key : 'overview.score.reason.fallback'
}

export function ScoreCard({ score }: { score: HisScore | null }) {
  const t = useT()
  if (score === null || score.status === 'not_set') return null
  const source = (
    <span className="ov-src">
      <TierGlyph tier="experimental" />
      {sourceTag(score.algoVersion)}
    </span>
  )
  const areas = HIS_AREAS.map((area) => {
    const value = score.areas[area]
    return value === null
      ? t('overview.score.areaNa', { area: t(AREA_LABEL[area]) })
      : t('overview.score.areaValue', { area: t(AREA_LABEL[area]), value: String(value) })
  }).join(' · ')
  // Why an area has no score: a second line, one reason per such area (codes, worded here).
  const why = HIS_AREAS.filter((area) => score.areas[area] === null).map((area) =>
    t('overview.score.areaReason', {
      area: t(AREA_LABEL[area]),
      reason: t(reasonKey(score.areaReasons[area] ?? 'fallback')),
    }),
  )
  return (
    <Card level={3} experimental title={t('overview.metric.healthScore')} aside={source}>
      {score.status === 'computed' && score.value !== null ? (
        <>
          <p>
            <ScoreReadout value={score.value} /> <span className="ov-small ov-muted">{t('overview.score.outOf')}</span>
          </p>
          <p className="ov-small ov-muted">{areas}</p>
          {why.length > 0 && <p className="ov-small ov-muted">{why.join(' · ')}</p>}
        </>
      ) : (
        <DataState kind="not-available">{t('overview.score.detail')}</DataState>
      )}
    </Card>
  )
}
