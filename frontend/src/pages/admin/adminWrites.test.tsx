import { screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'
import { setLocale } from '../../i18n'
import { fakeApi, reason } from '../../test/fakeApi'
import { renderAt } from '../../test/render'

// The admin writes (issue 550): the flag controls, "Invite someone…" and "Sign out all…", each
// behind the shared step-up dialog, and the Accounts table's Withings states in Swedish. Synthetic
// data only (synthetic: true): the invite code is made up.

const ADMIN = {
  authenticated: true,
  csrf_token: 'csrf-session',
  user: { id: 'u-admin', email: 'alex@example.com', role: 'admin' },
  session: {
    id: 's1', expires_at: '2030-01-01T00:00:00Z', recent_auth: true, mfa: true, stepup: true,
    totp_enabled: true, totp_required: true, second_factor: null, admin_pending_mfa: false,
  },
}

function flag(key: string, effective: unknown, environment: unknown, stored: unknown = null) {
  return {
    key, source: stored === null ? 'environment' : 'database', effective, environment, stored,
    stored_valid: true, updated_at: null, updated_by: null,
  }
}

function account(id: string, email: string, sessions: number, withings: string) {
  return {
    id, email, role: 'user', disabled_at: null, created_at: '2026-01-01T00:00:00Z', second_factor: 'on',
    last_sign_in: null, sessions, withings,
  }
}

const USERS = {
  users: [
    account('u-kim', 'kim@example.com', 0, 'not_connected'),
    account('u-sam', 'sam@example.com', 2, 'needs_reconnect'),
    account('u-alex', 'alex@example.com', 1, 'healthy'),
  ],
  next_offset: null,
}
const SYNTHETIC_CODE = 'synthetic-invite-code-0001'

describe('the flag controls', () => {
  it('sets sign-up to closed, never offers open, and reloads the flags', async () => {
    const api = fakeApi({
      'GET /api/auth/session': ADMIN,
      'GET /api/admin/settings': { flags: [flag('signup_mode', 'invite', 'invite')] },
      'PUT /api/admin/settings/signup_mode': flag('signup_mode', 'closed', 'invite', 'closed'),
    })
    renderAt('/admin?tab=flags')
    const group = await screen.findByRole('group', { name: 'Sign-up' })
    expect(within(group).queryByRole('button', { name: /Open/ })).toBeNull()
    expect(within(group).getByRole('button', { name: 'Invite only' })).toHaveAttribute('aria-pressed', 'true')
    await userEvent.click(within(group).getByRole('button', { name: 'Closed' }))
    await screen.findByRole('group', { name: 'Sign-up' })
    expect(api.to('PUT', '/api/admin/settings/signup_mode').map((call) => call.body)).toEqual([{ value: 'closed' }])
    await expect.poll(() => api.to('GET', '/api/admin/settings').length).toBe(2)
  })

  it('does not offer invite only while the server settings say closed', async () => {
    fakeApi({
      'GET /api/auth/session': ADMIN,
      'GET /api/admin/settings': { flags: [flag('signup_mode', 'closed', 'closed')] },
    })
    renderAt('/admin?tab=flags')
    const group = await screen.findByRole('group', { name: 'Sign-up' })
    expect(within(group).getByRole('button', { name: 'Invite only' })).toBeDisabled()
  })

  it('asks for a fresh code when the server wants a step-up, and a cancel changes nothing', async () => {
    const api = fakeApi({
      'GET /api/auth/session': ADMIN,
      'GET /api/admin/settings': { flags: [flag('signup_mode', 'invite', 'invite')] },
      'PUT /api/admin/settings/signup_mode': reason(403, 'step_up_required'),
    })
    renderAt('/admin?tab=flags')
    const group = await screen.findByRole('group', { name: 'Sign-up' })
    await userEvent.click(within(group).getByRole('button', { name: 'Closed' }))
    const dialog = await screen.findByRole('dialog', { name: "Confirm it's you" })
    expect(dialog).toHaveTextContent('Changing a flag')
    await userEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }))
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(screen.queryByRole('alert')).toBeNull()
    expect(api.to('PUT', '/api/admin/settings/signup_mode')).toHaveLength(1)
    expect(api.to('GET', '/api/admin/settings')).toHaveLength(1)
  })

  it('turns a switch off, or back to the server setting, from "Change…"', async () => {
    const api = fakeApi({
      'GET /api/auth/session': ADMIN,
      'GET /api/admin/settings': {
        flags: [flag('experimental_findings', 'on', 'on'), flag('ai_insights', 'off', 'on', 'off')],
      },
      'PUT /api/admin/settings/experimental_findings': flag('experimental_findings', 'off', 'on', 'off'),
      'DELETE /api/admin/settings/ai_insights': flag('ai_insights', 'on', 'on'),
    })
    renderAt('/admin?tab=flags')
    const experimental = (await screen.findByText('Experimental findings')).closest('.ov-setting') as HTMLElement
    expect(experimental).toHaveTextContent('Set by the server settings.')
    await userEvent.click(within(experimental).getByRole('button', { name: 'Change…' }))
    let dialog = screen.getByRole('dialog', { name: 'Change Experimental findings' })
    expect(within(dialog).queryByRole('button', { name: 'Use the server setting' })).toBeNull()
    expect(within(dialog).getByRole('button', { name: 'Save' })).toBeDisabled()
    await userEvent.click(within(dialog).getByRole('radio', { name: 'Off' }))
    await userEvent.click(within(dialog).getByRole('button', { name: 'Save' }))
    expect(api.to('PUT', '/api/admin/settings/experimental_findings').map((call) => call.body)).toEqual([{ value: 'off' }])
    expect(screen.queryByRole('dialog')).toBeNull()

    const ai = screen.getByText('AI insights').closest('.ov-setting') as HTMLElement
    await userEvent.click(within(ai).getByRole('button', { name: 'Change…' }))
    dialog = screen.getByRole('dialog', { name: 'Change AI insights' })
    await userEvent.click(within(dialog).getByRole('button', { name: 'Use the server setting' }))
    expect(api.to('DELETE', '/api/admin/settings/ai_insights')).toHaveLength(1)
    await expect.poll(() => api.to('GET', '/api/admin/settings').length).toBe(3)
  })

  it('sets the banner text and level', async () => {
    const api = fakeApi({
      'GET /api/auth/session': ADMIN,
      'GET /api/admin/settings': { flags: [flag('banner', null, null)] },
      'PUT /api/admin/settings/banner': flag('banner', { text: 'Maintenance tonight', level: 'warning' }, null, {}),
    })
    renderAt('/admin?tab=flags')
    const banner = (await screen.findByText('Banner')).closest('.ov-setting') as HTMLElement
    await userEvent.click(within(banner).getByRole('button', { name: 'Change…' }))
    const dialog = screen.getByRole('dialog', { name: 'Change Banner' })
    expect(within(dialog).queryByRole('button', { name: 'Remove the banner' })).toBeNull()
    await userEvent.type(within(dialog).getByLabelText('Text'), '  Maintenance tonight ')
    await userEvent.click(within(dialog).getByRole('radio', { name: 'Warning' }))
    await userEvent.click(within(dialog).getByRole('button', { name: 'Save' }))
    expect(api.to('PUT', '/api/admin/settings/banner').map((call) => call.body)).toEqual([
      { value: { text: 'Maintenance tonight', level: 'warning' } },
    ])
  })
})

