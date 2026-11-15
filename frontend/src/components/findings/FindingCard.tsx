import type { Finding } from '../../api/findings'
import { isMessageKey, useT, type MessageKey } from '../../i18n'
import { useFormat } from '../../i18n/useFormat'
import { Link } from '../../router/Router'
import { TierMark } from '../ui/TierMark'
import { knownTier, ruleKey } from './text'

// One finding in the list (docs/design/mockups/src/Findings.dc.html): who said it (tier and area),
// whether it is new, its name, its window, how sure the rule is and how much data stood behind it.
// A device finding says whose result it is; a finding the signal check did not pass says so.

export function FindingCard({ finding, to, selected }: { finding: Finding; to: string; selected: boolean }) {
  const t = useT()
  const f = useFormat()
  const tier = knownTier(finding.tier)
  const nameKey = ruleKey(finding.ruleId, 'name')
  const areaKey = `findings.area.${finding.area ?? ''}`
  const level = ['low', 'medium', 'high'].includes(finding.confidence) ? finding.confidence : null
  const { nDays, windowDays } = finding.quality
  const state = finding.origin === 'backfill' ? t('findings.card.history') : finding.isNew ? t('findings.card.new') : t('findings.card.seen')
  return (
    <li>
      <Link to={to} className="ov-fcard" data-tier={tier ?? undefined} data-selected={selected ? 'true' : undefined} aria-current={selected ? 'true' : undefined}>
        <span className="ov-fcard__head">
          <span className="ov-fcard__tags">
            {tier === null ? null : <TierMark tier={tier} />}
            {isMessageKey(areaKey) && tier !== 'system' ? <span className="ov-muted">{t(areaKey)}</span> : null}
            {tier === 'experimental' ? <span className="ov-muted">{t('findings.card.experimental')}</span> : null}
          </span>
          <span className="ov-fcard__state" data-new={finding.isNew && finding.origin !== 'backfill' ? 'true' : undefined}>
            {state}
          </span>
        </span>
        <span className="ov-fcard__name">{nameKey === null ? t('overview.finding.unnamed') : t(nameKey)}</span>
        <span className="ov-fcard__meta ov-muted">
          {f.day(finding.windowStart, 'short')}
          {finding.windowEnd === finding.windowStart ? '' : `–${f.day(finding.windowEnd, 'short')}`}
          {level === null ? '' : ` · ${t('overview.finding.confidence', { level: t(`overview.confidence.${level}` as MessageKey) })}`}
        </span>
        <span className="ov-fcard__meta ov-muted">
          {tier === 'device' ? t('findings.card.withings') : null}
          {finding.notEvaluated ? ` ${t('findings.card.notEvaluated')}` : null}
          {tier !== 'device' && nDays !== null && windowDays !== null
            ? t('findings.card.quality', { have: f.number(nDays), total: f.number(windowDays) })
            : null}
        </span>
      </Link>
    </li>
  )
}
