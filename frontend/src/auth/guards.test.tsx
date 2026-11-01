import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import type { ReactNode } from 'react'
import { describe, expect, it, vi } from 'vitest'
import { post } from '../api/client'
import { RouterProvider, useRouter } from '../router/Router'
import { SIGNED_IN, SIGNED_IN_NO_CODE, SIGNED_IN_NO_TOTP, fakeApi, reason } from '../test/fakeApi'
import { renderAt } from '../test/render'
import { RequireReady, RequireSignedOut } from './guards'
import { SessionProvider } from './SessionContext'

const here = () => window.location.pathname + window.location.search

/** Renders its children on one path only, as the app's route table does. */
function At({ path, children }: { path: string; children: ReactNode }) {
  const { pathname } = useRouter()
  return pathname === path ? children : null
}

/** A guard around a marker, mounted at `url`, without the rest of the app. */
function guarded(url: string, guard: 'ready' | 'signed-out') {
  window.history.replaceState(null, '', url)
  const Guard = guard === 'ready' ? RequireReady : RequireSignedOut
  return render(
    <RouterProvider>
      <SessionProvider>
        <At path={new URL(url, 'http://localhost').pathname}>
          <Guard>
            <p>inside the guard</p>
          </Guard>
        </At>
      </SessionProvider>
    </RouterProvider>,
  )
}

describe('signed-out visitors', () => {
  it('go to sign-in from the home page', async () => {
    fakeApi({})
    renderAt('/')
    expect(await screen.findByRole('heading', { name: 'Sign in' })).toBeInTheDocument()
    expect(here()).toBe('/signin')
    expect(screen.queryByText('The dashboard comes in M6.')).toBeNull()
  })

  it('go to sign-in, with the page to come back to, from any guarded page', async () => {
    fakeApi({})
    guarded('/metrics?range=30d', 'ready')
    await waitFor(() => {
      expect(here()).toBe('/signin?return_to=%2Fmetrics%3Frange%3D30d')
    })
    expect(screen.queryByText('inside the guard')).toBeNull()
  })

  it.each(['/two-factor/setup', '/two-factor'])('go to sign-in from %s', async (path) => {
    fakeApi({})
    renderAt(path)
    await waitFor(() => {
      expect(here()).toBe('/signin')
    })
  })

  it('may open sign-up', async () => {
    fakeApi({})
    renderAt('/signup')
    expect(await screen.findByRole('heading', { name: 'Create an account' })).toBeInTheDocument()
    expect(here()).toBe('/signup')
  })
})

describe('a session without TOTP', () => {
  const routes = { 'GET /api/auth/session': SIGNED_IN_NO_TOTP }

  it('goes to enrolment from the home page', async () => {
    fakeApi(routes)
    renderAt('/')
    expect(await screen.findByRole('heading', { name: 'Set up your authenticator app' })).toBeInTheDocument()
    expect(here()).toBe('/two-factor/setup')
    expect(screen.queryByText('The dashboard comes in M6.')).toBeNull()
  })

  it('goes to enrolment from a guarded page, even when the session says it passed the code', async () => {
    fakeApi({ 'GET /api/auth/session': { ...SIGNED_IN, session: { ...SIGNED_IN.session, totp_enabled: false } } })
    guarded('/metrics', 'ready')
    await waitFor(() => {
      expect(here()).toBe('/two-factor/setup?return_to=%2Fmetrics')
    })
    expect(screen.queryByText('inside the guard')).toBeNull()
  })

  it('is kept off sign-in and sent to enrolment, with the return_to kept', async () => {
    fakeApi(routes)
    renderAt('/signin?return_to=%2Fmetrics')
    await waitFor(() => {
      expect(here()).toBe('/two-factor/setup?return_to=%2Fmetrics')
    })
  })

  it('is kept off the code prompt', async () => {
    fakeApi(routes)
    renderAt('/two-factor')
    await waitFor(() => {
      expect(here()).toBe('/two-factor/setup')
    })
  })
})

