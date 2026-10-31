import { useState, type FormEvent } from 'react'
import { signUp } from '../api/auth'
import { Card, Field, Notice, useAction } from '../components/forms'
import { Link } from '../router/Router'

export function SignUpPage() {
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [invite, setInvite] = useState('')
  const [sent, setSent] = useState(false)
  const action = useAction()

  const submit = (event: FormEvent) => {
    event.preventDefault()
    void action.run(async () => {
      await signUp({ email, password, inviteCode: invite.trim() })
      setSent(true)
    })
  }

  if (sent) {
    return (
      <Card title="Check your inbox">
        <Notice kind="success">
          If this address can be used, we sent a link to it. Open the link and type your password
          again to verify your email address and finish. The link works for 24 hours.
        </Notice>
        <p className="card__links">
          <Link to="/signin">Back to sign in</Link>
        </p>
      </Card>
    )
  }

  return (
    <Card title="Create an account">
      <p>ownvitals is invite-only for now. Enter the invite code you were given.</p>
      {action.error ? <Notice kind="error">{action.error}</Notice> : null}
      <form onSubmit={submit}>
        <Field
          label="Invite code"
          autoComplete="off"
          value={invite}
          onChange={(e) => {
            setInvite(e.target.value)
          }}
        />
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
        <Field
          label="Password"
          type="password"
          autoComplete="new-password"
          hint="12 to 128 characters. You will type it again when you confirm."
          value={password}
          onChange={(e) => {
            setPassword(e.target.value)
          }}
          required
        />
        <button className="button" type="submit" disabled={action.busy}>
          Create account
        </button>
      </form>
      <p className="card__links">
        <Link to="/signin">Already have an account? Sign in</Link>
      </p>
    </Card>
  )
}
