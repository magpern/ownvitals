import { fireEvent, render, screen, within } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { App } from './App'
import { SIGNED_IN, SIGNED_IN_NO_TOTP, fakeApi, reason } from './test/fakeApi'
import { renderAt } from './test/render'

const here = () => window.location.pathname + window.location.search

/** Opens the account menu of the app frame and returns it. */
async function openAccountMenu() {
  fireEvent.click(await screen.findByRole('button', { name: /ann@example.test/ }))
  return screen.findByRole('menu', { name: 'Account' })
}

describe('the page frame', () => {
  it('shows the product name before the session has loaded', () => {
    vi.stubGlobal('fetch', vi.fn(() => new Promise<Response>(() => undefined)))
    window.history.replaceState(null, '', '/')
    render(<App />)
    expect(within(screen.getByRole('banner')).getByRole('link', { name: 'ownvitals' })).toHaveAttribute('href', '/')
  })

  it('offers sign in and sign up to a signed-out visitor', async () => {
    fakeApi({})
    renderAt('/signin')
    const nav = await screen.findByRole('navigation', { name: 'Account' })
    expect(within(nav).getByRole('link', { name: 'Sign in' })).toHaveAttribute('href', '/signin')
    expect(within(nav).getByRole('link', { name: 'Sign up' })).toHaveAttribute('href', '/signup')
    expect(within(nav).queryByRole('button', { name: 'Sign out' })).toBeNull()
  })

  it('shows who is signed in, with Sign out in the account menu', async () => {
    fakeApi({ 'GET /api/auth/session': SIGNED_IN })
    renderAt('/')
    const menu = await openAccountMenu()
    expect(within(menu).getByRole('menuitem', { name: 'Sign out' })).toBeInTheDocument()
  })

  it('shows the Overview placeholder for a signed-in user: the page header and Coming later', async () => {
    fakeApi({ 'GET /api/auth/session': SIGNED_IN })
    renderAt('/')
    expect(await screen.findByRole('heading', { level: 1, name: 'Overview' })).toBeInTheDocument()
    expect(screen.getByText('The dashboard comes in M6.')).toBeInTheDocument()
    expect(screen.getByText('Coming later')).toBeInTheDocument()
  })

  it('has a page for an unknown address', async () => {
    fakeApi({})
    renderAt('/nowhere')
    expect(await screen.findByRole('heading', { name: 'Page not found' })).toBeInTheDocument()
  })
})

describe('signing out', () => {
  it('posts the logout, reads the session again and goes to sign-in', async () => {
    let out = false
    const api = fakeApi({
      'GET /api/auth/session': () => (out ? { authenticated: false, csrf_token: 'a' } : SIGNED_IN),
      'POST /api/auth/logout': () => {
        out = true
        return new Response(null, { status: 204 })
      },
    })
    renderAt('/')
    fireEvent.click(within(await openAccountMenu()).getByRole('menuitem', { name: 'Sign out' }))
    expect(await screen.findByRole('heading', { name: 'Sign in' })).toBeInTheDocument()
    expect(here()).toBe('/signin')
    expect(api.to('POST', '/api/auth/logout')).toHaveLength(1)
    expect(screen.queryByText('ann@example.test')).toBeNull()
  })

  it('is possible from the enrolment page, so a user on the wrong account can leave', async () => {
    let out = false
    fakeApi({
      'GET /api/auth/session': () => (out ? { authenticated: false, csrf_token: 'a' } : SIGNED_IN_NO_TOTP),
      'POST /api/auth/logout': () => {
        out = true
        return new Response(null, { status: 204 })
      },
    })
    renderAt('/two-factor/setup')
    await screen.findByRole('heading', { name: 'Set up your authenticator app' })
    fireEvent.click(screen.getByRole('button', { name: 'Sign out' }))
    expect(await screen.findByRole('heading', { name: 'Sign in' })).toBeInTheDocument()
  })

  it('shows an error and keeps the user signed in when it fails', async () => {
    fakeApi({
      'GET /api/auth/session': SIGNED_IN,
      'POST /api/auth/logout': reason(500, 'boom'),
    })
    renderAt('/')
    fireEvent.click(within(await openAccountMenu()).getByRole('menuitem', { name: 'Sign out' }))
    expect(await screen.findByRole('alert')).toHaveTextContent(/went wrong on the server/)
    expect(here()).toBe('/')
    // Still signed in: the account menu opens again, with Sign out in it.
    expect(within(await openAccountMenu()).getByRole('menuitem', { name: 'Sign out' })).toBeEnabled()
  })
})

describe('mail-link pages', () => {
  it('are not mounted again when the session arrives, so the token is read and stripped once', async () => {
    fakeApi({ 'POST /api/auth/password/reset/info': { methods: [] } })
    renderAt('/reset#token=once')
    expect(window.location.hash).toBe('')
    expect(await screen.findByRole('button', { name: 'Change password' })).toBeInTheDocument()
    // The session read finished meanwhile; the page still has its token.
    await screen.findByRole('navigation', { name: 'Account' })
    expect(screen.getByRole('button', { name: 'Change password' })).toBeInTheDocument()
  })
})
