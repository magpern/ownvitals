import { decodeRecordingDetail, ecgPath, ecgPdfPath, type RecordingDetail } from '../api/ecg'
import { useResource } from '../api/useResource'
import { Classification } from '../components/ecg/Classification'
import { EcgCanvas } from '../components/ecg/EcgCanvas'
import { EstimateBox } from '../components/ecg/EstimateBox'
import { ReportedValues } from '../components/ecg/ReportedValues'
import { Card, Stack } from '../components/ui/Card'
import { ResourceView } from '../components/ui/DataState'
import { TierMark } from '../components/ui/TierMark'
import { useT } from '../i18n'
import { useFormat } from '../i18n/useFormat'
import { Link, useRouter } from '../router/Router'
import { matchAppRoute } from '../router/appRoutes'
import '../styles/ecg.css'

// One ECG (docs/design/mockups/src/ECG.dc.html): Withings' result and reported values, the
// calibrated strip, the experimental estimates only when the API sent them, and the PDF.

function Detail({ detail }: { detail: RecordingDetail }) {
  const t = useT()
  const f = useFormat()
  const { recording, signal, estimate } = detail
  const lead = signal?.leads[0]
  const seconds = signal !== null && lead !== undefined ? lead.length / signal.samplingHz : null
  const device =
    recording.deviceName ?? (recording.deviceModelId === null ? t('ecg.notReported') : t('ecg.deviceModel', { id: String(recording.deviceModelId) }))
  return (
    <Stack gap="lg">
      <div className="ov-ecg__head">
        <p className="ov-muted">
          {[
            f.dateTime(recording.recordedAt, recording.sourceTz),
            device,
            seconds === null || signal === null
              ? t('ecg.noSignal')
              : t('ecg.meta', { seconds: f.number(seconds), hz: f.number(signal.samplingHz) }),
            t('ecg.lead'),
          ].join(' · ')}
        </p>
        {signal === null ? null : (
          <a className="ov-btn" href={ecgPdfPath(recording.id)} download>
            {t('ecg.pdf')}
          </a>
        )}
      </div>
      <Card
        title={t('ecg.result.title')}
        aside={<TierMark tier="device" />}
      >
        <Classification afib={recording.afib} details={recording.afibDetails} />
        <ReportedValues recording={recording} />
      </Card>
      {signal !== null && lead !== undefined ? (
        <Card title={t('ecg.strip.title')}>
          <EcgCanvas microvolts={lead} rate={signal.samplingHz} markers={estimate?.rpeaks ?? null} />
          {signal.leads.length > 1 ? <p className="ov-muted">{t('ecg.moreLeads', { count: String(signal.leads.length) })}</p> : null}
        </Card>
      ) : (
        <Card title={t('ecg.strip.title')}>
          <p className="ov-muted">{t('ecg.noSignal.note')}</p>
        </Card>
      )}
      {estimate === null ? null : <EstimateBox estimate={estimate} />}
      <p className="ov-muted">{t('ecg.notMedical')}</p>
    </Stack>
  )
}

export function EcgPage() {
  const t = useT()
  const { pathname } = useRouter()
  const id = matchAppRoute(pathname)?.params.id ?? null
  const detail = useResource(id === null ? null : ecgPath(id), decodeRecordingDetail)
  return (
    <Stack>
      <nav className="ov-metric__crumbs" aria-label={t('detail.crumb')}>
        <Link to="/heart/ecg">{t('shell.route.ecg-list.title')}</Link>
      </nav>
      <ResourceView resource={detail}>{(d) => <Detail detail={d} />}</ResourceView>
    </Stack>
  )
}
