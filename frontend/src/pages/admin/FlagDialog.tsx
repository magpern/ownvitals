import { useId, useState, type FormEvent, type ReactNode } from 'react'
import { errorMessage } from '../../api/messages'
import { Notice } from '../../components/forms'
import { Cancelled, useProtected } from '../../components/StepUp'
import { Button } from '../../components/ui/Button'
import { useT } from '../../i18n'

// The dialog behind an admin flag's "Change…" (issue 550). It knows nothing about the flag: the
// caller passes the fields, what Save sends and, when a value is stored, what the reset sends. Both
// run through the shared "Confirm it's you" dialog (an admin write needs a code from the last 5
// minutes), so a later flag (the AI monthly caps) reuses it with its own fields.

export interface FlagDialogProps {
  title: string
  /** What the step-up dialog says needs the proof. */
  purpose: string
  /** Whether Save can be pressed (the fields hold a value the server may take). */
  ready: boolean
  save: () => Promise<unknown>
  /** Clears the stored value; absent when nothing is stored. */
  reset?: () => Promise<unknown>
  resetLabel?: string
  onClose: () => void
  /** After a write went through: the caller reloads what it shows. */
  onDone: () => void
  children: ReactNode
}

export function FlagDialog({ title, purpose, ready, save, reset, resetLabel, onClose, onDone, children }: FlagDialogProps) {
  const t = useT()
  const protect = useProtected()
  const titleId = useId()
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const run = async (write: () => Promise<unknown>) => {
    setBusy(true)
    setError(null)
    try {
      await protect(purpose, write)
      onDone()
    } catch (caught) {
      if (!(caught instanceof Cancelled)) setError(errorMessage(caught))
      setBusy(false)
    }
  }

  const submit = (event: FormEvent) => {
    event.preventDefault()
    void run(save)
  }

  return (
    <div className="ov-scrim">
      <form className="ov-dialog" role="dialog" aria-modal="true" aria-labelledby={titleId} onSubmit={submit}>
        <h2 id={titleId} className="ov-dialog__title">
          {title}
        </h2>
        {children}
        {error !== null ? <Notice kind="error">{error}</Notice> : null}
        <div className="ov-dialog__actions">
          <Button variant="ghost" onClick={onClose} disabled={busy}>
            {t('common.cancel')}
          </Button>
          {reset !== undefined ? (
            <Button lock disabled={busy} onClick={() => void run(reset)}>
              {resetLabel ?? t('admin.flags.reset')}
            </Button>
          ) : null}
          <Button variant="primary" type="submit" lock disabled={busy || !ready}>
            {t('admin.flags.save')}
          </Button>
        </div>
      </form>
    </div>
  )
}
