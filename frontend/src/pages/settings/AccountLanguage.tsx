import { useId, useState } from 'react'
import { setAccountLocale } from '../../api/auth'
import { errorMessage } from '../../api/messages'
import { useSession } from '../../auth/SessionContext'
import { Notice } from '../../components/forms'
import { t as translate, useT } from '../../i18n'
import { LOCALES, LOCALE_NAMES, isLocale } from '../../i18n/locale'

/** The account's language: "This browser's language" (null) or a code; applied at once. */
export function AccountLanguage() {
  const t = useT()
  const id = useId()
  const { session, refresh } = useSession()
  const [busy, setBusy] = useState(false)
  const [note, setNote] = useState<{ kind: 'success' | 'error'; text: string } | null>(null)

  const choose = (raw: string) => {
    setBusy(true)
    setNote(null)
    setAccountLocale(isLocale(raw) ? raw : null)
      .then(async () => {
        await refresh() // the session carries the value, and the page follows it
        setNote({ kind: 'success', text: translate('account.language.saved') })
      })
      .catch((caught: unknown) => {
        setNote({ kind: 'error', text: errorMessage(caught) })
      })
      .finally(() => {
        setBusy(false)
      })
  }

  return (
    <div className="ov-setting">
      <div className="ov-setting__what">
        <label className="ov-setting__label" htmlFor={id}>
          {t('account.language.label')}
        </label>
        <p>{t('account.language.help')}</p>
        {note !== null ? <Notice kind={note.kind}>{note.text}</Notice> : null}
      </div>
      <select
        id={id}
        className="ov-input"
        value={session?.user?.locale ?? ''}
        disabled={busy}
        onChange={(event) => {
          choose(event.target.value)
        }}
      >
        <option value="">{t('account.language.followBrowser')}</option>
        {LOCALES.map((code) => (
          <option key={code} value={code} lang={code}>
            {LOCALE_NAMES[code]}
          </option>
        ))}
      </select>
    </div>
  )
}
