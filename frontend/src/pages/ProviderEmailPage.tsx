import { useEffect, useState, type FormEvent } from 'react'
import { getPendingSignup, submitPendingEmail, type PendingSignup } from '../api/auth'
import { providerName } from '../api/messages'
import { Card, Field, Notice, useAction } from '../components/forms'
import { Link } from '../router/Router'

/**
 * The hand-off for a Google sign-in whose email is missing or unverified. Only an address is
 * sent; the provider identity stays on the server, tied to this browser.
 */
export function ProviderEmailPage() {
  const [pending, setPending] = useState<PendingSignup | null>(null)
  const [email, setEmail] = useState('')
  const [sentTo, setSentTo] = useState<string | null>(null)
  const action = useAction()

  useEffect(() => {
    const controller = new AbortController()
    getPendingSignup(controller.signal).then(setPending, () => {
      setPending({ pending: false, provider: null, awaitingEmail: false })
    })
    return () => {
      controller.abort()
    }
  }, [])

  const submit = (event: FormEvent) => {
    event.preventDefault()
    void action.run(async () => {
      await submitPendingEmail(email.trim())
      setSentTo(email.trim())
    })
  }

  if (pending === null) {
    return (
      <Card title="Almost there">
        <p>Loading</p>
      </Card>
    )
  }

  if (sentTo !== null) {
    return (
      <Card title="Check your inbox">
        <Notice kind="success">
          If we can use {sentTo}, a confirmation link is on its way. Open it in this same browser
          to finish. The link works for 24 hours.
        </Notice>
        <p>
          Mistyped the address? Nothing more can be sent from this page, because a second try
          would not send anything. Start the Google sign-in again to get a fresh chance.
        </p>
        <p className="card__links">
          <Link to="/signin">Start the Google sign-in again</Link>
        </p>
      </Card>
    )
  }

  if (!pending.pending || !pending.awaitingEmail) {
    return (
      <Card title="Nothing to finish here">
        <Notice kind="info">
          No Google sign-in is waiting for an address in this browser. It may have expired
          (after 15 minutes) or already been finished.
        </Notice>
        <p className="card__links">
          <Link to="/signin">Back to sign in</Link>
        </p>
      </Card>
    )
  }

  const name = pending.provider === null ? 'your provider' : providerName(pending.provider)
  return (
    <Card title="Confirm your email address">
      <p>
        {name} did not confirm an email address for you. Enter the address to use for ownvitals;
        we will mail you a link to confirm it.
      </p>
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
          Send confirmation link
        </button>
      </form>
    </Card>
  )
}
