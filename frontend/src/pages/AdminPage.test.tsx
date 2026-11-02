import { screen, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { fakeApi } from '../test/fakeApi'
import { renderAt } from '../test/render'

// The Accounts table of the Admin mock-up. Synthetic addresses only.

const ADMIN = {
  authenticated: true,
  csrf_token: 'csrf-session',
  user: { id: 'u-admin', email: 'alex@example.com', role: 'admin' },
  session: {
    id: 's1',
    expires_at: '2030-01-01T00:00:00Z',
    recent_auth: true,
    mfa: true,
    stepup: true,
    totp_enabled: true,
    second_factor: null,
    admin_pending_mfa: false,
  },
}

const PAGE = {
  users: [
    {
      id: 'u-alex',
      email: 'alex@example.com',
      role: 'admin',
      disabled_at: null,
      created_at: '2026-01-01T00:00:00Z',
      second_factor: 'on',
      last_sign_in: '2026-11-02T07:30:00Z',
      sessions: 3,
    },
    {
      id: 'u-robin',
      email: 'robin@example.com',
      role: 'user',
      disabled_at: '2026-10-01T00:00:00Z',
      created_at: '2026-01-02T00:00:00Z',
      second_factor: 'setup_pending',
      last_sign_in: null,
      sessions: 0,
    },
  ],
  next_offset: null,
}

describe('the admin accounts table', () => {
  it('shows the band, the accounts and the operator note', async () => {
    fakeApi({ 'GET /api/auth/session': ADMIN, 'GET /api/admin/users': PAGE })
    renderAt('/admin')
    expect(await screen.findByRole('heading', { level: 1, name: 'Admin' })).toBeInTheDocument()
    expect(screen.getByRole('note')).toHaveTextContent(
      "Health data isn't visible here, by design.",
    )
    expect(screen.getByText(/changes need a code from the last 5 min/)).toBeInTheDocument()
    const table = await screen.findByRole('table')
    const rows = within(table).getAllByRole('row').slice(1)
    expect(within(rows[0] as HTMLElement).getByText('alex@example.com')).toBeInTheDocument()
    expect(within(rows[0] as HTMLElement).getByText('Admin')).toBeInTheDocument()
    expect(within(rows[0] as HTMLElement).getByText('On')).toBeInTheDocument()
    expect(within(rows[0] as HTMLElement).getByText('Not available')).toBeInTheDocument()
    expect(within(rows[0] as HTMLElement).getByText('3')).toBeInTheDocument()
    expect(within(rows[1] as HTMLElement).getByText('robin@example.com')).toBeInTheDocument()
    expect(within(rows[1] as HTMLElement).getByText('User')).toBeInTheDocument()
    expect(within(rows[1] as HTMLElement).getByText('Setup pending')).toBeInTheDocument()
    expect(within(rows[1] as HTMLElement).getByText('Disabled')).toBeInTheDocument()
    expect(within(rows[1] as HTMLElement).getByText('Never')).toBeInTheDocument()
    expect(
      screen.getByText(
        'Role changes and second-factor resets run from the operator command line on the server, never from this page.',
      ),
    ).toBeInTheDocument()
    expect(screen.getByText('The admin pages come later.')).toBeInTheDocument()
  })
})
