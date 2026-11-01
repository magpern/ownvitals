import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { SIGNED_IN, fakeApi, reason } from '../test/fakeApi'
import { renderAt } from '../test/render'
import { deviceName, lastActive, signedInAt } from './SecurityPage'

// "Sign-in and security" (#11): the lock band, "Where you're signed in" and the shared "Confirm
// it's you" dialog that every step-up action runs through. Everything is synthetic.

const here = () => window.location.pathname

const FIREFOX = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:140.0) Gecko/20100101 Firefox/140.0'
const IPHONE =
  'Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1'
const SESSIONS = [
  { id: 's1', created_at: '2030-01-01T07:30:00Z', last_seen_at: '2030-01-01T07:30:00Z', expires_at: '2030-01-31T00:00:00Z', user_agent: FIREFOX, ip: '203.0.113.24', current: true },
  { id: 's2', created_at: '2029-12-27T07:30:00Z', last_seen_at: '2029-12-31T07:30:00Z', expires_at: '2030-01-26T00:00:00Z', user_agent: IPHONE, ip: '198.51.100.7', current: false },
]
const SIGNED_OUT = { authenticated: false, csrf_token: 'a' }

function typeCode(code: string) {
  const dialog = screen.getByRole('dialog', { name: "Confirm it's you" })
  const boxes = within(within(dialog).getByRole('group', { name: '6-digit code' })).getAllByRole('textbox')
  fireEvent.change(boxes[0] as HTMLElement, { target: { value: code } })
}

describe('the security tab', () => {
  it('shows the lock band, the sessions with this device marked, and the session limits', async () => {
    fakeApi({ 'GET /api/auth/session': SIGNED_IN, 'GET /api/auth/sessions': SESSIONS })
    renderAt('/settings/security')
    expect(await screen.findByText(/One code covers 10 minutes of protected actions/)).toBeInTheDocument()
    const table = await screen.findByRole('table')
    const rows = within(table).getAllByRole('row').slice(1)
    expect(within(rows[0] as HTMLElement).getByText('Firefox on Windows')).toBeInTheDocument()
    expect(within(rows[0] as HTMLElement).getByText('This device')).toBeInTheDocument()
    expect(within(rows[0] as HTMLElement).queryByRole('button')).toBeNull()
    expect(within(rows[1] as HTMLElement).getByText('Safari on iPhone')).toBeInTheDocument()
    expect(within(rows[1] as HTMLElement).getByText('198.51.100.7')).toBeInTheDocument()
    expect(screen.getByText('A session ends after 72 hours without activity, and after 30 days at most.')).toBeInTheDocument()
    const everywhere = screen.getByRole('button', { name: 'Sign out everywhere…' })
    expect(everywhere).toHaveAttribute('data-lock')
    expect(everywhere).toHaveAttribute('aria-haspopup', 'dialog')
  })

  it('signs out another session without asking for a code', async () => {
    let rows = SESSIONS
    const api = fakeApi({
      'GET /api/auth/session': SIGNED_IN,
      'GET /api/auth/sessions': () => rows,
      'DELETE /api/auth/sessions/s2': () => {
        rows = SESSIONS.slice(0, 1)
        return new Response(null, { status: 204 })
      },
    })
    renderAt('/settings/security')
    fireEvent.click(await screen.findByRole('button', { name: 'Sign out Safari on iPhone' }))
    await waitFor(() => {
      expect(screen.queryByText('Safari on iPhone')).toBeNull()
    })
    expect(api.to('DELETE', '/api/auth/sessions/s2')).toHaveLength(1)
    expect(screen.queryByRole('dialog')).toBeNull()
  })
})

