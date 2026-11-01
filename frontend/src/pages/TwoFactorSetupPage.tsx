import { useEffect, useId, useRef, useState, type FormEvent } from 'react'
import { confirmTotp, startTotp, type Enrollment } from '../api/auth'
import { SIGN_IN_PATH } from '../auth/access'
import { RequireTwoFactorStep } from '../auth/guards'
import { useSession } from '../auth/SessionContext'
import { CODE_LENGTH, CodeInput } from '../components/CodeInput'
import { Notice, useAction } from '../components/forms'
import { QrCode } from '../components/QrCode'
import { Button } from '../components/ui/Button'
import { safeReturnTo, useQuery, useRouter } from '../router/Router'

// Enrolment (docs/design/mockups/src/Auth.dc.html, "Set up the authenticator" and "Recovery
// codes"): mandatory for every account, before anything else (ADR 0003 section 2). The QR code
// and key, the first code to confirm, then the recovery codes, shown once: the page stays up
// (`hold`) until the user says they saved them.

const CONNECT_PATH = '/connections'

interface Saved {
  codes: string[]
  /** The enrolment went through but the session ended meanwhile: sign in again afterwards. */
  signedOut: boolean
}

export function TwoFactorSetupPage() {
  const [saved, setSaved] = useState<Saved | null>(null)
  return (
    <RequireTwoFactorStep step="enrol" hold={saved !== null}>
      {saved === null ? (
        <Setup onConfirmed={setSaved} />
      ) : (
        <RecoveryCodes
          saved={saved}
          onDone={() => {
            setSaved(null)
          }}
        />
      )}
    </RequireTwoFactorStep>
  )
}

/** The key in groups of four, as an authenticator app shows it. */
function grouped(secret: string): string {
  return secret.replace(/(.{4})(?=.)/g, '$1 ')
}

function Setup({ onConfirmed }: { onConfirmed: (saved: Saved) => void }) {
  const titleId = useId()
  const codeId = useId()
  const { refresh } = useSession()
  const [enrollment, setEnrollment] = useState<Enrollment | null>(null)
  const [code, setCode] = useState('')
  const action = useAction()
  const started = useRef(false)

  useEffect(() => {
    // Once, even under StrictMode's second mount: each start is a new secret.
    if (started.current) return
    started.current = true
    void action.run(async () => {
      setEnrollment(await startTotp())
    })
  }, [action])

  const confirm = (event: FormEvent) => {
    event.preventDefault()
    if (enrollment === null) return
    void action.run(async () => {
      const result = await confirmTotp(enrollment.pending, code)
      onConfirmed({ codes: result.recoveryCodes, signedOut: result.signedOut })
      // The session now counts as having passed the second factor; the codes stay up meanwhile.
      await refresh()
    })
  }

  return (
    <section className="ov-auth" aria-labelledby={titleId}>
      <form className="ov-auth__form" onSubmit={confirm}>
        <span className="ov-eyebrow">Step 1 of 3 · Account</span>
        <h1 id={titleId} className="ov-auth__title">
          Set up your authenticator app
        </h1>
        <p className="ov-small ov-muted">
          Scan the code with an authenticator app, then enter the 6 digits it shows.
        </p>
        {action.error ? <Notice kind="error">{action.error}</Notice> : null}
        {enrollment === null ? (
          <p className="ov-muted">{action.busy ? 'Loading' : null}</p>
        ) : (
          <div className="ov-auth__qr">
            <QrCode svg={enrollment.qrSvg} label="QR code for your authenticator app" />
            <div className="ov-stack ov-small" data-gap="sm">
              <span className="ov-muted">Can&apos;t scan it? Enter this key:</span>
              <span className="ov-mono break-all">{grouped(enrollment.secret)}</span>
              <span className="ov-muted">Time-based, 6 digits</span>
            </div>
          </div>
        )}
        <div className="field">
          <label htmlFor={codeId}>Code from the app</label>
          <CodeInput id={codeId} label="Code from the app" value={code} onChange={setCode} />
        </div>
        <Button
          variant="primary"
          wide
          type="submit"
          disabled={action.busy || enrollment === null || code.length !== CODE_LENGTH}
        >
          Confirm
        </Button>
      </form>
    </section>
  )
}

function RecoveryCodes({ saved, onDone }: { saved: Saved; onDone: () => void }) {
  const titleId = useId()
  const { refresh } = useSession()
  const { navigate } = useRouter()
  const returnTo = safeReturnTo(useQuery().get('return_to'))
  const [kept, setKept] = useState(false)
  const [copied, setCopied] = useState(false)
  const text = saved.codes.join('\n')

  const download = () => {
    const url = URL.createObjectURL(new Blob([`${text}\n`], { type: 'text/plain' }))
    const link = document.createElement('a')
    link.href = url
    link.download = 'ownvitals-recovery-codes.txt'
    link.click()
    URL.revokeObjectURL(url)
  }

  const copy = () => {
    void navigator.clipboard.writeText(text).then(
      () => {
        setCopied(true)
      },
      () => {
        setCopied(false)
      },
    )
  }

  const go = () => {
    onDone()
    if (saved.signedOut) {
      void refresh().then(() => {
        navigate(SIGN_IN_PATH)
      })
      return
    }
    navigate(returnTo ?? CONNECT_PATH)
  }

  return (
    <section className="ov-auth" aria-labelledby={titleId}>
      <h1 id={titleId} className="ov-auth__title">
        Save your recovery codes
      </h1>
      <p className="ov-small ov-muted">
        If you lose your phone, each code signs you in once. Keep them somewhere other than your
        phone.
      </p>
      {saved.signedOut ? (
        <Notice kind="info">
          Your authenticator is set up, but you were signed out meanwhile. Save the codes, then
          sign in again.
        </Notice>
      ) : null}
      <ol className="ov-codes ov-mono" aria-label="Recovery codes">
        {saved.codes.map((code) => (
          <li key={code}>{code}</li>
        ))}
      </ol>
      <div className="ov-row">
        <Button onClick={download}>Download</Button>
        <Button onClick={copy}>{copied ? 'Copied' : 'Copy'}</Button>
        <Button
          onClick={() => {
            window.print()
          }}
        >
          Print
        </Button>
      </div>
      <label className="ov-check">
        <input
          type="checkbox"
          checked={kept}
          onChange={(event) => {
            setKept(event.target.checked)
          }}
        />
        I&apos;ve saved them somewhere safe
      </label>
      <Button variant="primary" wide disabled={!kept} onClick={go}>
        {saved.signedOut ? 'Sign in again' : 'Continue to Connect Withings'}
      </Button>
    </section>
  )
}
