import { useEffect, useRef } from 'react'
import { HIS_AREA_KEYS, type HisCriterion } from '../../api/scoreDetail'
import type { HisScore } from '../../api/scores'
import { shortHash } from '../../lib/shortHash'
import { isMessageKey, useT } from '../../i18n'
import { Card } from '../ui/Card'
import { DataState, StatePill, type DataStateKind } from '../ui/DataState'
import { TierMark } from '../ui/TierMark'
import { Mono, Num } from '../ui/Value'
import { reasonKey, sourceTag } from './ScoreCard'
import { SCORE_LINK_ID, badBeyond, bandWord, hasBands, levelBounds, num, perfectRange } from './scorePanelModel'

// The Health score's "How it's computed" panel (issue 493, docs/design/README.md "Health score panel").
// It is the Reading panel's pattern (components/metric/ReadingPanel.tsx): a drawer with a header, a
// "calculated by ownvitals" card with a table, and the provenance of the stored row. Opened by the
// address (`?score=his`); Escape and the close button close it and the focus goes back to the link.

type PlainKind = Exclude<DataStateKind, 'importing' | 'insufficient-data'>
const STATE_KIND: Record<string, PlainKind> = {
  not_set: 'not-available',
  not_available: 'not-available',
  not_supported: 'not-supported',
}

type T = ReturnType<typeof useT>

function areaLabel(t: T, area: HisCriterion['area']): string {
  return area === null ? '' : t(`overview.score.area.${area}`)
}

function measureName(t: T, key: string): string {
  const k = `overview.score.criterion.${key}`
  return isMessageKey(k) ? t(k) : key
}

function bandText(t: T, c: HisCriterion): string {
  const parts = [`${t('overview.score.band.perfect')} ${perfectRange(c.low, c.high)}`]
  for (const level of ['good', 'average'] as const) {
    parts.push(`${t(`overview.score.band.${level}`)} ${levelBounds(level, c.low, c.high)}`)
  }
  const { below, above } = badBeyond(c.low, c.high)
  if (below !== null && above !== null) {
    parts.push(t('overview.score.band.badBoth', { below: num(below), above: num(above) }))
  } else if (below !== null) {
    parts.push(t('overview.score.band.badBelow', { below: num(below) }))
  } else if (above !== null) {
    parts.push(t('overview.score.band.badAbove', { above: num(above) }))
  }
  return parts.join(' · ')
}

function CriterionRow({ c }: { c: HisCriterion }) {
  const t = useT()
  const head = (
    <>
      <td>{areaLabel(t, c.area)}</td>
      <td>{measureName(t, c.key)}</td>
    </>
  )
  if (c.value === null) {
    return (
      <tr data-criterion={c.key} data-computed="false">
        {head}
        <td colSpan={3} className="ov-muted">
          {t(reasonKey(c.reason ?? 'fallback'))}
        </td>
      </tr>
    )
  }
  const word = bandWord(c.value, c.low, c.high)
  return (
    <tr data-criterion={c.key} data-computed="true">
      {head}
      <td className="ov-r">
        <Num>{num(c.value)}</Num> {c.unit}
      </td>
      <td>
        {word === null ? null : t(`overview.score.band.${word}`)}
        {hasBands(c) ? <div className="ov-small ov-muted">{bandText(t, c)}</div> : null}
      </td>
      <td className="ov-r">
        <Num>{c.points === null ? '' : num(c.points)}</Num>
      </td>
    </tr>
  )
}

function CriteriaTable({ criteria }: { criteria: HisCriterion[] }) {
  const t = useT()
  if (criteria.length === 0) return null
  return (
    <div className="ov-tbl-wrap">
      <table className="ov-tbl">
        <thead>
          <tr>
            <th scope="col">{t('overview.score.panel.area')}</th>
            <th scope="col">{t('overview.score.panel.measure')}</th>
            <th scope="col" className="ov-r">
              {t('overview.score.panel.value')}
            </th>
            <th scope="col">{t('overview.score.panel.band')}</th>
            <th scope="col" className="ov-r">
              {t('overview.score.panel.points')}
            </th>
          </tr>
        </thead>
        <tbody>
          {criteria.map((c) => (
            <CriterionRow key={c.key} c={c} />
          ))}
        </tbody>
      </table>
    </div>
  )
}