describe('a session that has not passed its code', () => {
  const routes = { 'GET /api/auth/session': SIGNED_IN_NO_CODE }

  it('goes to the code prompt from the home page', async () => {
    fakeApi(routes)
    renderAt('/')
    expect(await screen.findByRole('heading', { name: 'Enter your code' })).toBeInTheDocument()
    expect(here()).toBe('/two-factor')
  })

  it('is sent from enrolment to the code prompt', async () => {
    fakeApi(routes)
    renderAt('/two-factor/setup')
    expect(await screen.findByRole('heading', { name: 'Enter your code' })).toBeInTheDocument()
    expect(here()).toBe('/two-factor')
  })
})

describe('a session through its second factor', () => {
  it('sees the guarded page', async () => {
    fakeApi({ 'GET /api/auth/session': SIGNED_IN })
    renderAt('/')
    expect(await screen.findByText('The dashboard comes in M6.')).toBeInTheDocument()
    expect(here()).toBe('/')
  })

  it.each(['/signin', '/signup', '/two-factor', '/two-factor/setup'])(
    'is sent from %s to the home page',
    async (path) => {
      fakeApi({ 'GET /api/auth/session': SIGNED_IN })
      renderAt(path)
      await waitFor(() => {
        expect(here()).toBe('/')
      })
    },
  )

  it('is sent on to a safe return_to', async () => {
    fakeApi({ 'GET /api/auth/session': SIGNED_IN })
    renderAt('/signin?return_to=%2Fmetrics')
    await waitFor(() => {
      expect(here()).toBe('/metrics')
    })
  })

  it('is never sent to another host by return_to', async () => {
    fakeApi({ 'GET /api/auth/session': SIGNED_IN })
    renderAt('/signin?return_to=https%3A%2F%2Fevil.example%2F')
    await waitFor(() => {
      expect(here()).toBe('/')
    })
  })

  it('does not see a signed-out-only page', async () => {
    fakeApi({ 'GET /api/auth/session': SIGNED_IN })
    guarded('/anywhere', 'signed-out')
    await waitFor(() => {
      expect(here()).toBe('/')
    })
    expect(screen.queryByText('inside the guard')).toBeNull()
  })
})

describe('while the session is unknown', () => {
  it('shows nothing guarded before the session has loaded', () => {
    vi.stubGlobal('fetch', vi.fn(() => new Promise<Response>(() => undefined)))
    guarded('/', 'ready')
    expect(screen.getByText('One moment')).toBeInTheDocument()
    expect(screen.queryByText('inside the guard')).toBeNull()
  })

  it('shows an error with a retry when the session cannot be read, and recovers', async () => {
    let up = false
    fakeApi({ 'GET /api/auth/session': () => (up ? SIGNED_IN : reason(500, 'boom')) })
    guarded('/', 'ready')
    expect(await screen.findByText('Cannot reach the server')).toBeInTheDocument()
    expect(screen.queryByText('inside the guard')).toBeNull()
    up = true
    fireEvent.click(screen.getByRole('button', { name: 'Try again' }))
    expect(await screen.findByText('inside the guard')).toBeInTheDocument()
  })
})

describe('an API refusal with two_factor_required', () => {
  it('reads the session again and sends the user to the page of their step', async () => {
    let lapsed = false
    const api = fakeApi({
      'GET /api/auth/session': () => (lapsed ? SIGNED_IN_NO_CODE : SIGNED_IN),
      'POST /api/x': () => {
        lapsed = true
        return reason(403, 'two_factor_required')
      },
    })
    renderAt('/')
    expect(await screen.findByText('The dashboard comes in M6.')).toBeInTheDocument()
    const reads = api.to('GET', '/api/auth/session').length

    await expect(post('/api/x')).rejects.toMatchObject({ status: 403, reason: 'two_factor_required' })

    expect(await screen.findByRole('heading', { name: 'Enter your code' })).toBeInTheDocument()
    expect(here()).toBe('/two-factor')
    expect(api.to('GET', '/api/auth/session').length).toBeGreaterThan(reads)
  })
})
