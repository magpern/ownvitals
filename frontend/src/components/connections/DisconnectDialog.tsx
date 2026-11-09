import { useId, useState } from 'react'
import { disconnectWithings, type Disconnected } from '../../api/connections'
import { errorMessage } from '../../api/messages'
import { useT } from '../../i18n'
import { Notice } from '../forms'
import { Cancelled, useProtected } from '../StepUp'
import { Button } from '../ui/Button'

// "Disconnect Withings?" (Connections mock-up): keep or delete what was imported, then the shared
// "Confirm it's you" dialog asks for the authenticator code (the mock-up draws the code boxes
// inside this dialog; the shared dialog is the one place that asks, docs/design/README.md).

export function DisconnectDialog({ onClose, onDone }: { onClose: () => void; onDone: (result: Disconnected) => void }) {
  const t = useT()
  const protect = useProtected()
  const titleId = useId()
  const [deleteData, setDeleteData] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const run = async () => {
    setBusy(true)
    setError(null)
    try {
      onDone(await protect(t('connections.disconnect.purpose'), () => disconnectWithings(deleteData)))
    } catch (caught) {
      if (!(caught instanceof Cancelled)) setError(errorMessage(caught))
      setBusy(false)
    }
  }

  return (
    <div className="ov-scrim">
      <div className="ov-dialog" role="dialog" aria-modal="true" aria-labelledby={titleId}>
        <h2 id={titleId} className="ov-dialog__title">
          {t('connections.disconnect.title')}
        </h2>
        <p className="ov-small ov-muted">{t('connections.disconnect.text')}</p>
        <fieldset className="ov-choice">
          <legend>{t('connections.disconnect.data')}</legend>
          <label>
            <input type="radio" name="keep" checked={!deleteData} onChange={() => setDeleteData(false)} />
            <span>
              <strong>{t('connections.disconnect.keep')}</strong>
              <span className="ov-small ov-muted">{t('connections.disconnect.keep.help')}</span>
            </span>
          </label>
          <label>
            <input type="radio" name="keep" checked={deleteData} onChange={() => setDeleteData(true)} />
            <span>
              <strong>{t('connections.disconnect.delete')}</strong>
              <span className="ov-small ov-muted">{t('connections.disconnect.delete.help')}</span>
            </span>
          </label>
        </fieldset>
        {error !== null ? <Notice kind="error">{error}</Notice> : null}
        <div className="ov-dialog__actions">
          <Button variant="ghost" onClick={onClose} disabled={busy}>
            {t('common.cancel')}
          </Button>
          <Button variant="danger-solid" lock disabled={busy} onClick={() => void run()}>
            {t('connections.disconnect.confirm')}
          </Button>
        </div>
      </div>
    </div>
  )
}
