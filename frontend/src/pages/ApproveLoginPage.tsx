import { useEffect, useId, useRef, useState, type FormEvent, type KeyboardEvent, type ReactNode } from 'react'
import { ApiError } from '../api/client'
import {
  QR_UNAVAILABLE,
  approveQr,
  denyQr,
  nonceFromHash,
  qrErrorMessage,
  scanQr,
  type ScannedRequest,
} from '../api/qrlogin'
import { accessOf } from '../auth/access'
import { useSession } from '../auth/SessionContext'
import { Notice, useAction } from '../components/forms'
import { NO_CODE, SecondFactorField, complete, type SecondFactor } from '../components/SecondFactorField'
import { Button } from '../components/ui/Button'
import { Icon } from '../components/ui/Icon'
import { useT } from '../i18n'
import { useFormat } from '../i18n/useFormat'
import { Link } from '../router/Router'
import '../styles/qrlogin.css'

// The phone's side of QR approve-login (/qr#<nonce>; ADR 0011 sections 5, 7 and 8). The nonce is
// read from the fragment once and the fragment is removed from the address bar and the history
// at once. Only a full session scans; anyone else sees "Sign in on this phone first" and the nonce
// is dropped. The scan shows where the request comes from, the warning of section 7 and, when the
// networks differ, a second warning. "Approve…" opens a "Confirm it's you" dialog whose code is
// sent with the approval itself (an earlier step-up does not count); a wrong code stays in the
// dialog to try again. Nothing here is stored in the browser.

type View =
  | { kind: 'reading' }
  | { kind: 'signInFirst' }
  | { kind: 'scanned'; request: ScannedRequest }
  | { kind: 'approved' }
  | { kind: 'denied' }
  | { kind: 'failed'; title: string; message: string }

/** The server's own "Place unknown" (geo.shown), worded in the current language. */
const SERVER_PLACE_UNKNOWN = 'Place unknown'

/**
 * A new `/qr#<nonce>` opened while this page is showing (the camera reusing the tab) changes only
 * the fragment, so the browser does not load the page again: start over with the new request.
 */
export function ApproveLoginPage() {
  const [request, setRequest] = useState(0)
  useEffect(() => {
    const onHash = () => {
      if (window.location.hash !== '') setRequest((n) => n + 1)
    }
    window.addEventListener('hashchange', onHash)
    return () => {
      window.removeEventListener('hashchange', onHash)
    }
  }, [])
  return <ApproveRequest key={request} />
}

function ApproveRequest() {
  const t = useT()
  const { status, session } = useSession()
  // Read before the effect below removes it; the initializer may run twice, both before that.
  const [nonce] = useState(() => nonceFromHash(window.location.hash))
  // What the scan and the buttons decided; until then the view follows the session.
  const [outcome, setOutcome] = useState<View | null>(null)
  const decided = useRef(false)

  useEffect(() => {
    if (window.location.hash !== '') {
      window.history.replaceState(null, '', window.location.pathname + window.location.search)
    }
  }, [])

  const ready = status === 'ready' && accessOf(session) === 'ready'
  useEffect(() => {
    // Decided once, when the session is known: a page without a full session drops the nonce
    // for good, and the first scan wins on the server, so none is ever sent twice.
    if (status !== 'ready' || decided.current) return
    decided.current = true
    if (!ready || nonce === null) return
    scanQr(nonce).then(
      (request) => {
        setOutcome({ kind: 'scanned', request })
      },
      (caught: unknown) => {
        setOutcome({ kind: 'failed', title: t('passkeys.approve.unavailableTitle'), message: qrErrorMessage(caught) })
      },
    )
  }, [status, ready, nonce, t])

  let view: View = outcome ?? { kind: 'reading' }
  // The session could not be read: say so instead of staying on "Reading the code" (issue 304).
  if (outcome === null && status === 'error') {
    view = { kind: 'failed', title: t('auth.guard.unreachable'), message: t('auth.guard.retry') }
  }
  if (outcome === null && status === 'ready') {
    if (!ready) view = { kind: 'signInFirst' }
    else if (nonce === null) {
      view = { kind: 'failed', title: t('passkeys.approve.unavailableTitle'), message: t('passkeys.approve.unavailable') }
    }
  }
  return <ApproveView view={view} setView={setOutcome} />
}

