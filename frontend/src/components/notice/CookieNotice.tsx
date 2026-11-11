import '../../styles/cookie-notice.css'
import { useState } from 'react'
import { useLocale, useT } from '../../i18n'
import { cookiePagePath } from './cookiePath'
import { hasSeenCookieNotice, markCookieNoticeSeen } from './cookieNoticeStore'

/**
 * The first-visit cookie notice. The app sets only strictly necessary cookies and settings
 * (theme, language), so there is nothing to accept or reject: it says so once, links to the cookie
 * statement and is remembered in localStorage. Not modal: it does not take focus or block sign-in,
 * and being fixed it moves no content. The prerendered public pages set nothing and run no
 * script (ADR 0016), so they carry no notice; their footer says so.
 */
export function CookieNotice() {
  const t = useT()
  const locale = useLocale()
  const [open, setOpen] = useState(() => !hasSeenCookieNotice())
  if (!open) return null
  const dismiss = () => {
    markCookieNoticeSeen()
    setOpen(false)
  }
  return (
    <div className="cookie-notice" role="dialog" aria-modal="false" aria-labelledby="cookie-notice-title">
      <h2 className="cookie-notice__title" id="cookie-notice-title">
        {t('shell.cookies.title')}
      </h2>
      <p className="cookie-notice__text">
        {t('shell.cookies.text')} <a href={cookiePagePath(locale)}>{t('shell.cookies.link')}</a>
      </p>
      <button className="button" type="button" onClick={dismiss}>
        {t('shell.cookies.ok')}
      </button>
    </div>
  )
}