describe('the step-up dialog', () => {
  /** Sign out everywhere, refused until a step-up (and, with `reauth`, a password) is given. */
  function protectedRoutes({ reauth = false } = {}) {
    let password = !reauth
    let code = false
    let out = false
    return {
      'GET /api/auth/session': () => (out ? SIGNED_OUT : SIGNED_IN),
      'GET /api/auth/sessions': SESSIONS,
      'GET /api/auth/identities': { identities: [{ id: 'i1', provider: 'google' }], has_password: true, available: ['google'] },
      'POST /api/auth/logout-all': () => {
        if (!password) return reason(403, 'reauth_required')
        if (!code) return reason(403, 'step_up_required')
        out = true
        return new Response(null, { status: 204 })
      },
      'POST /api/auth/reauth': () => {
        password = true
        return { status: 'ok' }
      },
      'POST /api/auth/mfa/stepup': () => {
        code = true
        return { ok: true }
      },
    }
  }

  it('asks for a fresh code when the server wants a step-up, then does the action', async () => {
    const api = fakeApi(protectedRoutes())
    renderAt('/settings/security')
    fireEvent.click(await screen.findByRole('button', { name: 'Sign out everywhere…' }))
    const dialog = await screen.findByRole('dialog', { name: "Confirm it's you" })
    expect(dialog).toHaveAttribute('aria-modal', 'true')
    expect(within(dialog).getByText(/Signing out everywhere needs a fresh code from your authenticator app/)).toBeInTheDocument()
    expect(within(dialog).getByRole('button', { name: 'Confirm' })).toBeDisabled()
    typeCode('123456')
    fireEvent.click(within(dialog).getByRole('button', { name: 'Confirm' }))
    expect(await screen.findByRole('heading', { name: 'Sign in' })).toBeInTheDocument()
    expect(here()).toBe('/signin')
    expect(api.to('POST', '/api/auth/mfa/stepup')[0]?.body).toEqual({ code: '123456' })
    expect(api.to('POST', '/api/auth/logout-all')).toHaveLength(2)
  })

  it('takes a recovery code instead', async () => {
    const api = fakeApi(protectedRoutes())
    renderAt('/settings/security')
    fireEvent.click(await screen.findByRole('button', { name: 'Sign out everywhere…' }))
    const dialog = await screen.findByRole('dialog', { name: "Confirm it's you" })
    fireEvent.click(within(dialog).getByRole('button', { name: 'Use a recovery code instead' }))
    fireEvent.change(within(dialog).getByLabelText('Recovery code'), { target: { value: 'AAAA-BBBB' } })
    fireEvent.click(within(dialog).getByRole('button', { name: 'Confirm' }))
    await waitFor(() => {
      expect(api.to('POST', '/api/auth/mfa/stepup')[0]?.body).toEqual({ code: 'AAAA-BBBB' })
    })
  })

  it('keeps the dialog up with the reason when the code is wrong', async () => {
    fakeApi({ ...protectedRoutes(), 'POST /api/auth/mfa/stepup': reason(400, 'invalid_code') })
    renderAt('/settings/security')
    fireEvent.click(await screen.findByRole('button', { name: 'Sign out everywhere…' }))
    await screen.findByRole('dialog', { name: "Confirm it's you" })
    typeCode('000000')
    fireEvent.click(screen.getByRole('button', { name: 'Confirm' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('That code is not right')
    expect(screen.getByRole('dialog', { name: "Confirm it's you" })).toBeInTheDocument()
  })

  it('does nothing and shows no error when cancelled', async () => {
    const api = fakeApi(protectedRoutes())
    renderAt('/settings/security')
    fireEvent.click(await screen.findByRole('button', { name: 'Sign out everywhere…' }))
    const dialog = await screen.findByRole('dialog', { name: "Confirm it's you" })
    fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }))
    await waitFor(() => {
      expect(screen.queryByRole('dialog')).toBeNull()
    })
    expect(screen.queryByRole('alert')).toBeNull()
    expect(api.to('POST', '/api/auth/logout-all')).toHaveLength(1)
    expect(here()).toBe('/settings/security')
  })

  it('closes on Escape', async () => {
    fakeApi(protectedRoutes())
    renderAt('/settings/security')
    fireEvent.click(await screen.findByRole('button', { name: 'Sign out everywhere…' }))
    const dialog = await screen.findByRole('dialog', { name: "Confirm it's you" })
    fireEvent.keyDown(dialog, { key: 'Escape' })
    await waitFor(() => {
      expect(screen.queryByRole('dialog')).toBeNull()
    })
  })

  it('asks for the password first when the server wants re-authentication, then the code, in one dialog', async () => {
    const api = fakeApi(protectedRoutes({ reauth: true }))
    renderAt('/settings/security')
    fireEvent.click(await screen.findByRole('button', { name: 'Sign out everywhere…' }))
    const dialog = await screen.findByRole('dialog', { name: "Confirm it's you" })
    expect(within(dialog).getByText(/needs your password, then a fresh code/)).toBeInTheDocument()
    expect(await within(dialog).findByRole('button', { name: 'Use Google instead' })).toBeInTheDocument()
    fireEvent.change(within(dialog).getByLabelText('Password'), { target: { value: 'correct horse battery' } })
    fireEvent.click(within(dialog).getByRole('button', { name: 'Confirm' }))
    expect(await screen.findByText(/needs a fresh code from your authenticator app/)).toBeInTheDocument()
    expect(api.to('POST', '/api/auth/reauth')[0]?.body).toEqual({ password: 'correct horse battery' })
    typeCode('123456')
    fireEvent.click(screen.getByRole('button', { name: 'Confirm' }))
    expect(await screen.findByRole('heading', { name: 'Sign in' })).toBeInTheDocument()
    expect(api.to('POST', '/api/auth/logout-all')).toHaveLength(3)
  })

  it('shows any other refusal as an error on the page, without the dialog', async () => {
    fakeApi({ ...protectedRoutes(), 'POST /api/auth/logout-all': reason(500, 'boom') })
    renderAt('/settings/security')
    fireEvent.click(await screen.findByRole('button', { name: 'Sign out everywhere…' }))
    expect(await screen.findByRole('alert')).toHaveTextContent(/went wrong on the server/)
    expect(screen.queryByRole('dialog')).toBeNull()
  })
})

describe('the session rows', () => {
  it('names the browser and the system', () => {
    expect(deviceName(FIREFOX)).toBe('Firefox on Windows')
    expect(deviceName(IPHONE)).toBe('Safari on iPhone')
    expect(deviceName(null)).toBe('Unknown device')
  })

  it('says when, in words', () => {
    const now = new Date('2030-01-01T12:00:00Z')
    expect(lastActive('2030-01-01T11:59:30Z', now)).toBe('Now')
    expect(lastActive('2030-01-01T11:30:00Z', now)).toBe('30 minutes ago')
    expect(lastActive('2030-01-01T10:00:00Z', now)).toBe('2 hours ago')
    expect(lastActive('2029-12-29T12:00:00Z', now)).toBe('3 days ago')
    expect(signedInAt('2029-12-27T07:30:00Z', now)).toBe('27 Dec')
  })
})
