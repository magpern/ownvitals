import { fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { setLocale } from '../../i18n'
import { CookieNotice } from './CookieNotice'
import { COOKIE_NOTICE_KEY, COOKIE_NOTICE_VERSION } from './cookieNoticeStore'
import { cookiePagePath } from './cookiePath'

beforeEach(() => window.localStorage.clear())
afterEach(async () => {
  vi.restoreAllMocks()
  await setLocale('en')
})

describe('the cookie notice', () => {
  it('shows on a first visit as a non-modal dialog with a link to the cookie statement', () => {
    render(<CookieNotice />)
    const dialog = screen.getByRole('dialog', { name: 'Cookies and local storage' })
    expect(dialog).toHaveAttribute('aria-modal', 'false')
    expect(screen.getByRole('link', { name: 'Read the cookie statement' })).toHaveAttribute('href', '/cookies')
    // Information only: no accept/reject choice, because nothing non-essential exists.
    expect(screen.getAllByRole('button')).toHaveLength(1)
  })

  it('is keyboard operable, goes away on "Got it" and is remembered', () => {
    const { unmount } = render(<CookieNotice />)
    const button = screen.getByRole('button', { name: 'Got it' })
    button.focus()
    expect(button).toHaveFocus()
    fireEvent.click(button)
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(window.localStorage.getItem(COOKIE_NOTICE_KEY)).toBe(COOKIE_NOTICE_VERSION)
    unmount()
    render(<CookieNotice />)
    expect(screen.queryByRole('dialog')).toBeNull()
  })

  it('shows again when the stored version is not the current one', () => {
    window.localStorage.setItem(COOKIE_NOTICE_KEY, '0')
    render(<CookieNotice />)
    expect(screen.getByRole('dialog')).toBeInTheDocument()
  })

  it('sets no cookie and works when storage throws', () => {
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new Error('blocked')
    })
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new Error('blocked')
    })
    render(<CookieNotice />)
    fireEvent.click(screen.getByRole('button', { name: 'Got it' }))
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(document.cookie).toBe('')
  })

  it('speaks Swedish and links to the Swedish statement', async () => {
    await setLocale('sv')
    render(<CookieNotice />)
    expect(screen.getByRole('link', { name: 'Läs cookiepolicyn' })).toHaveAttribute('href', '/sv/cookies')
    expect(cookiePagePath('en')).toBe('/cookies')
  })
})