function ApproveView({ view, setView }: { view: View; setView: (view: View) => void }) {
  const t = useT()
  const titleId = useId()
  const action = useAction()
  const [asking, setAsking] = useState(false)

  const page = (title: string, body: ReactNode) => (
    <section className="ov-auth" aria-labelledby={titleId}>
      <h1 id={titleId} className="ov-auth__title">
        {title}
      </h1>
      {body}
    </section>
  )

  switch (view.kind) {
    case 'reading':
      return page(t('passkeys.approve.title'), <p className="ov-muted">{t('passkeys.approve.reading')}</p>)
    case 'signInFirst':
      return page(
        t('passkeys.approve.signInFirstTitle'),
        <>
          <p>{t('passkeys.approve.signInFirst')}</p>
          <Link to="/signin">{t('common.signIn')}</Link>
        </>,
      )
    case 'failed':
      return page(
        view.title,
        <>
          <p>{view.message}</p>
          <Link to="/">{t('passkeys.approve.home')}</Link>
        </>,
      )
    case 'approved':
      return page(t('passkeys.approve.approvedTitle'), <p>{t('passkeys.approve.approved')}</p>)
    case 'denied':
      return page(
        t('passkeys.approve.deniedTitle'),
        <>
          <p>{t('passkeys.approve.denied')}</p>
          <Link to="/">{t('passkeys.approve.home')}</Link>
        </>,
      )
    case 'scanned': {
      const { request } = view
      const deny = () => {
        void action.run(async () => {
          await denyQr(request.id)
          setView({ kind: 'denied' })
        }, qrErrorMessage)
      }
      return page(
        t('passkeys.approve.title'),
        <>
          <Facts request={request} />
          <Notice>{t('passkeys.approve.warning')}</Notice>
          {request.differentNetwork ? <Notice kind="error">{t('passkeys.approve.differentNetwork')}</Notice> : null}
          {action.error ? <Notice kind="error">{action.error}</Notice> : null}
          <div className="ov-approve__actions">
            <Button
              variant="primary"
              lock
              disabled={action.busy}
              onClick={() => {
                action.clear()
                setAsking(true)
              }}
            >
              {t('passkeys.approve.approve')}
            </Button>
            <Button disabled={action.busy} onClick={deny}>
              {t('passkeys.approve.deny')}
            </Button>
          </div>
          {asking ? (
            <ApproveDialog
              requestId={request.id}
              onApproved={() => {
                setView({ kind: 'approved' })
              }}
              onGone={(message) => {
                setView({ kind: 'failed', title: t('passkeys.approve.unavailableTitle'), message })
              }}
              onCancel={() => {
                setAsking(false)
              }}
            />
          ) : null}
        </>,
      )
    }
  }
}

function Facts({ request }: { request: ScannedRequest }) {
  const t = useT()
  const format = useFormat()
  const { desktop } = request
  const place = desktop.place === SERVER_PLACE_UNKNOWN ? t('passkeys.approve.placeUnknown') : desktop.place
  return (
    <dl className="ov-approve__facts">
      <dt>{t('passkeys.approve.device')}</dt>
      <dd>{t('passkeys.approve.deviceValue', { browser: desktop.browser, os: desktop.os })}</dd>
      <dt>{t('passkeys.approve.ip')}</dt>
      <dd>{desktop.ip === null ? t('passkeys.approve.unknown') : <span className="ov-mono">{desktop.ip}</span>}</dd>
      <dt>{t('passkeys.approve.place')}</dt>
      <dd>{place}</dd>
      <dt>{t('passkeys.approve.requested')}</dt>
      <dd className="ov-num">{format.time(request.requestedAt, format.browserZone())}</dd>
    </dl>
  )
}

/**
 * "Confirm it's you" for the approval: the shared dialog's look and wording (Security mock-up),
 * but the code goes to `POST /api/auth/qr/{id}/approve` itself, as ADR 0011 section 8 requires.
 */
function ApproveDialog({
  requestId,
  onApproved,
  onGone,
  onCancel,
}: {
  requestId: string
  onApproved: () => void
  onGone: (message: string) => void
  onCancel: () => void
}) {
  const t = useT()
  const titleId = useId()
  const textId = useId()
  const action = useAction()
  const [factor, setFactor] = useState<SecondFactor>(NO_CODE)

  const submit = (event: FormEvent) => {
    event.preventDefault()
    void action.run(async () => {
      try {
        await approveQr(requestId, factor.code.trim())
      } catch (caught) {
        if (caught instanceof ApiError && caught.reason === QR_UNAVAILABLE) {
          onGone(qrErrorMessage(caught))
          return
        }
        setFactor({ recovery: factor.recovery, code: '' })
        throw caught
      }
      onApproved()
    }, qrErrorMessage)
  }

  const onKey = (event: KeyboardEvent) => {
    if (event.key === 'Escape') {
      event.preventDefault()
      onCancel()
    }
  }

  return (
    <div className="ov-scrim" onKeyDown={onKey}>
      <form
        className="ov-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        aria-describedby={textId}
        onSubmit={submit}
      >
        <span className="ov-dialog__icon">
          <Icon name="lock" size={40} />
        </span>
        <h2 id={titleId} className="ov-dialog__title">
          {t('security.stepUp.title')}
        </h2>
        <p id={textId} className="ov-small ov-muted">
          {t('passkeys.approve.code', { purpose: t('passkeys.approve.purpose') })}
        </p>
        {action.error ? <Notice kind="error">{action.error}</Notice> : null}
        <SecondFactorField value={factor} onChange={setFactor} autoFocus disabled={action.busy} />
        <div className="ov-dialog__actions">
          <Button variant="ghost" onClick={onCancel}>
            {t('common.cancel')}
          </Button>
          <Button variant="primary" type="submit" disabled={action.busy || !complete(factor)}>
            {t('common.confirm')}
          </Button>
        </div>
      </form>
    </div>
  )
}
