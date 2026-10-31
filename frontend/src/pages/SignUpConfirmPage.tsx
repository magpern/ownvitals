import { useState, type FormEvent } from 'react'
import { confirmSignUp } from '../api/auth'
import { Card, Field, Notice, useAction } from '../components/forms'
import { Link, useMailToken } from '../router/Router'

/**
 * The page behind the sign-up confirmation mail (`/signup/confirm#token=...`). The token is
 * read from the fragment and removed from the address bar. Confirming never signs the browser
 * in: the user signs in afterwards.
 */
export function SignUpConfirmPage() {
  const token = useMailToken()
  const [password, setPassword] = useState('')
  const [done, setDone] = useState(false)
  const action = useAction()

  if (token === null) {
    return (
      <Card title="Confirm your account">
        <Notice kind="error">
          This page needs the link from the confirmation mail. Open the link again, or sign up
          again to get a new one.
        </Notice>
        <p className="card__links">
          <Link to="/signup">Create an account</Link>
        </p>
      </Card>
    )
  }

  if (done) {
    return (
      <Card title="Account created">
        <Notice kind="success">Your account is ready. Sign in to continue.</Notice>
        <p>
          <Link className="button" to="/signin">
            Sign in
          </Link>
        </p>
      </Card>
    )
  }

  const submit = (event: FormEvent) => {
    event.preventDefault()
    void action.run(async () => {
      // A provider sign-up has no password: the server then checks the browser instead.
      await confirmSignUp(token, password === '' ? null : password)
      setDone(true)
    })
  }

  return (
    <Card title="Confirm your account">
      <p>
        Type the password you chose when you signed up. If you signed up with Google, leave it
        empty.
      </p>
      {action.error ? <Notice kind="error">{action.error}</Notice> : null}
      <form onSubmit={submit}>
        <Field
          label="Password"
          type="password"
          autoComplete="current-password"
          value={password}
          onChange={(e) => {
            setPassword(e.target.value)
          }}
        />
        <button className="button" type="submit" disabled={action.busy}>
          Confirm account
        </button>
      </form>
      <p className="card__links">
        <Link to="/signin">Sign in</Link>
      </p>
    </Card>
  )
}
