import { act, fireEvent, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { installEventSource } from '../test/eventSourceFake'
import { ANONYMOUS, QR_SVG, SIGNED_IN, SIGNED_IN_NO_CODE, fakeApi, reason } from '../test/fakeApi'
import { renderAt } from '../test/render'
import { SIGNIN_OPTIONS, installWebAuthn, uninstallWebAuthn } from '../test/webauthnFake'

// The sign-in page's passkey button and the desktop's "Sign in with your phone" panel (ADR 0011
// sections 1 and 5): the QR, the stream's states, "Signed in as …" before anything else, and the
// expiry of an unscanned code. A faked server and stream; synthetic throughout.

afterEach(() => {
  uninstallWebAuthn()
  vi.useRealTimers()
})

function qrRoutes(signedIn: () => boolean) {
  let starts = 0
  return {
    'GET /api/auth/session': () => (signedIn() ? SIGNED_IN : ANONYMOUS),
    'POST /api/auth/qr/start': () => {
      starts += 1
      return { id: `row-${String(starts)}`, qr_svg: QR_SVG, qr_url: 'http://localhost/qr#made-up', expires_in: 60 }
    },
  }
}

async function openPanel() {
  fireEvent.click(await screen.findByRole('button', { name: 'Sign in with your phone' }))
  expect(await screen.findByRole('img', { name: 'QR code to sign in with your phone' })).toBeInTheDocument()
}

describe('sign in with your phone', () => {
  it('shows the QR, follows the stream and shows the account before anything else', async () => {
    let signedIn = false
    const Source = installEventSource()
    const api = fakeApi({
      ...qrRoutes(() => signedIn),
      'POST /api/auth/qr/row-1/complete': () => {
        signedIn = true
        return { id: 'u1', email: 'ann@example.test', role: 'user' }
      },
    })
    renderAt('/signin')
    await openPanel()
    expect(screen.getByText('The code works for 60 seconds.')).toBeInTheDocument()
    expect(Source.latest().url).toBe('/api/auth/qr/row-1/events')

    act(() => {
      Source.latest().emit('scanned')
    })
    expect(await screen.findByText('Scanned. Approve the sign-in on your phone.')).toBeInTheDocument()
    expect(screen.queryByRole('img', { name: 'QR code to sign in with your phone' })).toBeNull()

    act(() => {
      Source.latest().emit('approved')
    })
    expect(await screen.findByText('Signed in as ann@example.test')).toBeInTheDocument()
    expect(api.to('POST', '/api/auth/qr/row-1/complete')).toHaveLength(1)
    // Still on the sign-in page: the session is read again only on "Continue".
    expect(window.location.pathname).toBe('/signin')
    fireEvent.click(screen.getByRole('button', { name: 'Continue' }))
    await waitFor(() => {
      expect(window.location.pathname).toBe('/overview')
    })
  })

  it('says when the phone denied it, and shows a new code on request', async () => {
    const Source = installEventSource()
    const api = fakeApi(qrRoutes(() => false))
    renderAt('/signin')
    await openPanel()
    act(() => {
      Source.latest().emit('scanned')
      Source.latest().emit('denied')
    })
    expect(await screen.findByText('The sign-in was denied on the phone.')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Show a new code' }))
    expect(await screen.findByRole('img', { name: 'QR code to sign in with your phone' })).toBeInTheDocument()
    expect(api.to('POST', '/api/auth/qr/start')).toHaveLength(2)
    expect(Source.latest().url).toBe('/api/auth/qr/row-2/events')
  })

  it('expires an unscanned code after expires_in on this page too, and closes the stream', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    const Source = installEventSource()
    fakeApi(qrRoutes(() => false))
    renderAt('/signin')
    await openPanel()
    act(() => {
      vi.advanceTimersByTime(59_000)
    })
    expect(screen.queryByText('This code has expired.')).toBeNull()
    act(() => {
      vi.advanceTimersByTime(1_000)
    })
    expect(await screen.findByText('This code has expired.')).toBeInTheDocument()
    expect(Source.latest().readyState).toBe(Source.CLOSED)
  })

  it('does not expire a scanned code on the page clock', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    const Source = installEventSource()
    fakeApi(qrRoutes(() => false))
    renderAt('/signin')
    await openPanel()
    act(() => {
      Source.latest().emit('scanned')
      vi.advanceTimersByTime(90_000)
    })
    expect(screen.queryByText('This code has expired.')).toBeNull()
    act(() => {
      Source.latest().emit('expired')
    })
    expect(await screen.findByText('This code has expired.')).toBeInTheDocument()
  })

  it('reports a lost stream and a refused completion', async () => {
    const Source = installEventSource()
    fakeApi({ ...qrRoutes(() => false), 'POST /api/auth/qr/row-2/complete': reason(404, 'qr_unavailable') })
    renderAt('/signin')
    await openPanel()
    act(() => {
      Source.latest().fail()
    })
    expect(await screen.findByRole('alert')).toHaveTextContent('The connection was lost.')
    fireEvent.click(screen.getByRole('button', { name: 'Show a new code' }))
    await screen.findByRole('img', { name: 'QR code to sign in with your phone' })
    act(() => {
      Source.latest().emit('approved')
    })
    expect(await screen.findByRole('alert')).toHaveTextContent('This code has expired or was already used.')
  })

  it('goes back to the password form', async () => {
    installEventSource()
    fakeApi(qrRoutes(() => false))
    renderAt('/signin')
    await openPanel()
    fireEvent.click(screen.getByRole('button', { name: 'Use your password instead' }))
    expect(await screen.findByLabelText('Password')).toBeInTheDocument()
  })
})

