import type { Recording } from '../../api/ecg'
import { INTERVAL_KEYS } from '../../api/ecg'
import { useT } from '../../i18n'
import { useFormat } from '../../i18n/useFormat'
import { SourceMark } from '../ui/TierMark'
import { INTERVAL_LABELS } from './model'

/** Heart rate and the linked intervals, all "Reported by Withings"; a missing one is not 0. */
export function ReportedValues({ recording }: { recording: Recording }) {
  const t = useT()
  const f = useFormat()
  const cells: { key: string; label: string; value: number | null; unit: string }[] = [
    { key: 'hr', label: t('ecg.hr'), value: recording.heartRate, unit: t('ecg.unit.bpm') },
    ...INTERVAL_KEYS.map((key) => ({ key, label: t(INTERVAL_LABELS[key]), value: recording.intervals[key] ?? null, unit: 'ms' })),
  ]
  return (
    <>
      <dl className="ov-ecg__values">
        {cells.map((c) => (
          <div key={c.key}>
            <dt className="ov-muted">{c.label}</dt>
            <dd>
              {c.value === null ? (
                <span className="ov-muted">{t('ecg.notReported')}</span>
              ) : (
                <>
                  <span className="ov-ecg__number">{f.number(c.value)}</span> <span className="ov-muted">{c.unit}</span>
                </>
              )}
            </dd>
          </div>
        ))}
      </dl>
      <p className="ov-muted ov-ecg__source">
        <SourceMark source="withings" /> {t('ecg.reportedBy')}
      </p>
    </>
  )
}