describe('"Invite someone…"', () => {
  it('shows the new code once and forgets it when the dialog closes', async () => {
    const api = fakeApi({
      'GET /api/auth/session': ADMIN,
      'GET /api/admin/users': USERS,
      'POST /api/admin/invites': { code: SYNTHETIC_CODE, expires_at: '2026-11-24T07:30:00Z' },
    })
    renderAt('/admin?tab=accounts')
    await userEvent.click(await screen.findByRole('button', { name: 'Invite someone…' }))
    const dialog = await screen.findByRole('dialog', { name: 'Invite code' })
    expect(dialog).toHaveTextContent(SYNTHETIC_CODE)
    expect(dialog).toHaveTextContent("It is shown only now: it isn't stored and can't be shown again.")
    expect(api.to('POST', '/api/admin/invites')).toHaveLength(1)
    await userEvent.click(within(dialog).getByRole('button', { name: 'Done' }))
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(document.body).not.toHaveTextContent(SYNTHETIC_CODE)
  })

  it('asks for a fresh code first when the server wants one', async () => {
    fakeApi({
      'GET /api/auth/session': ADMIN,
      'GET /api/admin/users': USERS,
      'POST /api/admin/invites': reason(403, 'step_up_required'),
    })
    renderAt('/admin?tab=accounts')
    await userEvent.click(await screen.findByRole('button', { name: 'Invite someone…' }))
    expect(await screen.findByRole('dialog', { name: "Confirm it's you" })).toHaveTextContent('Creating an invite')
  })
})

