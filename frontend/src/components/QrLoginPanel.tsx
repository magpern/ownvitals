import { useCallback, useEffect, useId, useRef, useState } from 'react'
import { completeQrLogin, qrErrorMessage, startQrLogin, watchQrLogin, type QrState } from '../api/qrlogin'
import { plural, useT } from '../i18n'
import '../styles/qrlogin.css'
import { Notice } from './forms'
import { QrCode } from './QrCode'
import { Button } from './ui/Button'

// The desktop's "Sign in with your phone" (ADR 0011 section 5): a QR from `POST /api/auth/qr/start`,
// then the request's SSE stream. `scanned` says the phone has it; `approved` is followed by
// `POST …/complete`, whose answer sets the new session's cookie (never an event). The page then
// shows "Signed in as …" before anything else, so a desktop signed in to someone else's account
// is seen at once (T83), and only "Continue" reads the session. An unscanned code expires after
// `expires_in` seconds on this page's clock too; "Show a new code" starts a new request, which
// expires the old one on the server.

type View =
  | { kind: 'starting' }
  | { kind: 'showing'; qrSvg: string; expiresIn: number }
  | { kind: 'scanned' }
  | { kind: 'approved' }
  | { kind: 'signedIn'; email: string }
  | { kind: 'ended'; state: 'denied' | 'expired' | 'lost' }
  | { kind: 'error'; message: string }

export function QrLoginPanel({ onClose, onSignedIn }: { onClose: () => void; onSignedIn: () => void }) {
  const t = useT()
  const titleId = useId()
  const [view, setView] = useState<View>({ kind: 'starting' })
  // Each start has a number; anything a superseded start or stream reports is dropped.
  const generation = useRef(0)
  const stopStream = useRef<(() => void) | null>(null)
  const timer = useRef<number | null>(null)

  const stop = useCallback(() => {
    stopStream.current?.()
    stopStream.current = null
    if (timer.current !== null) window.clearTimeout(timer.current)
    timer.current = null
  }, [])

  const begin = useCallback(() => {
    stop()
    const mine = ++generation.current
    const current = () => mine === generation.current
    startQrLogin().then(
      (started) => {
        if (!current()) return
        setView({ kind: 'showing', qrSvg: started.qrSvg, expiresIn: started.expiresIn })
        const onState = (state: QrState) => {
          if (!current()) return
          if (state === 'scanned') {
            // The scan window is over; the server's approval window now applies.
            if (timer.current !== null) window.clearTimeout(timer.current)
            timer.current = null
            setView({ kind: 'scanned' })
          } else if (state === 'approved') {
            stop()
            setView({ kind: 'approved' })
            completeQrLogin(started.id).then(
              ({ email }) => {
                if (current()) setView({ kind: 'signedIn', email })
              },
              (caught: unknown) => {
                if (current()) setView({ kind: 'error', message: qrErrorMessage(caught) })
              },
            )
          } else if (state !== 'completed') {
            stop()
            setView({ kind: 'ended', state })
          }
        }
        stopStream.current = watchQrLogin(started.id, onState)
        timer.current = window.setTimeout(() => {
          if (current()) onState('expired')
        }, started.expiresIn * 1000)
      },
      (caught: unknown) => {
        if (current()) setView({ kind: 'error', message: qrErrorMessage(caught) })
      },
    )
  }, [stop])

  useEffect(() => {
    begin()
    return () => {
      generation.current += 1
      stop()
    }
  }, [begin, stop])

  const again =
    view.kind === 'ended' || view.kind === 'error' ? (
      <Button
        variant="primary"
        wide
        onClick={() => {
          setView({ kind: 'starting' })
          begin()
        }}
      >
        {t('passkeys.qr.newCode')}
      </Button>
    ) : null

  return (
    <section className="ov-qrlogin" aria-labelledby={titleId}>
      <h2 id={titleId} className="ov-qrlogin__title">
        {t('passkeys.qr.title')}
      </h2>
      {view.kind === 'showing' ? (
        <>
          <p>{t('passkeys.qr.scan')}</p>
          <QrCode svg={view.qrSvg} label={t('passkeys.qr.label')} />
          <p className="ov-small ov-muted">{plural('passkeys.qr.validFor', view.expiresIn)}</p>
        </>
      ) : null}
      <div aria-live="polite">
        {view.kind === 'starting' ? <p className="ov-muted">{t('passkeys.qr.starting')}</p> : null}
        {view.kind === 'scanned' ? <Notice>{t('passkeys.qr.scanned')}</Notice> : null}
        {view.kind === 'approved' ? <Notice>{t('passkeys.qr.approved')}</Notice> : null}
        {view.kind === 'ended' ? (
          <Notice kind={view.state === 'lost' ? 'error' : 'info'}>{t(`passkeys.qr.${view.state}`)}</Notice>
        ) : null}
        {view.kind === 'error' ? <Notice kind="error">{view.message}</Notice> : null}
      </div>
      {view.kind === 'signedIn' ? (
        <>
          <Notice kind="success">
            <strong>{t('passkeys.qr.signedInAs', { email: view.email })}</strong>
          </Notice>
          <p className="ov-small ov-muted">{t('passkeys.qr.signedInText')}</p>
          <Button variant="primary" wide onClick={onSignedIn}>
            {t('passkeys.qr.continue')}
          </Button>
        </>
      ) : (
        <div className="ov-qrlogin__alt">
          {again}
          <button className="ov-linkbutton" type="button" onClick={onClose}>
            {t('passkeys.qr.usePassword')}
          </button>
        </div>
      )}
    </section>
  )
}
