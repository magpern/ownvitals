import { useState, type FormEvent } from 'react'
import { confirmSignUp } from '../api/auth'
import { Card, Field, Notice, useAction } from '../components/forms'
import { useT } from '../i18n'
import { Link, useMailToken } from '../router/Router'

/**
 * The page behind the sign-up confirmation mail (`/signup/confirm#token=...`). The token is
 * read from the fragment and removed from the address bar. Confirming never signs the browser
 * in: the user signs in afterwards.
 */
export function SignUpConfirmPage() {
  const t = useT()
  const token = useMailToken()
  const [password, setPassword] = useState('')
  const [done, setDone] = useState(false)
  const action = useAction()

  if (token === null) {
    return (
      <Card title={t('auth.confirm.title')}>
        <Notice kind="error">{t('auth.confirm.missing')}</Notice>
        <p className="card__links">
          <Link to="/signup">{t('auth.signIn.create')}</Link>
        </p>
      </Card>
    )
  }

  if (done) {
    return (
      <Card title={t('auth.confirm.doneTitle')}>
        <Notice kind="success">{t('auth.confirm.done')}</Notice>
        <p>
          <Link className="button" to="/signin">
            {t('common.signIn')}
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
    <Card title={t('auth.confirm.title')}>
      <p>{t('auth.confirm.prompt')}</p>
      {action.error ? <Notice kind="error">{action.error}</Notice> : null}
      <form onSubmit={submit}>
        <Field
          label={t('common.password')}
          type="password"
          autoComplete="current-password"
          value={password}
          onChange={(e) => {
            setPassword(e.target.value)
          }}
        />
        <button className="button" type="submit" disabled={action.busy}>
          {t('auth.confirm.submit')}
        </button>
      </form>
      <p className="card__links">
        <Link to="/signin">{t('common.signIn')}</Link>
      </p>
    </Card>
  )
}
