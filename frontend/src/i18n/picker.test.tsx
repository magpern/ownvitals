import { fireEvent, screen, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { SIGNED_IN, fakeApi } from '../test/fakeApi'
import { renderAt } from '../test/render'
import { getLocale, initLocale, setLocale } from './store'

// The Swedish catalogue is English copies until the translation PR, so the test swaps in a
// catalogue with a few real differences. Everything else is the real runtime.
vi.mock('./sv', async () => {
  const { EN } = await import('./en')
  return {
    SV: {
      ...EN,
      'common.signIn': 'Logga in',
      'common.language': 'Språk',
      'common.email': 'E-post',
      'auth.signIn.forgot': 'Glömt lösenordet?',
      'auth.signUp.title': 'Skapa ett konto',
    },
  }
})

afterEach(async () => {
  window.localStorage.clear()
  await setLocale('en')
  window.localStorage.clear()
})

function picker() {
  return screen.getByRole('combobox', { name: /Language|Språk/ })
}

describe('the language picker', () => {
  it('is in the footer of the signed-out pages, with each language by its own name', async () => {
    fakeApi({})
    renderAt('/signin')
    await screen.findByRole('heading', { name: 'Sign in' })
    const footer = screen.getByRole('contentinfo')
    const options = within(footer).getAllByRole('option')
    expect(options.map((option) => option.textContent)).toEqual(['English', 'Svenska'])
    expect(picker()).toHaveValue('en')
  })

  it('re-renders the page in the chosen language with no reload, and keeps what was typed', async () => {
    fakeApi({})
    renderAt('/signin')
    await screen.findByRole('heading', { name: 'Sign in' })
    fireEvent.change(screen.getByLabelText('Email'), { target: { value: 'ann@example.test' } })

    fireEvent.change(picker(), { target: { value: 'sv' } })

    expect(await screen.findByRole('heading', { name: 'Logga in' })).toBeInTheDocument()
    expect(screen.getByText('Glömt lösenordet?')).toBeInTheDocument()
    expect(screen.getByLabelText('E-post')).toHaveValue('ann@example.test')
    expect(document.documentElement.lang).toBe('sv')
    expect(picker()).toHaveValue('sv')
    expect(window.location.pathname).toBe('/signin')
  })

  it('applies to the other signed-out pages too, and back again', async () => {
    fakeApi({})
    renderAt('/signup')
    await screen.findByRole('heading', { name: 'Create an account' })
    fireEvent.change(picker(), { target: { value: 'sv' } })
    expect(await screen.findByRole('heading', { name: 'Skapa ett konto' })).toBeInTheDocument()
    fireEvent.change(picker(), { target: { value: 'en' } })
    expect(await screen.findByRole('heading', { name: 'Create an account' })).toBeInTheDocument()
    expect(document.documentElement.lang).toBe('en')
  })

  it('keeps the choice for the next visit', async () => {
    fakeApi({})
    renderAt('/signin')
    await screen.findByRole('heading', { name: 'Sign in' })
    fireEvent.change(picker(), { target: { value: 'sv' } })
    await screen.findByRole('heading', { name: 'Logga in' })
    expect(window.localStorage.getItem('ov-locale')).toBe('sv')

    // A new visit: the app starts in English, then reads the stored choice.
    await setLocale('en')
    window.localStorage.setItem('ov-locale', 'sv')
    const pending = initLocale()
    expect(pending).toBeNull() // the chunk is loaded already
    expect(getLocale()).toBe('sv')
    renderAt('/signin')
    expect(await screen.findByRole('heading', { name: 'Logga in' })).toBeInTheDocument()
  })

  it('starts from the browser language when nothing is stored, and ignores an unknown stored code', async () => {
    vi.spyOn(window.navigator, 'languages', 'get').mockReturnValue(['sv-SE', 'en'])
    window.localStorage.setItem('ov-locale', 'xx')
    await initLocale()
    expect(getLocale()).toBe('sv')
    window.localStorage.removeItem('ov-locale')
    await setLocale('en')
    window.localStorage.removeItem('ov-locale')
    await initLocale()
    expect(getLocale()).toBe('sv')
  })

  it('is not shown to a signed-in user', async () => {
    fakeApi({ 'GET /api/auth/session': SIGNED_IN })
    renderAt('/two-factor')
    await screen.findByText(/ann@example.test/)
    expect(screen.queryByRole('combobox')).toBeNull()
  })

  it('works when storage is blocked', async () => {
    fakeApi({})
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new Error('blocked')
    })
    renderAt('/signin')
    await screen.findByRole('heading', { name: 'Sign in' })
    fireEvent.change(picker(), { target: { value: 'sv' } })
    expect(await screen.findByRole('heading', { name: 'Logga in' })).toBeInTheDocument()
  })
})
