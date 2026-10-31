import { useState, type FormEvent } from 'react'
import { confirmTotp, startTotp, type Enrollment } from '../api/auth'
import { RequireTwoFactorStep } from '../auth/guards'
import { useSession } from '../auth/SessionContext'
import { Card, Field, Notice, useAction } from '../components/forms'
import { QrCode } from '../components/QrCode'

/** Shown once, right after enrolment. The server never shows them again. */
function RecoveryCodes({ codes }: { codes: string[] }) {
  return (
    <div className="codes">
      <p>
        <strong>Save these recovery codes now.</strong> Each works once in place of an app code. They
        will not be shown again.
      </p>
      <ul aria-label="Recovery codes">
        {codes.map((code) => (
          <li key={code}>
            <code>{code}</code>
          </li>
        ))}
      </ul>
    </div>
  )
}

/**
 * Enrolment: the QR code and key for an authenticator app, then the first code to confirm. The
 * recovery codes that come back are shown once; the page stays up (`hold`) until the user says
 * they saved them, and only then does the guard send them on.
 */
export function TwoFactorSetupPage() {
  const [codes, setCodes] = useState<string[] | null>(null)
  return (
    <RequireTwoFactorStep step="enrol" hold={codes !== null}>
      <Setup codes={codes} onSaved={setCodes} />
    </RequireTwoFactorStep>
  )
}

function Setup({
  codes,
  onSaved,
}: {
  codes: string[] | null
  onSaved: (codes: string[] | null) => void
}) {
  const { refresh } = useSession()
  const [enrollment, setEnrollment] = useState<Enrollment | null>(null)
  const [code, setCode] = useState('')
  const action = useAction()

  const begin = () => {
    void action.run(async () => {
      setEnrollment(await startTotp())
    })
  }
  const confirm = (event: FormEvent) => {
    event.preventDefault()
    if (enrollment === null) return
    void action.run(async () => {
      const saved = await confirmTotp(enrollment.pending, code.trim())
      onSaved(saved)
      setEnrollment(null)
      // The session now counts as having passed the second factor; the codes stay up meanwhile.
      await refresh()
    })
  }

  if (codes !== null) {
    return (
      <Card title="Two-factor is on">
        <RecoveryCodes codes={codes} />
        <button
          className="button"
          type="button"
          onClick={() => {
            onSaved(null)
          }}
        >
          I have saved them
        </button>
      </Card>
    )
  }

  if (enrollment === null) {
    return (
      <Card title="Set up two-factor">
        <p>
          Every ownvitals account needs a second step at sign-in: a code from an authenticator app on
          your phone. Set it up now to continue.
        </p>
        {action.error ? <Notice kind="error">{action.error}</Notice> : null}
        <button className="button" type="button" disabled={action.busy} onClick={begin}>
          Set up an authenticator app
        </button>
      </Card>
    )
  }

  return (
    <Card title="Set up two-factor">
      <p>Scan this code with an authenticator app, or type the key by hand.</p>
      <QrCode svg={enrollment.qrSvg} label="QR code for your authenticator app" />
      <p>
        Key: <code className="break-all">{enrollment.secret}</code>
      </p>
      {action.error ? <Notice kind="error">{action.error}</Notice> : null}
      <form onSubmit={confirm}>
        <Field
          label="Code from the app"
          inputMode="numeric"
          autoComplete="one-time-code"
          value={code}
          onChange={(e) => {
            setCode(e.target.value)
          }}
          required
        />
        <button className="button" type="submit" disabled={action.busy}>
          Turn on two-factor
        </button>
      </form>
    </Card>
  )
}
