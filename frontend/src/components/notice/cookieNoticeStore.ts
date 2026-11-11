// Whether the visitor has seen the cookie notice (docs/legal/cookies.en.md). The notice informs; it
// is not a consent choice, because the app sets only strictly necessary storage. The version makes
// a changed cookie statement show it again. Every access is in try/catch: storage can throw or be
// empty in a private window, and the page must work without it.

export const COOKIE_NOTICE_KEY = 'ownvitals.cookie-notice'
export const COOKIE_NOTICE_VERSION = '1'

export function hasSeenCookieNotice(): boolean {
  try {
    return window.localStorage.getItem(COOKIE_NOTICE_KEY) === COOKIE_NOTICE_VERSION
  } catch {
    return false
  }
}

export function markCookieNoticeSeen(): void {
  try {
    window.localStorage.setItem(COOKIE_NOTICE_KEY, COOKIE_NOTICE_VERSION)
  } catch {
    // The notice then shows again on the next visit.
  }
}
