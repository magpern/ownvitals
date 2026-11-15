import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { goToProvider } from '../../lib/navigation'
import { SIGNED_IN, fakeApi, reason, type Handler } from '../../test/fakeApi'
import { renderAt } from '../../test/render'

// The Google row of "How you sign in" (issue 516): link Google from Security, unlink it, never
// the last sign-in method, and the callback's outcome on return. Everything is synthetic: made-up
// addresses on example.test, a fake authorization URL, no network.

vi.mock('../../lib/navigation', () => ({ goToProvider: vi.fn() }))

afterEach(() => {
  vi.mocked(goToProvider).mockReset()
})

const LINKED = { id: 'i1', provider: 'google', email_at_link: 'ann.synthetic@example.test', created_at: '2030-01-01T00:00:00Z' }
const AUTH_URL = 'https://accounts.example.test/o/oauth2/auth?state=synthetic'

function identities(rows: object[], hasPassword = true) {
  return { identities: rows, has_password: hasPassword, available: ['google'] }
}

/** Routes where `action` is refused with `step_up_required` until a fresh code is given. */
function steppedUp(routes: Record<string, Handler | object>, action: string, answer: Handler) {
  let code = false
  return {
    'GET /api/auth/session': SIGNED_IN,
    'GET /api/auth/sessions': [],
    'POST /api/auth/mfa/stepup': () => {
      code = true
      return { ok: true }
    },
    ...routes,
    [action]: (body: unknown, headers: Headers) => (code ? answer(body, headers) : reason(403, 'step_up_required')),
  }
}

function confirmWithCode(code: string) {
  const dialog = screen.getByRole('dialog', { name: "Confirm it's you" })
  const boxes = within(within(dialog).getByRole('group', { name: '6-digit code' })).getAllByRole('textbox')
  fireEvent.change(boxes[0] as HTMLElement, { target: { value: code } })
  fireEvent.click(within(dialog).getByRole('button', { name: 'Confirm' }))
}

