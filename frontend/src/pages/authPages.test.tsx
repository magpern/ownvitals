import { fireEvent, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { goToProvider } from '../lib/navigation'
import {
  SIGNED_IN,
  SIGNED_IN_NO_CODE,
  SIGNED_IN_NO_TOTP,
  fakeApi,
  json,
  reason,
} from '../test/fakeApi'
import { renderAt } from '../test/render'

vi.mock('../lib/navigation', () => ({ goToProvider: vi.fn() }))

const here = () => window.location.pathname + window.location.search

function type(label: RegExp | string, value: string) {
  fireEvent.change(screen.getByLabelText(label), { target: { value } })
}

beforeEach(() => {
  vi.mocked(goToProvider).mockReset()
})

describe('sign-in with a password', () => {
  /** Signed out until the login call, then signed in as `after`. */
  function loginRoutes(after: object) {
    let signedIn = false
    return {
      'GET /api/auth/session': () => (signedIn ? after : { authenticated: false, csrf_token: 'a' }),
      'POST /api/auth/login': () => {
        signedIn = true
        return { id: 'u1', email: 'ann@example.test', role: 'user' }
      },
    }
  }

  function signIn() {
    type('Email', 'ann@example.test')
    type('Password', 'correct horse battery')
    fireEvent.click(screen.getByRole('button', { name: 'Sign in' }))
  }

  it('posts the credentials and lands on the home page when the second factor is done', async () => {
    const api = fakeApi(loginRoutes(SIGNED_IN))
    renderAt('/signin')
    await screen.findByLabelText('Email')
    signIn()
    expect(await screen.findByText('The dashboard comes in M6.')).toBeInTheDocument()
    expect(api.to('POST', '/api/auth/login')[0]?.body).toEqual({
      email: 'ann@example.test',
      password: 'correct horse battery',
    })
    expect(here()).toBe('/')
  })

  it('goes on to the page the user came from', async () => {
    fakeApi(loginRoutes(SIGNED_IN))
    renderAt('/signin?return_to=%2Fmetrics')
    await screen.findByLabelText('Email')
    signIn()
    await waitFor(() => {
      expect(here()).toBe('/metrics')
    })
  })

  it('goes to enrolment when the account has no authenticator app yet', async () => {
    fakeApi(loginRoutes(SIGNED_IN_NO_TOTP))
    renderAt('/signin')
    await screen.findByLabelText('Email')
    signIn()
    expect(await screen.findByRole('heading', { name: 'Set up your authenticator app' })).toBeInTheDocument()
    expect(here()).toBe('/two-factor/setup')
  })

  it('goes to the code prompt when the account has an authenticator app', async () => {
    fakeApi(loginRoutes(SIGNED_IN_NO_CODE))
    renderAt('/signin')
    await screen.findByLabelText('Email')
    signIn()
    expect(await screen.findByRole('heading', { name: 'Enter your code' })).toBeInTheDocument()
    expect(here()).toBe('/two-factor')
  })

  it('says so in plain words when the credentials are wrong, and stays on the form', async () => {
    fakeApi({ 'POST /api/auth/login': reason(401, 'invalid_credentials') })
    renderAt('/signin')
    await screen.findByLabelText('Email')
    signIn()
    expect(await screen.findByRole('alert')).toHaveTextContent('The email or password is not right.')
    expect(here()).toBe('/signin')
  })

  it('links to password reset and sign-up', async () => {
    fakeApi({})
    renderAt('/signin')
    expect(await screen.findByRole('link', { name: 'Forgot your password?' })).toHaveAttribute('href', '/reset')
    expect(screen.getByRole('link', { name: 'Create an account' })).toHaveAttribute('href', '/signup')
  })

  it('explains an OAuth outcome from the callback', async () => {
    fakeApi({})
    renderAt('/signin?oauth=denied')
    expect(await screen.findByRole('alert')).toHaveTextContent('cancelled at Google')
  })

  it('does not echo an unknown OAuth outcome code', async () => {
    fakeApi({})
    renderAt('/signin?oauth=<b>x</b>')
    expect(await screen.findByRole('alert')).toHaveTextContent('could not be completed')
    expect(screen.queryByText(/<b>/)).toBeNull()
  })
})

describe('Continue with Google', () => {
  it('is offered when Google is enabled, and starts a sign-in with the invite and return address', async () => {
    const api = fakeApi({
      'GET /api/auth/oauth/providers': { providers: ['google'] },
      'POST /api/auth/oauth/google/start': { authorization_url: 'https://accounts.example.test/auth?state=x' },
    })
    renderAt('/signin?return_to=%2Fmetrics')
    const google = await screen.findByRole('button', { name: 'Continue with Google' })
    type('Invite code (only for your first sign-in with Google)', ' abc ')
    fireEvent.click(google)
    await waitFor(() => {
      expect(goToProvider).toHaveBeenCalledWith('https://accounts.example.test/auth?state=x')
    })
    expect(api.to('POST', '/api/auth/oauth/google/start')[0]?.body).toEqual({
      purpose: 'sign_in',
      invite_code: 'abc',
      return_to: '/metrics',
    })
  })

  it("follows Google's branding: the G mark from this origin and an approved label", async () => {
    fakeApi({ 'GET /api/auth/oauth/providers': { providers: ['google'] } })
    renderAt('/signin')
    const google = await screen.findByRole('button', { name: 'Continue with Google' })
    expect(google).toHaveClass('google-button')
    const mark = google.querySelector('svg[data-testid="google-logo"]')
    expect(mark).not.toBeNull()
    expect(google.querySelector('img')).toBeNull() // drawn inline: no request for the mark
    expect(google.innerHTML).not.toMatch(/https?:|\/\/|data:/)
    expect(google.textContent).toBe('Continue with Google')
  })

  it('is not offered when no provider is enabled', async () => {
    fakeApi({})
    renderAt('/signin')
    await screen.findByLabelText('Email')
    expect(screen.queryByRole('button', { name: /Continue with/ })).toBeNull()
  })

  it('shows the error when the sign-in cannot be started', async () => {
    fakeApi({
      'GET /api/auth/oauth/providers': { providers: ['google'] },
      'POST /api/auth/oauth/google/start': reason(404, 'provider_disabled'),
    })
    renderAt('/signin')
    fireEvent.click(await screen.findByRole('button', { name: 'Continue with Google' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('not available')
    expect(goToProvider).not.toHaveBeenCalled()
  })

  it('sends a return address that is not a plain path as none at all', async () => {
    const api = fakeApi({
      'GET /api/auth/oauth/providers': { providers: ['google'] },
      'POST /api/auth/oauth/google/start': { authorization_url: 'https://accounts.example.test/auth' },
    })
    renderAt('/signin?return_to=https%3A%2F%2Fevil.example%2F')
    fireEvent.click(await screen.findByRole('button', { name: 'Continue with Google' }))
    await waitFor(() => {
      expect(goToProvider).toHaveBeenCalled()
    })
    expect(api.to('POST', '/api/auth/oauth/google/start')[0]?.body).toMatchObject({ return_to: null })
  })
})

describe('sign-up and the email verification notice', () => {
  it('posts email, password and invite, then asks the user to check the inbox', async () => {
    const api = fakeApi({ 'POST /api/auth/signup': new Response(null, { status: 202 }) })
    renderAt('/signup')
    await screen.findByLabelText('Email')
    type('Invite code', 'abc')
    type('Email', 'ann@example.test')
    type('Password', 'correct horse battery')
    fireEvent.click(screen.getByRole('button', { name: 'Create account' }))
    expect(await screen.findByText('Check your inbox')).toBeInTheDocument()
    expect(screen.getByRole('status')).toHaveTextContent(/Open the link/)
    expect(api.to('POST', '/api/auth/signup')[0]?.body).toEqual({
      email: 'ann@example.test',
      password: 'correct horse battery',
      invite_code: 'abc',
    })
  })

  it('sends no invite code when the field is empty', async () => {
    const api = fakeApi({ 'POST /api/auth/signup': new Response(null, { status: 202 }) })
    renderAt('/signup')
    await screen.findByLabelText('Email')
    type('Email', 'ann@example.test')
    type('Password', 'correct horse battery')
    fireEvent.click(screen.getByRole('button', { name: 'Create account' }))
    await screen.findByText('Check your inbox')
    expect(api.to('POST', '/api/auth/signup')[0]?.body).toMatchObject({ invite_code: null })
  })

  it('shows the invite error in plain words', async () => {
    fakeApi({ 'POST /api/auth/signup': reason(400, 'invalid_invite') })
    renderAt('/signup')
    await screen.findByLabelText('Email')
    type('Email', 'ann@example.test')
    type('Password', 'correct horse battery')
    fireEvent.click(screen.getByRole('button', { name: 'Create account' }))
    expect(await screen.findByRole('alert')).toHaveTextContent(/invite code is not valid/)
  })

  it('says when sign-ups are closed', async () => {
    fakeApi({ 'POST /api/auth/signup': reason(403, 'signups_closed') })
    renderAt('/signup')
    await screen.findByLabelText('Email')
    type('Email', 'ann@example.test')
    type('Password', 'correct horse battery')
    fireEvent.click(screen.getByRole('button', { name: 'Create account' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Sign-ups are closed')
  })
})

describe('sign-up confirmation page', () => {
  it('strips the token from the address bar, posts it with the password and never signs in', async () => {
    const api = fakeApi({ 'POST /api/auth/signup/confirm': { status: 'created' } })
    renderAt('/signup/confirm#token=secret-token')
    expect(window.location.hash).toBe('')
    expect(window.location.pathname).toBe('/signup/confirm')

    type('Password', 'correct horse battery')
    fireEvent.click(screen.getByRole('button', { name: 'Confirm account' }))

    expect(await screen.findByText('Account created')).toBeInTheDocument()
    expect(api.to('POST', '/api/auth/signup/confirm')[0]?.body).toEqual({
      token: 'secret-token',
      password: 'correct horse battery',
    })
    expect(api.to('POST', '/api/auth/login')).toHaveLength(0)
  })

  it('explains a wrong password and keeps the form', async () => {
    fakeApi({ 'POST /api/auth/signup/confirm': reason(400, 'wrong_password') })
    renderAt('/signup/confirm#token=t')
    type('Password', 'nope')
    fireEvent.click(screen.getByRole('button', { name: 'Confirm account' }))
    expect(await screen.findByRole('alert')).toHaveTextContent(/not the password you chose/)
    expect(screen.getByRole('button', { name: 'Confirm account' })).toBeInTheDocument()
  })

  it('tells a Google sign-up in the wrong browser to finish where it started', async () => {
    fakeApi({ 'POST /api/auth/signup/confirm': reason(400, 'wrong_browser') })
    renderAt('/signup/confirm#token=t')
    fireEvent.click(screen.getByRole('button', { name: 'Confirm account' }))
    expect(await screen.findByRole('alert')).toHaveTextContent(/device and browser where you started/)
  })

  it('asks for the mail link when there is no token', () => {
    fakeApi({})
    renderAt('/signup/confirm')
    expect(screen.getByRole('alert')).toHaveTextContent(/needs the link/)
  })
})

describe('provider email page', () => {
  it('sends only the address, then tells a mistyper to start the Google sign-in again', async () => {
    const api = fakeApi({
      'GET /api/auth/oauth/pending': { pending: true, provider: 'google', awaiting_email: true },
      'POST /api/auth/oauth/pending/email': new Response(JSON.stringify({}), { status: 202 }),
    })
    renderAt('/signin/email')
    await screen.findByLabelText('Email')
    type('Email', 'ann@example.test')
    fireEvent.click(await screen.findByRole('button', { name: 'Send confirmation link' }))

    expect(await screen.findByText(/a second try would not send anything/)).toBeInTheDocument()
    expect(screen.getByRole('link', { name: /Start the Google sign-in again/ })).toHaveAttribute('href', '/signin')
    expect(screen.queryByRole('button', { name: 'Send confirmation link' })).toBeNull()
    expect(api.to('POST', '/api/auth/oauth/pending/email')[0]?.body).toEqual({ email: 'ann@example.test' })
  })

  it('says so when no sign-in is waiting', async () => {
    fakeApi({ 'GET /api/auth/oauth/pending': { pending: false, provider: null, awaiting_email: false } })
    renderAt('/signin/email')
    expect(await screen.findByText('Nothing to finish here')).toBeInTheDocument()
  })

  it('is where the callback sends ?oauth=email_needed', async () => {
    fakeApi({ 'GET /api/auth/oauth/pending': { pending: true, provider: 'google', awaiting_email: true } })
    renderAt('/signin?oauth=email_needed')
    expect(await screen.findByText('Confirm your email address')).toBeInTheDocument()
    expect(window.location.pathname).toBe('/signin/email')
  })
})

describe('password reset', () => {
  it('requests a link for an address', async () => {
    const api = fakeApi({ 'POST /api/auth/password/reset/request': new Response(null, { status: 202 }) })
    renderAt('/reset')
    type('Email', 'ann@example.test')
    fireEvent.click(screen.getByRole('button', { name: 'Send reset link' }))
    expect(await screen.findByText('Check your inbox')).toBeInTheDocument()
    expect(api.to('POST', '/api/auth/password/reset/request')[0]?.body).toEqual({ email: 'ann@example.test' })
  })

  it('lists linked methods and removes the ticked ones in the same step', async () => {
    const api = fakeApi({
      'POST /api/auth/password/reset/info': { methods: [{ id: 'm1', provider: 'google' }] },
      'POST /api/auth/password/reset/complete': { status: 'ok' },
    })
    renderAt('/reset#token=reset-token')
    expect(window.location.hash).toBe('')
    fireEvent.click(await screen.findByLabelText('Remove Google'))
    type('New password', 'a brand new passphrase')
    fireEvent.click(screen.getByRole('button', { name: 'Change password' }))

    expect(await screen.findByText('Password changed')).toBeInTheDocument()
    expect(api.to('POST', '/api/auth/password/reset/info')[0]?.body).toEqual({ token: 'reset-token' })
    expect(api.to('POST', '/api/auth/password/reset/complete')[0]?.body).toEqual({
      token: 'reset-token',
      password: 'a brand new passphrase',
      remove_methods: ['m1'],
    })
    expect(api.to('POST', '/api/auth/login')).toHaveLength(0)
  })

  it('reads the session again after a completed reset, so the header updates', async () => {
    let reset = false
    const api = fakeApi({
      'GET /api/auth/session': () => (reset ? { authenticated: false, csrf_token: 'a' } : SIGNED_IN),
      'POST /api/auth/password/reset/info': { methods: [] },
      'POST /api/auth/password/reset/complete': () => {
        reset = true
        return { status: 'ok' }
      },
    })
    renderAt('/reset#token=reset-token')
    expect(await screen.findByRole('button', { name: 'Sign out' })).toBeInTheDocument()
    type('New password', 'a brand new passphrase')
    fireEvent.click(screen.getByRole('button', { name: 'Change password' }))
    expect(await screen.findByText('Password changed')).toBeInTheDocument()
    await waitFor(() => {
      expect(screen.queryByRole('button', { name: 'Sign out' })).toBeNull()
    })
    expect(api.to('GET', '/api/auth/session').length).toBeGreaterThanOrEqual(2)
  })

  it('reports a dead link', async () => {
    fakeApi({ 'POST /api/auth/password/reset/info': reason(400, 'invalid_token') })
    renderAt('/reset#token=old')
    expect(await screen.findByRole('alert')).toHaveTextContent(/not valid any more/)
  })

  it('reads and strips a token that arrives as a fragment-only change in an open tab', async () => {
    fakeApi({ 'POST /api/auth/password/reset/info': { methods: [] } })
    renderAt('/reset')
    expect(screen.getByRole('button', { name: 'Send reset link' })).toBeInTheDocument()

    window.history.pushState(null, '', '/reset#token=later')
    window.dispatchEvent(new HashChangeEvent('hashchange'))

    expect(await screen.findByRole('button', { name: 'Change password' })).toBeInTheDocument()
    expect(window.location.hash).toBe('')
  })

  it('shows a server error in plain words', async () => {
    fakeApi({ 'POST /api/auth/password/reset/request': json({}, 500) })
    renderAt('/reset')
    type('Email', 'ann@example.test')
    fireEvent.click(screen.getByRole('button', { name: 'Send reset link' }))
    expect(await screen.findByRole('alert')).toHaveTextContent(/went wrong on the server/)
  })
})
