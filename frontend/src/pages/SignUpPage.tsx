import { useState, type FormEvent } from 'react'
import { signUp } from '../api/auth'
import { Card, Field, Notice, useAction } from '../components/forms'
import { useT } from '../i18n'
import { Link } from '../router/Router'

export function SignUpPage() {
  const t = useT()
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
      <Card title={t('auth.checkInbox')}>
        <Notice kind="success">{t('auth.signUp.sent')}</Notice>
        <p className="card__links">
          <Link to="/signin">{t('auth.backToSignIn')}</Link>
        </p>
      </Card>
    )
  }

  return (
    <Card title={t('auth.signUp.title')}>
      <p>{t('auth.signUp.invitePrompt')}</p>
      {action.error ? <Notice kind="error">{action.error}</Notice> : null}
      <form onSubmit={submit}>
        <Field
          label={t('auth.signUp.inviteCode')}
          autoComplete="off"
          value={invite}
          onChange={(e) => {
            setInvite(e.target.value)
          }}
        />
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
        <Field
          label={t('common.password')}
          type="password"
          autoComplete="new-password"
          hint={t('auth.signUp.passwordHint')}
          value={password}
          onChange={(e) => {
            setPassword(e.target.value)
          }}
          required
        />
        <button className="button" type="submit" disabled={action.busy}>
          {t('auth.signUp.submit')}
        </button>
      </form>
      <p className="card__links">
        <Link to="/signin">{t('auth.signUp.haveAccount')}</Link>
      </p>
    </Card>
  )
}
