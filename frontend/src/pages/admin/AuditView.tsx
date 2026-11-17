import { useEffect, useId, useState } from 'react'
import { listAudit, type AuditRow } from '../../api/adminViews'
import { errorMessage } from '../../api/messages'
import { Notice } from '../../components/forms'
import { Button } from '../../components/ui/Button'
import { ResourceView, StatePill } from '../../components/ui/DataState'
import { getLocale, useT } from '../../i18n'
import { browserZone, formatDateTime } from '../../lib/format'

// The audit log: when, action, target, result. The server sends more (params, request id); none
// of it is decoded or shown. Newest first, older entries on request.

function Result({ outcome }: { outcome: string }) {
  const t = useT()
  if (outcome === 'ok') return <StatePill tone="ok">{t('admin.audit.result.ok')}</StatePill>
  if (outcome === 'refused') return <StatePill tone="urgent">{t('admin.audit.result.refused')}</StatePill>
  return <StatePill tone="attn">{t('admin.audit.result.error')}</StatePill>
}

export function AuditView() {
  const t = useT()
  const headingId = useId()
  const [rows, setRows] = useState<AuditRow[] | null>(null)
  const [next, setNext] = useState<number | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [cursor, setCursor] = useState<number | null>(null)
  const [attempt, setAttempt] = useState(0)
  const [loaded, setLoaded] = useState<{ cursor: number | null } | null>(null)
  const busy = loaded === null || loaded.cursor !== cursor

  useEffect(() => {
    const controller = new AbortController()
    listAudit(cursor, controller.signal).then(
      (page) => {
        setRows((before) => (cursor === null || before === null ? page.rows : [...before, ...page.rows]))
        setNext(page.nextBefore)
        setError(null)
        setLoaded({ cursor })
      },
      (caught: unknown) => {
        if (controller.signal.aborted) return
        setError(errorMessage(caught))
        setLoaded({ cursor })
      },
    )
    return () => {
      controller.abort()
    }
  }, [cursor, attempt])

  const retry = () => {
    setAttempt((n) => n + 1)
  }

  return (
    <section className="ov-stack" aria-labelledby={headingId}>
      <h2 id={headingId}>{t('admin.audit.title')}</h2>
      {rows === null ? (
        <ResourceView
          resource={{
            status: error === null ? 'loading' : 'error',
            data: null,
            error: error === null ? null : { kind: 'failed', message: error, retryAfter: null },
            reload: retry,
          }}
        >
          {() => null}
        </ResourceView>
      ) : null}
      {rows !== null && error !== null ? <Notice kind="error">{error}</Notice> : null}
      {rows !== null && rows.length === 0 ? <p className="ov-muted">{t('admin.audit.empty')}</p> : null}
      {rows !== null && rows.length > 0 ? (
        <div className="ov-tbl-wrap" tabIndex={0} role="region" aria-label={t('admin.audit.title')}>
          <table className="ov-tbl">
            <thead>
              <tr>
                <th>{t('admin.audit.col.when')}</th>
                <th>{t('admin.audit.col.action')}</th>
                <th>{t('admin.audit.col.target')}</th>
                <th>{t('admin.audit.col.result')}</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr key={row.id}>
                  <td className="ov-num">{formatDateTime(getLocale(), row.at, browserZone())}</td>
                  <td className="ov-mono">{row.action}</td>
                  <td className={row.target === null ? 'ov-muted' : undefined}>{row.target ?? '—'}</td>
                  <td>
                    <Result outcome={row.outcome} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}
      {rows !== null && error !== null ? (
        <div>
          <Button onClick={retry}>{t('common.tryAgain')}</Button>
        </div>
      ) : null}
      {rows !== null && error === null && next !== null ? (
        <div>
          <Button
            disabled={busy}
            onClick={() => {
              setCursor(next)
            }}
          >
            {t('admin.audit.more')}
          </Button>
        </div>
      ) : null}
      <p className="ov-small ov-muted">{t('admin.audit.note')}</p>
    </section>
  )
}
