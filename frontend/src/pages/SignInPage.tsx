import { useEffect, useState, type FormEvent } from 'react'
import { login } from '../api/auth'
import { oauthOutcomeMessage } from '../api/messages'
import { passkeysSupported, signInWithPasskey } from '../api/passkeys'
import { useSession } from '../auth/SessionContext'
import { Card, Field, Notice, useAction } from '../components/forms'
import { passkeyError } from '../components/PasskeyList'
import { ProviderButtons } from '../components/ProviderButtons'
import { QrLoginPanel } from '../components/QrLoginPanel'
import { useT } from '../i18n'
import { Link, safeReturnTo, useQuery, useRouter } from '../router/Router'

/**
 * Sign in with a password, a passkey (ADR 0011 section 1; the browser offers this device's
 * passkeys, a security key and "Use a phone"), your phone (the QR of section 5) or Google.
 * Signing in only reads the session again: the `RequireSignedOut` guard around this page then
 * sends the user on, to `return_to`, to enrolment or to the code prompt. A passkey replaces the
 * password, never the authenticator code.
 */
export function SignInPage() {
  const t = useT()
  const { navigate } = useRouter()
  const { refresh } = useSession()
  const query = useQuery()
  const outcome = query.get('oauth')
  const returnTo = safeReturnTo(query.get('return_to'))
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [phone, setPhone] = useState(false)
  const action = useAction()

  useEffect(() => {
    // A Google sign-in without a verified address: the address page finishes it.
    if (outcome === 'email_needed') navigate('/signin/email', { replace: true })
  }, [outcome, navigate])

  const submit = (event: FormEvent) => {
    event.preventDefault()
    void action.run(async () => {
      await login(email, password)
      await refresh()
    })
  }

  const withPasskey = () => {
    void action.run(
      async () => {
        await signInWithPasskey()
        await refresh()
      },
      (error) => passkeyError(error, t) ?? t('passkeys.cancelled'),
    )
  }

  if (phone) {
    return (
      <Card title={t('common.signIn')}>
        <QrLoginPanel
          onClose={() => {
            setPhone(false)
          }}
          onSignedIn={() => {
            void refresh()
          }}
        />
      </Card>
    )
  }

  return (
    <Card title={t('common.signIn')}>
      {outcome && outcome !== 'email_needed' ? (
        <Notice kind="error">{oauthOutcomeMessage(outcome)}</Notice>
      ) : null}
      {action.error ? <Notice kind="error">{action.error}</Notice> : null}
      <form onSubmit={submit}>
        <Field
          label={t('common.email')}
          type="email"
          autoComplete="username"
          value={email}
          onChange={(e) => {
            setEmail(e.target.value)
          }}
          required
        />
        <Field
          label={t('common.password')}
          type="password"
          autoComplete="current-password"
          value={password}
          onChange={(e) => {
            setPassword(e.target.value)
          }}
          required
        />
        <button className="button" type="submit" disabled={action.busy}>
          {t('common.signIn')}
        </button>
      </form>
      <div className="ov-signin-alt">
        <p className="divider">{t('auth.providers.or')}</p>
        {passkeysSupported() ? (
          <>
            <button className="button button--secondary" type="button" disabled={action.busy} onClick={withPasskey}>
              {t('passkeys.signIn.passkey')}
            </button>
            <p className="field__hint">{t('passkeys.signIn.passkeyHint')}</p>
          </>
        ) : null}
        <button
          className="button button--secondary"
          type="button"
          disabled={action.busy}
          onClick={() => {
            action.clear()
            setPhone(true)
          }}
        >
          {t('passkeys.signIn.phone')}
        </button>
      </div>
      <ProviderButtons returnTo={returnTo} />
      <p className="card__links">
        <Link to="/reset">{t('auth.signIn.forgot')}</Link>
        <Link to="/signup">{t('auth.signIn.create')}</Link>
      </p>
    </Card>
  )
}
