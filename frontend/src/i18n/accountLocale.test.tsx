import { fireEvent, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { SIGNED_IN, SIGNED_IN_NO_TOTP, fakeApi, reason } from '../test/fakeApi'
import { renderAt } from '../test/render'
import { getLocale, setLocale } from './store'
import { STORAGE_KEY } from './resolve'

// The account's language (ADR 0015 section 2, #249): the session's value goes first, and the
// "Language" row of Settings → Profile and display writes it. The Swedish catalogue is English copies until
// the translation PR, so the test swaps in one with a real difference. Synthetic data only.
vi.mock('./sv', async () => {
  const { EN } = await import('./en')
  return { SV: { ...EN, 'account.language.label': 'Språk' } }
})

const withLocale = (locale: string | null) => ({ ...SIGNED_IN, user: { ...SIGNED_IN.user, locale } })
const SESSIONS = {}

afterEach(async () => {
  window.localStorage.clear()
  await setLocale('en')
  window.localStorage.clear()
})

/** The row of the profile page (the signed-out frame's picker shows while the session loads). */
async function languageRow(): Promise<HTMLSelectElement> {
  await screen.findByText(/^(Display|Visning)$/)
  return screen.getByRole<HTMLSelectElement>('combobox', { name: /^(Language|Språk)$/ })
}

describe("the account's language", () => {
  it('comes before the choice stored in this browser, without replacing it', async () => {
    window.localStorage.setItem(STORAGE_KEY, 'en')
    fakeApi({ 'GET /api/auth/session': withLocale('sv'), ...SESSIONS })
    renderAt('/settings/profile')
    await waitFor(() => {
      expect(document.documentElement.lang).toBe('sv')
    })
    expect((await languageRow()).value).toBe('sv')
    expect(window.localStorage.getItem(STORAGE_KEY)).toBe('en')
  })

  it('follows this browser when the account has none, or one this build does not know', async () => {
    window.localStorage.setItem(STORAGE_KEY, 'sv')
    fakeApi({ 'GET /api/auth/session': withLocale('xx'), ...SESSIONS })
    renderAt('/settings/profile')
    expect((await languageRow()).value).toBe('')
    await waitFor(() => {
      expect(getLocale()).toBe('sv')
    })
  })

  it('is written by the Language row and applied at once', async () => {
    let stored: string | null = null
    const api = fakeApi({
      'GET /api/auth/session': () => withLocale(stored),
      'PUT /api/account/locale': (body: unknown) => {
        stored = (body as { locale: string | null }).locale
        return { locale: stored }
      },
      ...SESSIONS,
    })
    renderAt('/settings/profile')
    const row = await languageRow()
    expect(row.value).toBe('')
    expect(screen.getByRole('option', { name: "This browser's language" })).toBeInTheDocument()
    fireEvent.change(row, { target: { value: 'sv' } })
    await waitFor(() => {
      expect(document.documentElement.lang).toBe('sv')
    })
    expect(api.to('PUT', '/api/account/locale')[0]?.body).toEqual({ locale: 'sv' })
    expect(api.to('PUT', '/api/account/locale')[0]?.headers.get('X-CSRF-Token')).toBeTruthy()
    expect(await screen.findByRole('status')).toHaveTextContent('Language saved.')
    fireEvent.change(await languageRow(), { target: { value: '' } })
    await waitFor(() => {
      expect(api.to('PUT', '/api/account/locale')[1]?.body).toEqual({ locale: null })
    })
    await waitFor(() => {
      expect(screen.getByRole<HTMLSelectElement>('combobox', { name: /^(Language|Språk)$/ }).value).toBe('')
    })
  })

  it("is dropped at sign-out: the browser's own choice holds again (#282)", async () => {
    window.localStorage.setItem(STORAGE_KEY, 'en')
    let out = false
    fakeApi({
      'GET /api/auth/session': () =>
        out ? { authenticated: false, csrf_token: 'a' } : { ...SIGNED_IN_NO_TOTP, user: { ...SIGNED_IN.user, locale: 'sv' } },
      'POST /api/auth/logout': () => {
        out = true
        return new Response(null, { status: 204 })
      },
    })
    renderAt('/two-factor/setup')
    await waitFor(() => {
      expect(document.documentElement.lang).toBe('sv')
    })
    fireEvent.click(await screen.findByRole('button', { name: 'Sign out' }))
    expect(await screen.findByRole('heading', { name: 'Sign in' })).toBeInTheDocument()
    await waitFor(() => {
      expect(document.documentElement.lang).toBe('en')
    })
    expect(getLocale()).toBe('en')
    expect(window.localStorage.getItem(STORAGE_KEY)).toBe('en')
  })

  it('shows a refusal and keeps the stored value', async () => {
    fakeApi({
      'GET /api/auth/session': withLocale(null),
      'PUT /api/account/locale': reason(400, 'locale_not_offered'),
      ...SESSIONS,
    })
    renderAt('/settings/profile')
    fireEvent.change(await languageRow(), { target: { value: 'sv' } })
    expect(await screen.findByRole('alert')).toBeInTheDocument()
    expect((await languageRow()).value).toBe('')
    expect(getLocale()).toBe('en')
  })
})
