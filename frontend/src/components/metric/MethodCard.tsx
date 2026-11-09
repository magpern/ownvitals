import type { MetricDetail } from '../../api/metrics'
import { methodMessage } from '../../api/codes'
import { useT } from '../../i18n'
import { Card } from '../ui/Card'
import { Mono } from '../ui/Value'

// "How this page is computed": the method in words (the API sends a code and numbers, the text is
// the catalogue's) and the version of the analysis the numbers came from.
export function MethodCard({ detail }: { detail: MetricDetail }) {
  const t = useT()
  const { method, provenance } = detail
  const versions = provenance.algoVersions.length > 0 ? provenance.algoVersions : method.algoVersions
  return (
    <Card title={t('detail.method.title')} level={3}>
      <p className="ov-small">
        {methodMessage(method.code, {
          band_k: method.bandK,
          band_window_days: method.bandWindowDays,
          morning_start: method.morningStart,
          morning_end: method.morningEnd,
        })}
      </p>
      <p className="ov-small ov-muted">
        {versions.length === 0 || provenance.inputRevision === null ? (
          t('detail.method.versionNone')
        ) : (
          <Mono>{t('detail.method.version', { version: versions.join(', '), revision: provenance.inputRevision })}</Mono>
        )}
        {provenance.recomputePending ? ` ${t('detail.method.pending')}` : ''}
      </p>
    </Card>
  )
}
