import type { ReactNode } from 'react'
import { decodeFindingDetail, findingPath, type FindingDetail as Detail } from '../../api/findings'
import { dayUrl, metricUrl } from '../../api/metrics'
import { useResource } from '../../api/useResource'
import { useT, type MessageKey } from '../../i18n'
import { interpolate } from '../../i18n/translate'
import { useFormat } from '../../i18n/useFormat'
import { Link } from '../../router/Router'
import { useMetricName } from '../metric/names'
import { Card } from '../ui/Card'
import { ResourceView } from '../ui/DataState'
import { TierMark } from '../ui/TierMark'
import { EvidenceChart } from './EvidenceChart'
import { evidenceParams } from './model'
import { knownTier, ruleKey } from './text'

// The chosen finding (docs/design/mockups/src/Findings.dc.html): its rule, its name, the evidence
// chart, "why you're seeing this" worded from the evidence, "what it isn't", the data quality, how
// sure the rule is and where the numbers come from, and links to the readings.

function Row({ label, children }: { label: string; children: ReactNode }) {
  return (
    <>
      <dt>{label}</dt>
      <dd>{children}</dd>
    </>
  )
}

function DetailBody({ detail, today }: { detail: Detail; today: string }) {
  const t = useT()
  const f = useFormat()
  const metricName = useMetricName()
  const { finding } = detail
  const tier = knownTier(finding.tier)
  const nameKey = ruleKey(finding.ruleId, 'name')
  const textKey = ruleKey(finding.ruleId, 'text')
  const params = Object.fromEntries(Object.entries(evidenceParams(detail.evidence)).map(([k, v]) => [k, f.number(v, Number.isInteger(v) ? 0 : 1)]))
  const filled = textKey === null ? null : interpolate(t(textKey), params)
  // A text whose numbers the evidence does not hold is never shown half-filled.
  const why = filled !== null && !/\{[A-Za-z]/.test(filled) ? filled : null
  const metric = finding.metricKeys[0] ?? null
  const q = finding.quality
  const level = ['low', 'medium', 'high'].includes(finding.confidence) ? finding.confidence : null
  const origin = finding.origin === 'backfill' ? t('findings.detail.origin.backfill') : t('findings.detail.origin.live')
  return (
    <Card>
      <div className="ov-fdetail">
        <div className="ov-fdetail__rule">
          {tier === null ? null : <TierMark tier={tier} />}
          <span className="ov-muted">{t('findings.detail.rule', { rule: finding.ruleId, version: finding.ruleVersion, origin })}</span>
        </div>
        <h2 className="ov-fdetail__title">{nameKey === null ? t('overview.finding.unnamed') : t(nameKey)}</h2>
        {finding.notEvaluated ? <p className="ov-muted">{t('findings.detail.notEvaluated')}</p> : null}
        {metric === null ? null : (
          <EvidenceChart metric={metric} windowStart={finding.windowStart} windowEnd={finding.windowEnd} today={today} />
        )}
        {why === null ? null : (
          <section>
            <h3 className="ov-fdetail__sub">{t('findings.detail.why')}</h3>
            <p className="ov-muted">{why}</p>
          </section>
        )}
        {tier === null ? null : (
          <section>
            <h3 className="ov-fdetail__sub">{t('findings.detail.isnt')}</h3>
            <p className="ov-muted">{t(`findings.isnt.${tier}` as MessageKey)}</p>
          </section>
        )}
        <dl className="ov-kv">
          <Row label={t('findings.detail.quality')}>
            {q.nDays !== null && q.windowDays !== null && q.coverage !== null && q.longestGapDays !== null
              ? t('findings.detail.quality.value', {
                  have: f.number(q.nDays),
                  total: f.number(q.windowDays),
                  coverage: f.number(Math.round(q.coverage * 100)),
                  gap: f.number(q.longestGapDays),
                })
              : t('findings.detail.quality.none')}
          </Row>
          <Row label={t('findings.detail.confidence')}>
            {level === null ? t('findings.detail.quality.none') : t(`overview.confidence.${level}` as MessageKey)}
          </Row>
          <Row label={t('findings.detail.sources')}>
            {tier === 'device' ? t('findings.detail.sources.withings') : t('findings.detail.sources.ownvitals')}
            {finding.metricKeys.length > 0 ? ` · ${finding.metricKeys.map(metricName).join(', ')}` : ''}
          </Row>
        </dl>
        {metric === null ? null : (
          <div className="ov-card__actions">
            <Link to={metricUrl(metric)} className="ov-btn">
              {t('findings.detail.open', { metric: metricName(metric) })}
            </Link>
            {finding.referenceDay === null ? null : (
              <Link to={dayUrl(metric, finding.referenceDay)} className="ov-btn">
                {t('findings.detail.day', { day: f.day(finding.referenceDay, 'short') })}
              </Link>
            )}
          </div>
        )}
      </div>
    </Card>
  )
}

export function FindingDetail({ id, today }: { id: string; today: string }) {
  const resource = useResource(findingPath(id), decodeFindingDetail)
  return <ResourceView resource={resource}>{(detail) => <DetailBody detail={detail} today={today} />}</ResourceView>
}

