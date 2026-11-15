import { useCallback, useEffect, useState } from 'react'
import { downloadExport, listExports, requestExport, type ExportItem } from '../../api/exports'
import { errorMessage } from '../../api/messages'
import { useT } from '../../i18n'
import { useFormat } from '../../i18n/useFormat'
import { Row } from '../../pages/settings/controls'
import { Notice } from '../forms'
import { Cancelled, useProtected } from '../StepUp'
import { Button } from '../ui/Button'

// "Export everything" in Settings → Your data (issue 211, ADR 0013 section 4). The latest export
// decides the row: none (or one that expired or failed) offers "Export…", one that builds says so
// and is polled, a ready one offers "Download". Both actions go through the shared "Confirm it's
// you" dialog: the request wants a recent sign-in and a fresh code, the download a fresh code.

const POLL_MS = 3000

export function ExportCard() {
  const t = useT()
  const format = useFormat()
  const protect = useProtected()
  const [latest, setLatest] = useState<ExportItem | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [downloaded, setDownloaded] = useState(false)

  const load = useCallback(async () => {
    const items = await listExports()
    setLatest(items[0] ?? null)
  }, [])

  useEffect(() => {
    const controller = new AbortController()
    listExports(controller.signal).then(
      (items) => {
        setLatest(items[0] ?? null)
      },
      () => {
        // The row stays at "Export…"; a request answers with its own error.
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

  const start = () => {
    setDownloaded(false)
    void run(t('settings.data.export.purpose'), requestExport)
  }

  const fetchFile = (item: ExportItem) => {
    void run(
      t('settings.data.export.downloadPurpose'),
      () => downloadExport(item.id),
      () => {
        setDownloaded(true)
      },
    )
  }

  let status: string | null = null
  if (latest?.state === 'building') status = t('settings.data.export.preparing')
  if (latest?.state === 'ready' && latest.expiresAt !== null) {
    status = t('settings.data.export.ready', {
      size: format.number((latest.size ?? 0) / 1_000_000, 1),
      until: format.dateTime(latest.expiresAt, format.browserZone()),
    })
  }
  let failed: string | null = null
  if (latest?.state === 'failed') {
    failed =
      latest.failure === 'export_too_large'
        ? t('settings.data.export.tooLarge', { part: latest.largest ?? '' })
        : t('settings.data.export.failed')
  }

  const below = (
    <>
      {status !== null ? <p role="status">{status}</p> : null}
      {failed !== null ? <Notice kind="error">{failed}</Notice> : null}
      {downloaded ? <Notice kind="info">{t('settings.data.export.downloaded')}</Notice> : null}
      {error !== null ? <Notice kind="error">{error}</Notice> : null}
    </>
  )

  return (
    <Row label={t('settings.data.export.label')} help={t('settings.data.export.help')} below={below}>
      {latest?.state === 'ready' ? (
        <Button
          lock
          disabled={busy}
          onClick={() => {
            fetchFile(latest)
          }}
        >
          {t('settings.data.export.download')}
        </Button>
      ) : (
        <Button lock disabled={busy || building} onClick={start}>
          {t('settings.data.export.button')}
        </Button>
      )}
    </Row>
  )
}
