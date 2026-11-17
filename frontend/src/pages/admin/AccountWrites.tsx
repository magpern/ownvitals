import { useId, useState } from 'react'
import { createInvite, signOutAll, type NewInvite } from '../../api/adminWrites'
import { errorMessage } from '../../api/messages'
import { Notice } from '../../components/forms'
import { eventTime } from '../../components/security/SecurityActivity'
import { Cancelled, useProtected } from '../../components/StepUp'
import { Button } from '../../components/ui/Button'
import { useT } from '../../i18n'

// The Accounts table's two writes (Admin mock-up, issue 550): "Invite someone…" and, per account
// with a session, "Sign out all…". Both are admin writes: a code from the last 5 minutes (the
// shared step-up dialog), audited by the server. The invite code is shown once, in this dialog, and
// kept only in its state: closing the dialog forgets it.

export function InviteButton() {
  const t = useT()
  const protect = useProtected()
  const [invite, setInvite] = useState<NewInvite | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const create = () => {
    setBusy(true)
    setError(null)
    protect(t('admin.invite.purpose'), createInvite).then(
      (created) => {
        setInvite(created)
        setBusy(false)
      },
      (caught: unknown) => {
        if (!(caught instanceof Cancelled)) setError(errorMessage(caught))
        setBusy(false)
      },
    )
  }

  return (
    <>
      <Button variant="primary" lock disabled={busy} onClick={create}>
        {t('admin.invite.button')}
      </Button>
      {error !== null ? <Notice kind="error">{error}</Notice> : null}
      {invite !== null ? (
        <InviteCodeDialog
          invite={invite}
          onClose={() => {
            setInvite(null)
          }}
        />
      ) : null}
    </>
  )
}

function InviteCodeDialog({ invite, onClose }: { invite: NewInvite; onClose: () => void }) {
  const t = useT()
  const titleId = useId()
  const [copied, setCopied] = useState(false)
  const copy = () => {
    void navigator.clipboard?.writeText(invite.code).then(
      () => {
        setCopied(true)
      },
      () => {
        setCopied(false)
      },
    )
  }
  return (
    <div className="ov-scrim">
      <div className="ov-dialog" role="dialog" aria-modal="true" aria-labelledby={titleId}>
        <h2 id={titleId} className="ov-dialog__title">
          {t('admin.invite.title')}
        </h2>
        <p className="ov-small ov-muted">{t('admin.invite.text', { date: eventTime(invite.expiresAt) })}</p>
        <p className="ov-mono ov-invite-code" aria-label={t('admin.invite.code')}>
          {invite.code}
        </p>
        <p className="ov-small ov-muted">{t('admin.invite.once')}</p>
        {copied ? <Notice kind="success">{t('admin.invite.copied')}</Notice> : null}
        <div className="ov-dialog__actions">
          <Button onClick={copy}>{t('admin.invite.copy')}</Button>
          <Button variant="primary" onClick={onClose}>
            {t('admin.invite.done')}
          </Button>
        </div>
      </div>
    </div>
  )
}

export function SignOutAllButton({ userId, email, onDone }: { userId: string; email: string; onDone: () => void }) {
  const t = useT()
  const protect = useProtected()
  const titleId = useId()
  const [open, setOpen] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const run = () => {
    setBusy(true)
    setError(null)
    protect(t('admin.signOut.purpose'), () => signOutAll(userId)).then(
      () => {
        setBusy(false)
        setOpen(false)
        onDone()
      },
      (caught: unknown) => {
        if (!(caught instanceof Cancelled)) setError(errorMessage(caught))
        setBusy(false)
      },
    )
  }

  return (
    <>
      <Button
        lock
        onClick={() => {
          setError(null)
          setOpen(true)
        }}
      >
        {t('admin.signOut.button')}
      </Button>
      {open ? (
        <div className="ov-scrim">
          <div className="ov-dialog" role="dialog" aria-modal="true" aria-labelledby={titleId}>
            <h2 id={titleId} className="ov-dialog__title">
              {t('admin.signOut.title', { email })}
            </h2>
            <p className="ov-small ov-muted">{t('admin.signOut.text')}</p>
            {error !== null ? <Notice kind="error">{error}</Notice> : null}
            <div className="ov-dialog__actions">
              <Button
                variant="ghost"
                disabled={busy}
                onClick={() => {
                  setOpen(false)
                }}
              >
                {t('common.cancel')}
              </Button>
              <Button variant="danger-solid" lock disabled={busy} onClick={run}>
                {t('admin.signOut.confirm')}
              </Button>
            </div>
          </div>
        </div>
      ) : null}
    </>
  )
}