describe('sign in with a passkey', () => {
  it('signs in and goes on to the code prompt', async () => {
    let signedIn = false
    const { get } = installWebAuthn()
    const api = fakeApi({
      'GET /api/auth/session': () => (signedIn ? SIGNED_IN_NO_CODE : ANONYMOUS),
      'POST /api/auth/passkeys/signin/options': SIGNIN_OPTIONS,
      'POST /api/auth/passkeys/signin': () => {
        signedIn = true
        return { id: 'u1', email: 'ann@example.test', role: 'user' }
      },
    })
    renderAt('/signin')
    expect(await screen.findByText('On another device? Choose "Use a phone" in the passkey window.')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Sign in with a passkey' }))
    await waitFor(() => {
      expect(window.location.pathname).toBe('/two-factor')
    })
    expect(get).toHaveBeenCalledTimes(1)
    expect(api.to('POST', '/api/auth/passkeys/signin')).toHaveLength(1)
  })

  it('says so when the passkey window is closed or the server refuses', async () => {
    installWebAuthn({ get: () => Promise.reject(new DOMException('closed', 'NotAllowedError')) })
    fakeApi({ 'POST /api/auth/passkeys/signin/options': SIGNIN_OPTIONS })
    renderAt('/signin')
    fireEvent.click(await screen.findByRole('button', { name: 'Sign in with a passkey' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('The passkey window was closed')

    installWebAuthn()
    fakeApi({
      'POST /api/auth/passkeys/signin/options': SIGNIN_OPTIONS,
      'POST /api/auth/passkeys/signin': reason(401, 'passkey_signin_failed'),
    })
    fireEvent.click(screen.getByRole('button', { name: 'Sign in with a passkey' }))
    await waitFor(() => {
      expect(screen.getByRole('alert')).toHaveTextContent('The passkey was not accepted.')
    })
  })

  it('has no passkey button in a browser without WebAuthn', async () => {
    fakeApi({})
    renderAt('/signin')
    expect(await screen.findByRole('button', { name: 'Sign in with your phone' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Sign in with a passkey' })).toBeNull()
  })
})
