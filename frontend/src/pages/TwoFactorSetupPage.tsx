import { useEffect, useId, useRef, useState, type FormEvent } from 'react'
import { confirmTotp, startTotp, type Enrollment } from '../api/auth'
import { SIGN_IN_PATH } from '../auth/access'
import { RequireTwoFactorStep } from '../auth/guards'
import { useSession } from '../auth/SessionContext'
import { CODE_LENGTH, CodeInput } from '../components/CodeInput'
import { Notice, useAction } from '../components/forms'
import { QrCode } from '../components/QrCode'
import { Button } from '../components/ui/Button'
import { useT } from '../i18n'
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
  const t = useT()
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
        <span className="ov-eyebrow">{t('auth.setup.step')}</span>
        <h1 id={titleId} className="ov-auth__title">
          {t('auth.setup.title')}
        </h1>
        <p className="ov-small ov-muted">{t('auth.setup.scan')}</p>
        {action.error ? <Notice kind="error">{action.error}</Notice> : null}
        {enrollment === null ? (
          <p className="ov-muted">{action.busy ? t('common.loading') : null}</p>
        ) : (
          <div className="ov-auth__qr">
            <QrCode svg={enrollment.qrSvg} label={t('auth.setup.qrLabel')} />
            <div className="ov-stack ov-small" data-gap="sm">
              <span className="ov-muted">{t('auth.setup.cantScan')}</span>
              <span className="ov-mono break-all">{grouped(enrollment.secret)}</span>
              <span className="ov-muted">{t('auth.setup.timeBased')}</span>
            </div>
          </div>
        )}
        <div className="field">
          <label htmlFor={codeId}>{t('auth.setup.codeLabel')}</label>
          <CodeInput id={codeId} label={t('auth.setup.codeLabel')} value={code} onChange={setCode} />
        </div>
        <Button
          variant="primary"
          wide
          type="submit"
          disabled={action.busy || enrollment === null || code.length !== CODE_LENGTH}
        >
          {t('common.confirm')}
        </Button>
      </form>
    </section>
  )
}

function RecoveryCodes({ saved, onDone }: { saved: Saved; onDone: () => void }) {
  const t = useT()
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
        {t('auth.recovery.title')}
      </h1>
      <p className="ov-small ov-muted">{t('auth.recovery.intro')}</p>
      {saved.signedOut ? (
        <Notice kind="info">{t('auth.recovery.signedOut')}</Notice>
      ) : null}
      <ol className="ov-codes ov-mono" aria-label={t('auth.recovery.listLabel')}>
        {saved.codes.map((code) => (
          <li key={code}>{code}</li>
        ))}
      </ol>
      <div className="ov-row">
        <Button onClick={download}>{t('auth.recovery.download')}</Button>
        <Button onClick={copy}>{copied ? t('auth.recovery.copied') : t('auth.recovery.copy')}</Button>
        <Button
          onClick={() => {
            window.print()
          }}
        >
          {t('auth.recovery.print')}
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
        {t('auth.recovery.saved')}
      </label>
      <Button variant="primary" wide disabled={!kept} onClick={go}>
        {saved.signedOut ? t('auth.recovery.signInAgain') : t('auth.recovery.continue')}
      </Button>
    </section>
  )
}