describe('"Sign out all…"', () => {
  it('is offered for each account with a session, asks first, then signs it out and reloads', async () => {
    const api = fakeApi({
      'GET /api/auth/session': ADMIN,
      'GET /api/admin/users': USERS,
      'DELETE /api/admin/users/u-sam/sessions': { id: 'u-sam', sessions_revoked: 2 },
    })
    renderAt('/admin?tab=accounts')
    const table = await screen.findByRole('table')
    const rows = within(table).getAllByRole('row').slice(1)
    expect(within(rows[0] as HTMLElement).queryByRole('button', { name: 'Sign out all…' })).toBeNull()
    expect(within(rows[2] as HTMLElement).getByRole('button', { name: 'Sign out all…' })).toBeInTheDocument()
    await userEvent.click(within(rows[1] as HTMLElement).getByRole('button', { name: 'Sign out all…' }))
    const dialog = screen.getByRole('dialog', { name: 'Sign out sam@example.com everywhere?' })
    expect(api.to('DELETE', '/api/admin/users/u-sam/sessions')).toHaveLength(0)
    await userEvent.click(within(dialog).getByRole('button', { name: 'Sign out all' }))
    await expect.poll(() => api.to('GET', '/api/admin/users').length).toBe(2)
    expect(api.to('DELETE', '/api/admin/users/u-sam/sessions')).toHaveLength(1)
    expect(screen.queryByRole('dialog')).toBeNull()
  })

  it("says on the admin's own row that this session ends too", async () => {
    fakeApi({
      'GET /api/auth/session': ADMIN,
      'GET /api/admin/users': {
        users: [account('u-admin', 'alex@example.com', 1, 'healthy'), account('u-sam', 'sam@example.com', 2, 'healthy')],
        next_offset: null,
      },
    })
    renderAt('/admin?tab=accounts')
    const table = await screen.findByRole('table')
    const [own, other] = within(table).getAllByRole('row').slice(1)
    await userEvent.click(within(own as HTMLElement).getByRole('button', { name: 'Sign out all…' }))
    let dialog = screen.getByRole('dialog', { name: 'Sign out alex@example.com everywhere?' })
    expect(dialog).toHaveTextContent('this one too: you will be signed out here.')
    await userEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }))
    await userEvent.click(within(other as HTMLElement).getByRole('button', { name: 'Sign out all…' }))
    dialog = screen.getByRole('dialog', { name: 'Sign out sam@example.com everywhere?' })
    expect(dialog).not.toHaveTextContent('this one too')
  })

  it('says why when the server refuses', async () => {
    fakeApi({
      'GET /api/auth/session': ADMIN,
      'GET /api/admin/users': USERS,
      'DELETE /api/admin/users/u-sam/sessions': reason(404, 'not_found'),
    })
    renderAt('/admin?tab=accounts')
    const table = await screen.findByRole('table')
    const sam = within(table).getAllByRole('row')[2] as HTMLElement
    await userEvent.click(within(sam).getByRole('button', { name: 'Sign out all…' }))
    const dialog = screen.getByRole('dialog', { name: 'Sign out sam@example.com everywhere?' })
    await userEvent.click(within(dialog).getByRole('button', { name: 'Sign out all' }))
    expect(await within(dialog).findByRole('alert')).toBeInTheDocument()
  })
})

describe('the Accounts table in Swedish', () => {
  it('shows the Withings states in Swedish', async () => {
    fakeApi({ 'GET /api/auth/session': ADMIN, 'GET /api/admin/users': USERS })
    await setLocale('sv')
    try {
      renderAt('/admin?tab=accounts')
      const table = await screen.findByRole('table')
      const rows = within(table).getAllByRole('row').slice(1)
      expect(within(rows[0] as HTMLElement).getByText('Inte ansluten')).toHaveClass('ov-state--off')
      expect(within(rows[1] as HTMLElement).getByText('Behöver anslutas igen')).toHaveClass('ov-state--urgent')
      expect(within(rows[2] as HTMLElement).getByText('Fungerar')).toHaveClass('ov-state--ok')
      expect(screen.getByRole('button', { name: 'Bjud in någon…' })).toBeInTheDocument()
    } finally {
      await setLocale('en')
    }
  })
})
