import { useState, type FormEvent } from 'react'
import { verifyMfa } from '../api/auth'
import { RequireTwoFactorStep } from '../auth/guards'
import { useSession } from '../auth/SessionContext'
import { Card, Field, Notice, useAction } from '../components/forms'

/**
 * The second step after sign-in: a code from the authenticator app, or a recovery code. A
 * correct code only reads the session again; the guard then sends the user on.
 */
export function TwoFactorChallengePage() {
  return (
    <RequireTwoFactorStep step="challenge">
      <Challenge />
    </RequireTwoFactorStep>
  )
}

function Challenge() {
  const { refresh } = useSession()
  const [code, setCode] = useState('')
  const action = useAction()

  const submit = (event: FormEvent) => {
    event.preventDefault()
    void action.run(async () => {
      await verifyMfa(code.trim())
      await refresh()
    })
  }

  return (
    <Card title="Enter your code">
      <p>Type the code from your authenticator app, or one of your recovery codes.</p>
      {action.error ? <Notice kind="error">{action.error}</Notice> : null}
      <form onSubmit={submit}>
        <Field
          label="Authenticator or recovery code"
          autoComplete="one-time-code"
          value={code}
          onChange={(e) => {
            setCode(e.target.value)
          }}
          required
        />
        <button className="button" type="submit" disabled={action.busy}>
          Verify
        </button>
      </form>
    </Card>
  )
}
