import { useId, useState, type FormEvent } from 'react'
import { verifyMfa } from '../api/auth'
import { RequireTwoFactorStep } from '../auth/guards'
import { useSession } from '../auth/SessionContext'
import { Notice, useAction } from '../components/forms'
import { NO_CODE, SecondFactorField, complete, type SecondFactor } from '../components/SecondFactorField'
import { Button } from '../components/ui/Button'
import { useSignOut } from '../components/useSignOut'
import { useT } from '../i18n'

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
  const t = useT()
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
        <span className="ov-eyebrow">{t('auth.challenge.step')}</span>
        <h1 id={titleId} className="ov-auth__title">
          {t('auth.challenge.title')}
        </h1>
        <p className="ov-muted">{t('auth.challenge.prompt')}</p>
        {action.error ? <Notice kind="error">{action.error}</Notice> : null}
        <SecondFactorField value={factor} onChange={setFactor} autoFocus disabled={action.busy} />
        <Button variant="primary" wide type="submit" disabled={action.busy || !complete(factor)}>
          {t('auth.challenge.verify')}
        </Button>
      </form>
      <div className="ov-auth__foot ov-small ov-muted">
        <span>
          {session?.user?.email === undefined
            ? t('auth.challenge.signedInAsYou')
            : t('auth.challenge.signedInAs', { email: session.user.email })}
        </span>
        <button className="ov-linkbutton" type="button" disabled={leaving} onClick={signOut}>
          {t('auth.challenge.notYou')}
        </button>
      </div>
    </section>
  )
}
