import { screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'
import { eventLabel } from '../../components/security/SecurityActivity'
import { fakeApi, reason } from '../../test/fakeApi'
import { renderAt } from '../../test/render'

// The admin abuse view (issue 461). Synthetic accounts and origins only; never an address.

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
    totp_required: true,
    second_factor: null,
    admin_pending_mfa: false,
  },
}
const USERS = { users: [], next_offset: null }

function row(id: string, email: string | null, signals: string[]) {
  return { id, at: '2026-10-02T07:30:00Z', user_id: email === null ? null : `u-${id}`, email, signals, browser: 'Firefox', os: 'Linux', place: null }
}

describe('the admin abuse view', () => {
  it('loads only when asked, then shows the accounts, signals and origin', async () => {
    const api = fakeApi({
      'GET /api/auth/session': ADMIN,
      'GET /api/admin/users': USERS,
      'GET /api/admin/anomalies': {
        rows: [row('a1', 'robin@example.com', ['new_ip_and_agent', 'request_rate']), row('a2', null, ['later_code'])],
        next: { before_at: '2026-10-01T00:00:00Z', before_id: 'a2' },
      },
      'GET /api/admin/anomalies?before_at=2026-10-01T00%3A00%3A00Z&before_id=a2': {
        rows: [row('a3', 'kim@example.com', ['country_change'])],
        next: null,
      },
    })
    renderAt('/admin')
    const show = await screen.findByRole('button', { name: 'Show anomalies' })
    expect(api.calls.some((call) => call.path.startsWith('/api/admin/anomalies'))).toBe(false)
    await userEvent.click(show)
    const table = await screen.findByRole('table')
    const rows = within(table).getAllByRole('row').slice(1)
    expect(within(rows[0] as HTMLElement).getByText('robin@example.com')).toBeInTheDocument()
    expect(within(rows[0] as HTMLElement).getByText('New network and browser, Too many requests')).toBeInTheDocument()
    expect(within(rows[0] as HTMLElement).getByText('Firefox on Linux')).toBeInTheDocument()
    expect(within(rows[1] as HTMLElement).getByText('Deleted account')).toBeInTheDocument()
    expect(within(rows[1] as HTMLElement).getByText('Other signal')).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'Show older' }))
    expect(await screen.findByText('kim@example.com')).toBeInTheDocument()
    expect(screen.getByText('Another country')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Show older' })).not.toBeInTheDocument()
  })

  it('says so when there is nothing to show', async () => {
    fakeApi({
      'GET /api/auth/session': ADMIN,
      'GET /api/admin/users': USERS,
      'GET /api/admin/anomalies': { rows: [], next: null },
    })
    renderAt('/admin')
    await userEvent.click(await screen.findByRole('button', { name: 'Show anomalies' }))
    expect(await screen.findByText('No anomalies in the last 30 days.')).toBeInTheDocument()
  })

  it('asks for a fresh code when the server wants a step-up', async () => {
    fakeApi({
      'GET /api/auth/session': ADMIN,
      'GET /api/admin/users': USERS,
      'GET /api/admin/anomalies': reason(403, 'step_up_required'),
    })
    renderAt('/admin')
    await userEvent.click(await screen.findByRole('button', { name: 'Show anomalies' }))
    expect(await screen.findByRole('dialog')).toHaveTextContent('Viewing session anomalies')
  })

  it('words the user-facing event', () => {
    expect(eventLabel({ kind: 'session_anomaly', method: null })).toBe('Unusual use of a signed-in session')
  })
})
