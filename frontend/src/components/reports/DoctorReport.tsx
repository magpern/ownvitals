import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  MAX_ECG,
  downloadDoctorReport,
  listDoctorReports,
  requestDoctorReport,
  type DoctorItem,
} from '../../api/doctorReport'
import { decodeRecordingPage, ecgListPath } from '../../api/ecg'
import { errorMessage } from '../../api/messages'
import { decodeMetrics } from '../../api/overview'
import { metricsPath } from '../../api/metrics'
import { useResource } from '../../api/useResource'
import { useT } from '../../i18n'
import { useFormat } from '../../i18n/useFormat'
import { Chip, Row } from '../../pages/settings/controls'
import { Field, Notice } from '../forms'
import { useMetricName } from '../metric/names'
import { Cancelled, useProtected } from '../StepUp'
import { Button } from '../ui/Button'

// "Report for your doctor" in Settings → Your data (issue 210, ADR 0013 section 6). The user picks a
// period of at most 12 months, the metrics and up to 10 ECG recordings; the request and the
// single download both go through the shared "Confirm it's you" dialog. The PDF holds readings,
// Withings' own device findings and the ECG strips at true scale; no estimates, scores or AI text.

const POLL_MS = 3000
const DAY_MS = 86_400_000

const isoDay = (date: Date): string => date.toISOString().slice(0, 10)

export function DoctorReport() {
  const t = useT()
  const format = useFormat()
  const protect = useProtected()
  const metricName = useMetricName()
  const [latest, setLatest] = useState<DoctorItem | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [downloaded, setDownloaded] = useState(false)
  const today = useMemo(() => new Date(), [])
  const [end, setEnd] = useState(isoDay(today))
  const [start, setStart] = useState(isoDay(new Date(today.getTime() - 89 * DAY_MS)))
  const [metrics, setMetrics] = useState<string[]>([])
  const [recordings, setRecordings] = useState<string[]>([])

  const tiles = useResource(metricsPath('1y'), decodeMetrics)
  const ecg = useResource(ecgListPath(0), decodeRecordingPage)
  const choices = (tiles.data ?? []).filter((m) => m.latest !== null)
  const strips = (ecg.data?.recordings ?? []).filter((r) => r.hasSignal)

  const load = useCallback(async () => {
    const items = await listDoctorReports()
    setLatest(items[0] ?? null)
  }, [])

  useEffect(() => {
    const controller = new AbortController()
    listDoctorReports(controller.signal).then(
      (items) => {
        setLatest(items[0] ?? null)
      },
      () => {
        // The row stays at "Create report…"; a request answers with its own error.
      },
    )
    return () => {
      controller.abort()
    }
  }, [])

  const building = latest?.state === 'building'
  useEffect(() => {
    if (!building) return
    const timer = window.setInterval(() => {
      load().catch(() => {})
    }, POLL_MS)
    return () => {
      window.clearInterval(timer)
    }
  }, [building, load])

  const run = async (purpose: string, action: () => Promise<unknown>, after?: () => void) => {
    setBusy(true)
    setError(null)
    try {
      await protect(purpose, action)
      after?.()
    } catch (caught) {
      if (!(caught instanceof Cancelled)) setError(errorMessage(caught))
    } finally {
      setBusy(false)
      await load().catch(() => {})
    }
  }

  const toggle = (list: string[], set: (next: string[]) => void, value: string) => {
    set(list.includes(value) ? list.filter((x) => x !== value) : [...list, value])
  }

  const days = (Date.parse(end) - Date.parse(start)) / DAY_MS
  const validPeriod = Number.isFinite(days) && days >= 0 && days < 366
  const ready = validPeriod && metrics.length > 0

  const start_ = () => {
    setDownloaded(false)
    void run(t('settings.data.doctor.purpose'), () => requestDoctorReport({ start, end, metrics, recordings }))
  }

  let status: string | null = null
  if (building) status = t('settings.data.doctor.preparing')
  if (latest?.state === 'ready' && latest.expiresAt !== null) {
    status = t('settings.data.doctor.ready', {
      until: format.dateTime(latest.expiresAt, format.browserZone()),
    })
  }

  const below = (
    <>
      <p className="field__hint">{t('settings.data.doctor.note')}</p>
      <Field
        label={t('settings.data.doctor.from')}
        type="date"
        value={start}
        onChange={(event) => {
          setStart(event.target.value)
        }}
      />
      <Field
        label={t('settings.data.doctor.to')}
        type="date"
        value={end}
        onChange={(event) => {
          setEnd(event.target.value)
        }}
      />
      {validPeriod ? null : <Notice kind="error">{t('settings.data.doctor.period')}</Notice>}
      <div role="group" aria-label={t('settings.data.doctor.metrics')}>
        {choices.map((m) => (
          <Chip
            key={m.key}
            label={metricName(m.key)}
            pressed={metrics.includes(m.key)}
            onClick={() => {
              toggle(metrics, setMetrics, m.key)
            }}
          />
        ))}
      </div>
      {strips.length > 0 ? (
        <div role="group" aria-label={t('settings.data.doctor.ecg', { max: String(MAX_ECG) })}>
          {strips.map((r) => (
            <Chip
              key={r.id}
              label={format.dateTime(r.recordedAt, r.sourceTz)}
              pressed={recordings.includes(r.id)}
              disabled={!recordings.includes(r.id) && recordings.length >= MAX_ECG}
              onClick={() => {
                toggle(recordings, setRecordings, r.id)
              }}
            />
          ))}
        </div>
      ) : null}
      {status !== null ? <p role="status">{status}</p> : null}
      {latest?.state === 'failed' ? <Notice kind="error">{t('settings.data.doctor.failed')}</Notice> : null}
      {downloaded ? <Notice kind="info">{t('settings.data.doctor.downloaded')}</Notice> : null}
      {error !== null ? <Notice kind="error">{error}</Notice> : null}
    </>
  )

  return (
    <Row label={t('settings.data.doctor.label')} help={t('settings.data.doctor.help')} below={below}>
      {latest?.state === 'ready' ? (
        <Button
          lock
          disabled={busy}
          onClick={() => {
            void run(
              t('settings.data.doctor.downloadPurpose'),
              () => downloadDoctorReport(latest.id),
              () => {
                setDownloaded(true)
              },
            )
          }}
        >
          {t('settings.data.doctor.download')}
        </Button>
      ) : (
        <Button lock disabled={busy || building || !ready} onClick={start_}>
          {t('settings.data.doctor.button')}
        </Button>
      )}
    </Row>
  )
}
