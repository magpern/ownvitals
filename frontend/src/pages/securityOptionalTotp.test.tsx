import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'
import { MFA_ON, SIGNED_IN, SIGNED_IN_OPTIONAL_TOTP, fakeApi, reason } from '../test/fakeApi'
import { renderAt } from '../test/render'
import { SIGNIN_OPTIONS, installWebAuthn, uninstallWebAuthn } from '../test/webauthnFake'
import { markTotpOffered } from '../auth/totpOffer'

// Issue 475: the authenticator app is optional for users and required for administrators. The
// Security tab's "Second factor" card, and the "Confirm it's you" dialog for an account without
// one (the password, Google or a passkey is the step-up). Everything is synthetic.

const here = () => window.location.pathname + window.location.search
const SIGNED_OUT = { authenticated: false, csrf_token: 'a' }
const PASSKEY = {
  id: 'p1',
  name: 'Laptop',
  created_at: '2030-01-01T00:00:00Z',
  last_used_at: null,
  synced: false,
  backed_up: false,
  suspended: false,
}

afterEach(() => {
  uninstallWebAuthn()
})

/** A user without TOTP; "Sign out everywhere" is refused until a fresh first factor. */
function plainRoutes() {
  let proved = false
  let out = false
  return {
    'GET /api/auth/session': () => (out ? SIGNED_OUT : SIGNED_IN_OPTIONAL_TOTP),
    'GET /api/auth/sessions': [],
    'GET /api/auth/mfa': { totp_enabled: false, recovery_codes_left: 0 },
    'GET /api/auth/passkeys': { passkeys: [PASSKEY], limit: 10 },
    'GET /api/auth/identities': { identities: [], has_password: true, available: ['google'] },
    'POST /api/auth/logout-all': () => {
      if (!proved) return reason(403, 'step_up_required')
      out = true
      return new Response(null, { status: 204 })
    },
    'POST /api/auth/reauth': () => {
      proved = true
      return { status: 'ok' }
    },
    'POST /api/auth/passkeys/reauth/options': SIGNIN_OPTIONS,
    'POST /api/auth/passkeys/reauth': () => {
      proved = true
      return { status: 'ok' }
    },
  }
}

describe('the second factor card', () => {
  it('recommends the authenticator app while it is off and links to its setup', async () => {
    markTotpOffered('u1')
    fakeApi(plainRoutes())
    renderAt('/settings/security')
    expect(await screen.findByText(/confirm with your password, Google or a passkey/)).toBeInTheDocument()
    const card = (await screen.findByRole('heading', { name: 'Second factor' })).closest('section') ?? document.body
    expect(within(card).getByText('Off')).toBeInTheDocument()
    expect(within(card).getByText(/Recommended/)).toBeInTheDocument()
    expect(within(card).queryByRole('button', { name: 'Turn off…' })).toBeNull()
    fireEvent.click(within(card).getByRole('button', { name: 'Set up' }))
    await waitFor(() => {
      expect(here()).toBe('/two-factor/setup?return_to=%2Fsettings%2Fsecurity')
    })
  })

  it('turns the authenticator app off behind a fresh code, then reads the session again', async () => {
    let code = false
    let removed = false
    const api = fakeApi({
      'GET /api/auth/session': () =>
        removed ? { ...SIGNED_IN, session: { ...SIGNED_IN.session, totp_enabled: false } } : SIGNED_IN,
      'GET /api/auth/sessions': [],
      'GET /api/auth/mfa': () => (removed ? { totp_enabled: false, recovery_codes_left: 0 } : MFA_ON),
      'GET /api/auth/passkeys': { passkeys: [], limit: 10 },
      'DELETE /api/auth/mfa/totp': () => {
        if (!code) return reason(403, 'step_up_required')
        removed = true
        return new Response(null, { status: 204 })
      },
      'POST /api/auth/mfa/stepup': () => {
        code = true
        return { ok: true }
      },
    })
    markTotpOffered('u1')
    renderAt('/settings/security')
    expect(await screen.findByText('8 left. Each works once if you lose your phone.')).toBeInTheDocument()
    fireEvent.click(await screen.findByRole('button', { name: 'Turn off…' }))
    const dialog = await screen.findByRole('dialog', { name: "Confirm it's you" })
    expect(within(dialog).getByText(/Turning off the authenticator app needs a fresh code/)).toBeInTheDocument()
    const boxes = within(within(dialog).getByRole('group', { name: '6-digit code' })).getAllByRole('textbox')
    fireEvent.change(boxes[0] as HTMLElement, { target: { value: '123456' } })
    fireEvent.click(within(dialog).getByRole('button', { name: 'Confirm' }))
    expect(await screen.findByRole('button', { name: 'Set up' })).toBeInTheDocument()
    expect(api.to('DELETE', '/api/auth/mfa/totp')).toHaveLength(2)
  })

  it('offers no way to turn it off to an administrator, who must have it', async () => {
    fakeApi({
      'GET /api/auth/session': { ...SIGNED_IN, session: { ...SIGNED_IN.session, totp_required: true } },
      'GET /api/auth/sessions': [],
      'GET /api/auth/mfa': MFA_ON,
      'GET /api/auth/passkeys': { passkeys: [], limit: 10 },
    })
    renderAt('/settings/security')
    expect(await screen.findByText('On · required')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Turn off…' })).toBeNull()
  })
})

describe('the step-up dialog without an authenticator app', () => {
  it('asks for the password alone on a step-up, then does the action', async () => {
    markTotpOffered('u1')
    const api = fakeApi(plainRoutes())
    renderAt('/settings/security')
    fireEvent.click(await screen.findByRole('button', { name: 'Sign out everywhere…' }))
    const dialog = await screen.findByRole('dialog', { name: "Confirm it's you" })
    expect(within(dialog).getByText(/Signing out everywhere needs your password\. It also covers/)).toBeInTheDocument()
    expect(within(dialog).queryByRole('group', { name: '6-digit code' })).toBeNull()
    fireEvent.change(within(dialog).getByLabelText('Password'), { target: { value: 'synthetic password' } })
    fireEvent.click(within(dialog).getByRole('button', { name: 'Confirm' }))
    expect(await screen.findByRole('heading', { name: 'Sign in' })).toBeInTheDocument()
    expect(api.to('POST', '/api/auth/reauth')[0]?.body).toEqual({ password: 'synthetic password' })
    expect(api.to('POST', '/api/auth/mfa/stepup')).toHaveLength(0)
  })

  it('takes a passkey instead when the account has one', async () => {
    const { get } = installWebAuthn()
    markTotpOffered('u1')
    const api = fakeApi(plainRoutes())
    renderAt('/settings/security')
    fireEvent.click(await screen.findByRole('button', { name: 'Sign out everywhere…' }))
    const dialog = await screen.findByRole('dialog', { name: "Confirm it's you" })
    fireEvent.click(await within(dialog).findByRole('button', { name: 'Use a passkey instead' }))
    expect(await screen.findByRole('heading', { name: 'Sign in' })).toBeInTheDocument()
    expect(get).toHaveBeenCalledTimes(1)
    expect(api.to('POST', '/api/auth/passkeys/reauth')).toHaveLength(1)
    expect(api.to('POST', '/api/auth/reauth')).toHaveLength(0)
  })
})
