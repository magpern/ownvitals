import { useEffect, useState, type FormEvent } from 'react'
import { getPendingSignup, submitPendingEmail, type PendingSignup } from '../api/auth'
import { providerName } from '../api/messages'
import { Card, Field, Notice, useAction } from '../components/forms'
import { useT } from '../i18n'
import { Link } from '../router/Router'

/**
 * The hand-off for a Google sign-in whose email is missing or unverified. Only an address is
 * sent; the provider identity stays on the server, tied to this browser.
 */
export function ProviderEmailPage() {
  const t = useT()
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
      <Card title={t('auth.provider.loadingTitle')}>
        <p>{t('common.loading')}</p>
      </Card>
    )
  }

  if (sentTo !== null) {
    return (
      <Card title={t('auth.checkInbox')}>
        <Notice kind="success">{t('auth.provider.sent', { email: sentTo })}</Notice>
        <p>{t('auth.provider.mistyped')}</p>
        <p className="card__links">
          <Link to="/signin">{t('auth.provider.restart')}</Link>
        </p>
      </Card>
    )
  }

  if (!pending.pending || !pending.awaitingEmail) {
    return (
      <Card title={t('auth.provider.nothingTitle')}>
        <Notice kind="info">{t('auth.provider.nothing')}</Notice>
        <p className="card__links">
          <Link to="/signin">{t('auth.backToSignIn')}</Link>
        </p>
      </Card>
    )
  }

  const name = pending.provider === null ? t('auth.provider.yourProvider') : providerName(pending.provider)
  return (
    <Card title={t('auth.provider.title')}>
      <p>{t('auth.provider.prompt', { name })}</p>
      {action.error ? <Notice kind="error">{action.error}</Notice> : null}
      <form onSubmit={submit}>
        <Field
          label={t('common.email')}
          type="email"
          autoComplete="email"
          value={email}
          onChange={(e) => {
            setEmail(e.target.value)
          }}
          required
        />
        <button className="button" type="submit" disabled={action.busy}>
          {t('auth.provider.submit')}
        </button>
      </form>
    </Card>
  )
}
