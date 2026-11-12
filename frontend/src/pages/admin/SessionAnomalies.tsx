import { useId, useState } from 'react'
import { listSessionAnomalies, type AnomalyCursor, type SessionAnomaly } from '../../api/adminAnomalies'
import { errorMessage } from '../../api/messages'
import { eventDevice, eventTime } from '../../components/security/SecurityActivity'
import { Notice } from '../../components/forms'
import { Cancelled, useProtected } from '../../components/StepUp'
import { Button } from '../../components/ui/Button'
import { isMessageKey, t as translate, useT } from '../../i18n'

// The admin abuse view (issue 461, ADR 0017): the session anomalies of the last 30 days across
// accounts. A read of other people's accounts, so it needs a fresh code (the step-up dialog) and
// loads only when the admin asks. Nothing here signs anyone out: the admin acts from Accounts.

function signalLabel(code: string): string {
  const key = `security.admin.anomalies.signal.${code}`
  return isMessageKey(key) ? translate(key) : translate('security.admin.anomalies.signal.unknown')
}

function origin(row: SessionAnomaly): string {
  const device = eventDevice(row)
  return row.place === null ? device : `${device} · ${row.place}`
}

export function SessionAnomalies() {
  const t = useT()
  const headingId = useId()
  const protect = useProtected()
  const [rows, setRows] = useState<SessionAnomaly[] | null>(null)
  const [next, setNext] = useState<AnomalyCursor | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const load = (after: AnomalyCursor | null) => {
    setBusy(true)
    protect(t('security.admin.anomalies.purpose'), () => listSessionAnomalies(after)).then(
      (page) => {
        setRows((before) => (after === null || before === null ? page.rows : [...before, ...page.rows]))
        setNext(page.next)
        setError(null)
        setBusy(false)
      },
      (caught: unknown) => {
        if (!(caught instanceof Cancelled)) setError(errorMessage(caught))
        setBusy(false)
      },
    )
  }

  return (
    <section className="ov-stack" aria-labelledby={headingId}>
      <h2 id={headingId}>{t('security.admin.anomalies.title')}</h2>
      <p className="ov-small ov-muted">{t('security.admin.anomalies.intro')}</p>
      {error !== null ? <Notice kind="error">{error}</Notice> : null}
      {rows === null ? (
        <div>
          <Button lock disabled={busy} onClick={() => { load(null) }}>
            {t('security.admin.anomalies.show')}
          </Button>
        </div>
      ) : null}
      {rows !== null && rows.length === 0 ? <p className="ov-muted">{t('security.admin.anomalies.empty')}</p> : null}
      {rows !== null && rows.length > 0 ? (
        <div className="ov-tbl-wrap" tabIndex={0} role="region" aria-label={t('security.admin.anomalies.title')}>
          <table className="ov-tbl">
            <thead>
              <tr>
                <th>{t('security.admin.anomalies.col.when')}</th>
                <th>{t('security.admin.anomalies.col.account')}</th>
                <th>{t('security.admin.anomalies.col.signals')}</th>
                <th>{t('security.admin.anomalies.col.origin')}</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr key={row.id}>
                  <td className="ov-num">{eventTime(row.at)}</td>
                  <td className={row.email === null ? 'ov-muted' : undefined}>
                    {row.email ?? t('security.admin.anomalies.deleted')}
                  </td>
                  <td>{row.signals.map(signalLabel).join(', ')}</td>
                  <td>{origin(row)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}
      {rows !== null && next !== null ? (
        <div>
          <Button lock disabled={busy} onClick={() => { load(next) }}>
            {t('security.admin.anomalies.more')}
          </Button>
        </div>
      ) : null}
    </section>
  )
}
