import { useId, useState, type FormEvent } from 'react'
import { verifyMfa } from '../api/auth'
import { RequireTwoFactorStep } from '../auth/guards'
import { useSession } from '../auth/SessionContext'
import { Notice, useAction } from '../components/forms'
import { NO_CODE, SecondFactorField, complete, type SecondFactor } from '../components/SecondFactorField'
import { Button } from '../components/ui/Button'
import { useSignOut } from '../components/useSignOut'

/**
 * The second step after sign-in (docs/design/mockups/src/Auth.dc.html, "Second factor"): a code
 * from the authenticator app, or a recovery code. A correct code only reads the session again;
 * the guard then sends the user on. The step expires ten minutes after the sign-in (ADR 0003
 * section 2), and "Not you? Sign out" leaves it.
 */
export function TwoFactorChallengePage() {
  return (
    <RequireTwoFactorStep step="challenge">
      <Challenge />
    </RequireTwoFactorStep>
  )
}

function Challenge() {
  const titleId = useId()
  const { session, refresh } = useSession()
  const { signOut, busy: leaving } = useSignOut()
  const [factor, setFactor] = useState<SecondFactor>(NO_CODE)
  const action = useAction()

  const submit = (event: FormEvent) => {
    event.preventDefault()
    void action.run(async () => {
      await verifyMfa(factor.code.trim())
      await refresh()
    })
  }

  return (
    <section className="ov-auth" aria-labelledby={titleId}>
      <form className="ov-auth__form" onSubmit={submit}>
        <span className="ov-eyebrow">Step 2 of 2</span>
        <h1 id={titleId} className="ov-auth__title">
          Enter your code
        </h1>
        <p className="ov-muted">Open your authenticator app and enter the 6-digit code for ownvitals.</p>
        {action.error ? <Notice kind="error">{action.error}</Notice> : null}
        <SecondFactorField value={factor} onChange={setFactor} autoFocus disabled={action.busy} />
        <Button variant="primary" wide type="submit" disabled={action.busy || !complete(factor)}>
          Verify
        </Button>
      </form>
      <div className="ov-auth__foot ov-small ov-muted">
        <span>
          Signed in as {session?.user?.email ?? 'you'}. This step expires in 10 minutes.
        </span>
        <button className="ov-linkbutton" type="button" disabled={leaving} onClick={signOut}>
          Not you? Sign out
        </button>
      </div>
    </section>
  )
}
