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
import { useTurnstile } from '../components/Turnstile'
import { useT } from '../i18n'
import { Link, useMailToken } from '../router/Router'

/** `/reset`: ask for a link. `/reset#token=...`: choose a new password. */
export function ResetPage() {
  const token = useMailToken()
  return token === null ? <RequestReset /> : <ChoosePassword token={token} />
}

function RequestReset() {
  const t = useT()
  const [email, setEmail] = useState('')
  const [sent, setSent] = useState(false)
  const action = useAction()
  const turnstile = useTurnstile('reset')

  const submit = (event: FormEvent) => {
    event.preventDefault()
    void action.run(async () => {
      try {
        await requestPasswordReset(email.trim(), turnstile.token)
      } finally {
        turnstile.reset() // a token is single-use
      }
      setSent(true)
    })
  }

  if (sent) {
    return (
      <Card title={t('auth.checkInbox')}>
        <Notice kind="success">{t('auth.reset.sent')}</Notice>
        <p className="card__links">
          <Link to="/signin">{t('auth.backToSignIn')}</Link>
        </p>
      </Card>
    )
  }

  return (
    <Card title={t('auth.reset.title')}>
      <p>{t('auth.reset.prompt')}</p>
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
        {turnstile.widget}
        <button className="button" type="submit" disabled={action.busy || (turnstile.required && !turnstile.token)}>
          {t('auth.reset.submit')}
        </button>
      </form>
      <p className="card__links">
        <Link to="/signin">{t('auth.backToSignIn')}</Link>
      </p>
    </Card>
  )
}

type Methods = { state: 'loading' } | { state: 'bad'; message: string } | { state: 'ok'; list: LinkedMethod[] }

function ChoosePassword({ token }: { token: string }) {
  const t = useT()
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
      <Card title={t('auth.reset.doneTitle')}>
        <Notice kind="success">{t('auth.reset.done')}</Notice>
        <p>
          <Link className="button" to="/signin">
            {t('common.signIn')}
          </Link>
        </p>
      </Card>
    )
  }
  if (methods.state === 'loading') {
    return (
      <Card title={t('auth.reset.chooseTitle')}>
        <p>{t('auth.reset.checking')}</p>
      </Card>
    )
  }
  if (methods.state === 'bad') {
    return (
      <Card title={t('auth.reset.chooseTitle')}>
        <Notice kind="error">{methods.message}</Notice>
        <p className="card__links">
          <Link to="/reset">{t('auth.reset.newLink')}</Link>
        </p>
      </Card>
    )
  }

  return (
    <Card title={t('auth.reset.chooseTitle')}>
      {action.error ? <Notice kind="error">{action.error}</Notice> : null}
      <form onSubmit={submit}>
        <Field
          label={t('auth.reset.newPassword')}
          type="password"
          autoComplete="new-password"
          hint={t('auth.reset.passwordHint')}
          value={password}
          onChange={(e) => {
            setPassword(e.target.value)
          }}
          required
        />
        {methods.list.length > 0 ? (
          <fieldset className="fieldset">
            <legend>{t('auth.reset.linked')}</legend>
            <p className="field__hint">{t('auth.reset.linkedHint')}</p>
            {methods.list.map((method) => (
              <label key={method.id} className="check">
                <input
                  type="checkbox"
                  checked={remove.has(method.id)}
                  onChange={() => {
                    toggle(method.id)
                  }}
                />
                {t('auth.reset.remove', { provider: providerName(method.provider) })}
              </label>
            ))}
          </fieldset>
        ) : null}
        <button className="button" type="submit" disabled={action.busy}>
          {t('auth.reset.change')}
        </button>
      </form>
    </Card>
  )
}
