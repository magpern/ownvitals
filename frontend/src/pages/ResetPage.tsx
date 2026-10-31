import { useEffect, useState, type FormEvent } from 'react'
import {
  completeReset,
  requestPasswordReset,
  resetInfo,
  type LinkedMethod,
} from '../api/auth'
import { errorMessage, providerName } from '../api/messages'
import { useSession } from '../auth/SessionContext'
import { Card, Field, Notice, useAction } from '../components/forms'
import { Link, useMailToken } from '../router/Router'

/** `/reset`: ask for a link. `/reset#token=...`: choose a new password. */
export function ResetPage() {
  const token = useMailToken()
  return token === null ? <RequestReset /> : <ChoosePassword token={token} />
}

function RequestReset() {
  const [email, setEmail] = useState('')
  const [sent, setSent] = useState(false)
  const action = useAction()

  const submit = (event: FormEvent) => {
    event.preventDefault()
    void action.run(async () => {
      await requestPasswordReset(email.trim())
      setSent(true)
    })
  }

  if (sent) {
    return (
      <Card title="Check your inbox">
        <Notice kind="success">
          If an account uses that address, a link to choose a new password is on its way. It works
          for 30 minutes.
        </Notice>
        <p className="card__links">
          <Link to="/signin">Back to sign in</Link>
        </p>
      </Card>
    )
  }

  return (
    <Card title="Reset your password">
      <p>Enter your email address and we will mail you a link.</p>
      {action.error ? <Notice kind="error">{action.error}</Notice> : null}
      <form onSubmit={submit}>
        <Field
          label="Email"
          type="email"
          autoComplete="email"
          value={email}
          onChange={(e) => {
            setEmail(e.target.value)
          }}
          required
        />
        <button className="button" type="submit" disabled={action.busy}>
          Send reset link
        </button>
      </form>
      <p className="card__links">
        <Link to="/signin">Back to sign in</Link>
      </p>
    </Card>
  )
}

type Methods = { state: 'loading' } | { state: 'bad'; message: string } | { state: 'ok'; list: LinkedMethod[] }

function ChoosePassword({ token }: { token: string }) {
  const [methods, setMethods] = useState<Methods>({ state: 'loading' })
  const [remove, setRemove] = useState<Set<string>>(new Set())
  const [password, setPassword] = useState('')
  const [done, setDone] = useState(false)
  const action = useAction()
  const { refresh } = useSession()

  useEffect(() => {
    let current = true
    resetInfo(token).then(
      (list) => {
        if (current) setMethods({ state: 'ok', list })
      },
      (error: unknown) => {
        if (current) setMethods({ state: 'bad', message: errorMessage(error) })
      },
    )
    return () => {
      current = false
    }
  }, [token])

  const toggle = (id: string) => {
    setRemove((previous) => {
      const next = new Set(previous)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  const submit = (event: FormEvent) => {
    event.preventDefault()
    void action.run(async () => {
      await completeReset(token, password, [...remove])
      // A reset revokes every session: update the nav without a reload.
      await refresh()
      setDone(true)
    })
  }

  if (done) {
    return (
      <Card title="Password changed">
        <Notice kind="success">
          Your password is changed and you are signed out everywhere. Sign in with the new one.
        </Notice>
        <p>
          <Link className="button" to="/signin">
            Sign in
          </Link>
        </p>
      </Card>
    )
  }
  if (methods.state === 'loading') {
    return (
      <Card title="Choose a new password">
        <p>Checking your link</p>
      </Card>
    )
  }
  if (methods.state === 'bad') {
    return (
      <Card title="Choose a new password">
        <Notice kind="error">{methods.message}</Notice>
        <p className="card__links">
          <Link to="/reset">Get a new link</Link>
        </p>
      </Card>
    )
  }

  return (
    <Card title="Choose a new password">
      {action.error ? <Notice kind="error">{action.error}</Notice> : null}
      <form onSubmit={submit}>
        <Field
          label="New password"
          type="password"
          autoComplete="new-password"
          hint="12 to 128 characters."
          value={password}
          onChange={(e) => {
            setPassword(e.target.value)
          }}
          required
        />
        {methods.list.length > 0 ? (
          <fieldset className="fieldset">
            <legend>Linked sign-in methods</legend>
            <p className="field__hint">
              These can also sign in to your account. Remove any you do not recognise or no longer
              use.
            </p>
            {methods.list.map((method) => (
              <label key={method.id} className="check">
                <input
                  type="checkbox"
                  checked={remove.has(method.id)}
                  onChange={() => {
                    toggle(method.id)
                  }}
                />
                Remove {providerName(method.provider)}
              </label>
            ))}
          </fieldset>
        ) : null}
        <button className="button" type="submit" disabled={action.busy}>
          Change password
        </button>
      </form>
    </Card>
  )
}