function AreaLines({ score }: { score: HisScore }) {
  const t = useT()
  return (
    <ul className="ov-metric__list">
      {HIS_AREA_KEYS.map((area) => {
        const value = score.areas[area]
        const name = t(`overview.score.area.${area}`)
        const text =
          value === null
            ? t('overview.score.areaReason', {
                area: t('overview.score.areaNa', { area: name }),
                reason: t(reasonKey(score.areaReasons[area] ?? 'fallback')),
              })
            : t('overview.score.areaValue', {
                area: name,
                value: String(value),
              })
        return <li key={area}>{text}</li>
      })}
    </ul>
  )
}

function Provenance({ score }: { score: HisScore }) {
  const t = useT()
  const d = score.detail
  const row = (label: string, value: string | null) =>
    value === null ? null : (
      <>
        <dt>{label}</dt>
        <dd>
          <Mono>{value}</Mono>
        </dd>
      </>
    )
  return (
    <>
      <div className="ov-card__head">
        <h4 className="ov-card__title ov-card__title--small">{t('detail.reading.provenance')}</h4>
        <StatePill tone={d.stale ? 'attn' : 'ok'} dot={d.stale ? 'attn' : 'ok'}>
          {t(d.stale ? 'overview.score.panel.stale' : 'overview.score.panel.current')}
        </StatePill>
      </div>
      <dl className="ov-metric__facts">
        {row('algo_version', score.algoVersion)}
        {row('table', d.table === null ? null : `${d.table.id} · ${d.table.version}`)}
        {row('table_sha256', d.table?.sha == null ? null : shortHash(d.table.sha))}
        {row('input_revision', d.inputRevision)}
        {row('input_fingerprint', d.inputFingerprint === null ? null : shortHash(d.inputFingerprint))}
        {row('computed', d.day)}
      </dl>
    </>
  )
}

function Body({ score }: { score: HisScore }) {
  const t = useT()
  const computed = score.status === 'computed' && score.value !== null
  const kind = STATE_KIND[score.status]
  const table = score.detail.table
  const sentence = t(reasonKey(score.reason ?? 'fallback'))
  return (
    <>
      {computed ? null : score.status === 'insufficient_data' ? (
        <DataState kind="insufficient-data">{sentence}</DataState>
      ) : kind === undefined ? null : (
        <DataState kind={kind}>{sentence}</DataState>
      )}
      <Card
        level={3}
        title={computed ? t('overview.score.panel.how', { value: String(score.value) }) : t('overview.score.panel.why')}
        aside={<span className="ov-small ov-muted">{t('overview.score.panel.calculated')}</span>}
      >
        <CriteriaTable criteria={score.detail.criteria} />
        <AreaLines score={score} />
        <p className="ov-small">{t('overview.score.panel.method.points')}</p>
        <p className="ov-small">{t('overview.score.panel.method.area')}</p>
        <p className="ov-small">{t('overview.score.panel.method.score')}</p>
      </Card>
      <h4 className="ov-card__title ov-card__title--small">{t('overview.score.panel.bandsTitle')}</h4>
      <p className="ov-small">
        {table === null
          ? t('overview.score.panel.bands.noTable')
          : t('overview.score.panel.bands', {
              source: table.source,
              id: table.id,
              version: table.version,
            })}
      </p>
      <p className="ov-small">{t('overview.score.panel.notMedical')}</p>
      <Provenance score={score} />
    </>
  )
}

export function ScorePanel({ score, onClose }: { score: HisScore; onClose: () => void }) {
  const t = useT()
  const ref = useRef<HTMLElement>(null)
  const close = useRef(onClose)
  useEffect(() => {
    close.current = onClose
  })
  useEffect(() => {
    ref.current?.focus()
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') close.current()
    }
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('keydown', onKey)
      document.getElementById(SCORE_LINK_ID)?.focus() // the focus goes back to the link
    }
  }, [])
  return (
    <aside
      ref={ref}
      tabIndex={-1}
      className="ov-metric__drawer ov-score-panel"
      aria-label={t('overview.metric.healthScore')}
    >
      <div className="ov-card__head">
        <h3 className="ov-card__title">{t('overview.metric.healthScore')}</h3>
        <button type="button" className="ov-btn" onClick={onClose}>
          {t('overview.score.panel.close')}
        </button>
      </div>
      <p className="ov-small">
        <span className="ov-src">{sourceTag(score.algoVersion)}</span> <TierMark tier="experimental" />
      </p>
      <Body score={score} />
    </aside>
  )
}
