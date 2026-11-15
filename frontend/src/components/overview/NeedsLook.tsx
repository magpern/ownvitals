import type { Finding, Findings } from '../../api/overview'
import { useT } from '../../i18n'
import { useFormat } from '../../i18n/useFormat'
import type { MessageKey } from '../../i18n/en'
import { isMessageKey } from '../../i18n/store'
import { Link } from '../../router/Router'
import { Card } from '../ui/Card'
import { TierMark, type Tier } from '../ui/TierMark'

// "Needs a look" (docs/design/mockups/src/Dashboard.dc.html): the live findings, quietly. A
// finding shows its tier (who said it), its name, whether it is new, how sure the rule is and the
// days it covers. Nothing here nudges: no count badge on the page, no "act now".

const TIERS: readonly string[] = ['device', 'trend', 'experimental', 'system']
const SHOWN = 3

function Row({ finding }: { finding: Finding }) {
  const t = useT()
  const f = useFormat()
  const nameKey = `health.rule.${finding.ruleId}.name`
  const level = ['low', 'medium', 'high'].includes(finding.confidence) ? finding.confidence : null
  return (
    <li className="ov-finding">
      <div className="ov-row ov-finding__head">
        {TIERS.includes(finding.tier) ? <TierMark tier={finding.tier as Tier} /> : null}
        {finding.isNew ? <span className="ov-finding__new">{t('overview.finding.new')}</span> : null}
      </div>
      <p className="ov-finding__name">{isMessageKey(nameKey) ? t(nameKey) : t('overview.finding.unnamed')}</p>
      <p className="ov-small ov-muted">
        {f.day(finding.windowStart, 'short')}
        {finding.windowEnd === finding.windowStart ? '' : `–${f.day(finding.windowEnd, 'short')}`}
        {level === null ? '' : ` · ${t('overview.finding.confidence', { level: t(`overview.confidence.${level}` as MessageKey) })}`}
      </p>
    </li>
  )
}

export function NeedsLook({ findings }: { findings: Findings }) {
  const t = useT()
  const f = useFormat()
  const count = `${f.number(findings.findings.length)}${findings.more ? '+' : ''}`
  return (
    <Card
      title={t('overview.section.needsLook')}
      aside={
        findings.findings.length > SHOWN || findings.more ? (
          <Link to="/findings">{t('overview.link.allFindings', { count })}</Link>
        ) : null
      }
    >
      {findings.findings.length === 0 ? (
        <p className="ov-muted">{t('overview.nothingToLook')}</p>
      ) : (
        <ul className="ov-findings">
          {findings.findings.slice(0, SHOWN).map((finding) => (
            <Row key={finding.id} finding={finding} />
          ))}
        </ul>
      )}
    </Card>
  )
}