describe('the Google row', () => {
  it('is not shown while Google sign-in is off on the server', async () => {
    fakeApi({ 'GET /api/auth/session': SIGNED_IN, 'GET /api/auth/sessions': [] })
    renderAt('/settings/security')
    await screen.findByRole('heading', { name: 'How you sign in' })
    await waitFor(() => {
      expect(screen.queryByText('Google', { selector: '.ov-setting__label' })).toBeNull()
    })
    expect(screen.queryByRole('button', { name: 'Link…' })).toBeNull()
  })

  it('offers "Link…" behind the lock while Google is not linked', async () => {
    fakeApi({ 'GET /api/auth/session': SIGNED_IN, 'GET /api/auth/sessions': [], 'GET /api/auth/identities': identities([]) })
    renderAt('/settings/security')
    const link = await screen.findByRole('button', { name: 'Link…' })
    expect(link).toHaveAttribute('data-lock')
    expect(link).toHaveAttribute('aria-haspopup', 'dialog')
    expect(screen.getByText(/^Not linked\./)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Unlink…' })).toBeNull()
  })

  it('shows the linked address and "Unlink…" behind the lock', async () => {
    fakeApi({ 'GET /api/auth/session': SIGNED_IN, 'GET /api/auth/sessions': [], 'GET /api/auth/identities': identities([LINKED]) })
    renderAt('/settings/security')
    expect(await screen.findByText('Linked to ann.synthetic@example.test')).toBeInTheDocument()
    const unlink = screen.getByRole('button', { name: 'Unlink…' })
    expect(unlink).toHaveAttribute('data-lock')
    expect(screen.queryByRole('button', { name: 'Link…' })).toBeNull()
  })

  it('steps up before the link starts, then sends the browser to Google', async () => {
    const api = fakeApi(
      steppedUp({ 'GET /api/auth/identities': identities([]) }, 'POST /api/auth/oauth/google/start', () => ({
        authorization_url: AUTH_URL,
      })),
    )
    renderAt('/settings/security')
    fireEvent.click(await screen.findByRole('button', { name: 'Link…' }))
    const dialog = await screen.findByRole('dialog', { name: "Confirm it's you" })
    expect(within(dialog).getByText(/Linking Google needs a fresh code/)).toBeInTheDocument()
    expect(goToProvider).not.toHaveBeenCalled()
    confirmWithCode('123456')
    await waitFor(() => {
      expect(goToProvider).toHaveBeenCalledWith(AUTH_URL)
    })
    const starts = api.to('POST', '/api/auth/oauth/google/start')
    expect(starts).toHaveLength(2) // refused for the step-up, then started
    expect(starts[1]?.body).toEqual({ purpose: 'link', invite_code: null, return_to: null })
    expect(api.to('POST', '/api/auth/mfa/stepup')[0]?.body).toEqual({ code: '123456' })
  })

  it('does not start the link when the step-up is cancelled', async () => {
    const api = fakeApi(
      steppedUp({ 'GET /api/auth/identities': identities([]) }, 'POST /api/auth/oauth/google/start', () => ({
        authorization_url: AUTH_URL,
      })),
    )
    renderAt('/settings/security')
    fireEvent.click(await screen.findByRole('button', { name: 'Link…' }))
    const dialog = await screen.findByRole('dialog', { name: "Confirm it's you" })
    fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }))
    await waitFor(() => {
      expect(screen.queryByRole('dialog')).toBeNull()
    })
    expect(goToProvider).not.toHaveBeenCalled()
    expect(api.to('POST', '/api/auth/oauth/google/start')).toHaveLength(1)
    expect(screen.queryByRole('alert')).toBeNull()
  })

  it('steps up before unlinking, then shows Google as not linked', async () => {
    let rows: object[] = [LINKED]
    const api = fakeApi(
      steppedUp({ 'GET /api/auth/identities': () => identities(rows) }, 'DELETE /api/auth/identities/i1', () => {
        rows = []
        return new Response(null, { status: 204 })
      }),
    )
    renderAt('/settings/security')
    fireEvent.click(await screen.findByRole('button', { name: 'Unlink…' }))
    const dialog = await screen.findByRole('dialog', { name: "Confirm it's you" })
    expect(within(dialog).getByText(/Unlinking Google needs a fresh code/)).toBeInTheDocument()
    expect(api.to('DELETE', '/api/auth/identities/i1')).toHaveLength(1)
    confirmWithCode('123456')
    expect(await screen.findByRole('button', { name: 'Link…' })).toBeInTheDocument()
    expect(api.to('DELETE', '/api/auth/identities/i1')).toHaveLength(2)
    expect(screen.queryByText('Linked to ann.synthetic@example.test')).toBeNull()
  })

  it('offers no unlink for the last sign-in method and says why', async () => {
    fakeApi({
      'GET /api/auth/session': SIGNED_IN,
      'GET /api/auth/sessions': [],
      'GET /api/auth/identities': identities([LINKED], false),
    })
    renderAt('/settings/security')
    expect(await screen.findByText(/This is your only way to sign in\./)).toBeInTheDocument()
    expect(screen.getByText(/^Linked to ann\.synthetic@example\.test/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Unlink…' })).toBeNull()
  })

  it("shows the server's reason when it refuses to unlink the last method", async () => {
    // The page thought a password was there (it was removed meanwhile): the server refuses.
    fakeApi({
      'GET /api/auth/session': SIGNED_IN,
      'GET /api/auth/sessions': [],
      'GET /api/auth/identities': identities([LINKED]),
      'DELETE /api/auth/identities/i1': reason(409, 'last_sign_in_method'),
    })
    renderAt('/settings/security')
    fireEvent.click(await screen.findByRole('button', { name: 'Unlink…' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('This is your only way to sign in.')
    expect(screen.getByText('Linked to ann.synthetic@example.test')).toBeInTheDocument()
  })
})

describe('the outcome of a link, on return', () => {
  const routes = {
    'GET /api/auth/session': SIGNED_IN,
    'GET /api/auth/sessions': [],
    'GET /api/auth/identities': identities([LINKED]),
  }

  it('says Google is linked, at the Security tab, and drops the outcome from the address', async () => {
    fakeApi(routes)
    renderAt('/account?oauth=linked')
    expect(await screen.findByText('Google is linked. You can now sign in with Google.')).toHaveAttribute('role', 'status')
    expect(window.location.pathname).toBe('/settings/security')
    expect(window.location.search).toBe('')
    expect(await screen.findByText('Linked to ann.synthetic@example.test')).toBeInTheDocument()
  })

  it.each([
    ['identity_taken', 'That Google account is already linked to another ownvitals account.'],
    ['identity_mismatch', 'That was a different Google account than the one linked here.'],
    ['<b>made-up</b>', 'The sign-in could not be completed. Try again.'],
  ])('explains %s', async (code, text) => {
    fakeApi({ ...routes, 'GET /api/auth/identities': identities([]) })
    renderAt(`/account?oauth=${encodeURIComponent(code)}`)
    expect(await screen.findByRole('alert')).toHaveTextContent(text)
    expect(screen.queryByText(/<b>/)).toBeNull()
    expect(window.location.pathname).toBe('/settings/security')
    expect(window.location.search).toBe('')
  })

  it('says nothing for a step-up that came back', async () => {
    fakeApi(routes)
    renderAt('/account?oauth=reauthed')
    expect(await screen.findByText('Linked to ann.synthetic@example.test')).toBeInTheDocument()
    expect(screen.queryByRole('alert')).toBeNull()
    expect(window.location.search).toBe('')
  })
})
