import type { Estimate } from '../../api/ecg'
import { useT } from '../../i18n'
import { useFormat } from '../../i18n/useFormat'
import { Card } from '../ui/Card'
import { StatePill } from '../ui/DataState'
import { TierMark } from '../ui/TierMark'
import { GATE_KEYS, gateState, reasonKey } from './model'

// Experimental estimates (ADR 0006 section 9): rendered only when the API sent one, which it does
// only behind the owner-only switch. Boxed and dashed, labelled as ours, never in a Withings slot.
// Overlay states: gate passed (values), gate not passed (no values, the reasons in words) and no
// gate result ("not evaluated by ownvitals"). A reason code is never shown as text.

export function EstimateBox({ estimate }: { estimate: Estimate }) {
  const t = useT()
  const f = useFormat()
  const gate = gateState(estimate.gate)
  const values: [string, number | null, string][] =
    gate === 'pass'
      ? [
          [t('ecg.hr'), estimate.hrBpm, t('ecg.unit.bpm')],
          ['RMSSD', estimate.hrv?.rmssd ?? null, 'ms'],
          ['SDNN', estimate.hrv?.sdnn ?? null, 'ms'],
          ['pNN50', estimate.hrv?.pnn50 ?? null, '%'],
        ]
      : []
  const words = estimate.reasons.map((r) => reasonKey(r)).filter((k) => k !== null)
  return (
    <Card experimental title={<TierMark tier="experimental" />} level={2} aside={<StatePill tone={gate === 'pass' ? 'ok' : 'attn'}>{t(GATE_KEYS[gate])}</StatePill>}>
      <p className="ov-ecg__estimate-label">{t('ecg.estimate.label')}</p>
      {values.length > 0 ? (
        <dl className="ov-ecg__values">
          {values.map(([label, value, unit]) => (
            <div key={label}>
              <dt className="ov-muted">{label}</dt>
              <dd>
                <span className="ov-muted">{t('ecg.estimate.est')}</span>{' '}
                {value === null ? <span className="ov-muted">{t('ecg.notComputed')}</span> : <span className="ov-ecg__number ov-ecg__number--est">{f.number(value, 1)}</span>}{' '}
                <span className="ov-muted">{unit}</span>
              </dd>
            </div>
          ))}
        </dl>
      ) : (
        <p className="ov-muted">{gate === 'fail' ? t('ecg.estimate.hidden') : t('ecg.estimate.notEvaluated')}</p>
      )}
      {gate === 'fail' && words.length > 0 ? (
        <ul className="ov-ecg__reasons">
          {words.map((k) => (
            <li key={k}>{t(k)}</li>
          ))}
        </ul>
      ) : null}
      <p className="ov-muted">{t('ecg.estimate.note')}</p>
    </Card>
  )
}
