import { screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'
import { setLocale } from '../../i18n'
import { fakeApi, reason } from '../../test/fakeApi'
import { renderAt } from '../../test/render'

// The admin tabs and the read-only Jobs, Flags and Audit log views. Synthetic data only
// (synthetic: true); the job and audit fixtures carry fields the screens must never show.

const ADMIN = {
  authenticated: true,
  csrf_token: 'csrf-session',
  user: { id: 'u-admin', email: 'alex@example.com', role: 'admin' },
  session: {
    id: 's1', expires_at: '2030-01-01T00:00:00Z', recent_auth: true, mfa: true, stepup: true,
    totp_enabled: true, totp_required: true, second_factor: null, admin_pending_mfa: false,
  },
}

const JOBS = {
  days: 7,
  states: [{ state: 'queued', count: 12 }, { state: 'dead_letter', count: 1 }],
  failures: [{ kind: 'synthetic_secret_kind', category: 'synthetic_category', count: 3, last_at: '2026-11-02T07:30:00Z' }],
}

function flagRow(key: string, source: string, effective: unknown, stored_valid = true) {
  return { key, source, effective, environment: effective, stored: null, stored_valid, updated_at: null, updated_by: 'synthetic-admin-id' }
}

function auditRow(id: number, action: string, outcome: string, target_kind: string | null, target_id: string | null) {
  return {
    id, created_at: '2026-10-02T07:30:00Z', actor_id: 'u-admin', actor_email: 'alex@example.com', action,
    target_user_id: null, target_kind, target_id, params: { secret: 'synthetic-param' }, outcome, reason: null, request_id: 'synthetic-request',
  }
}

const OVERVIEW = {
  'GET /api/admin/users': { users: [], next_offset: null },
  'GET /api/admin/jobs': JOBS,
  'GET /api/admin/settings': { flags: [flagRow('signup_mode', 'database', 'invite')] },
  'GET /api/admin/audit': { rows: [auditRow(5, 'flag.set', 'ok', 'setting', 'signup_mode')], next_before: null },
}

describe('the admin tabs', () => {
  it('lists the six tabs and marks the current one', async () => {
    fakeApi({ 'GET /api/auth/session': ADMIN, ...OVERVIEW })
    renderAt('/admin')
    const nav = await screen.findByRole('navigation', { name: 'Admin' })
    expect(within(nav).getAllByRole('link').map((a) => a.textContent)).toEqual([
      'Overview', 'Accounts', 'Jobs', 'Withings app', 'Flags', 'Audit log',
    ])
    expect(within(nav).getByRole('link', { name: 'Overview' })).toHaveAttribute('aria-current', 'page')
    expect(screen.queryByText('The admin pages come later.')).toBeNull()
  })

  it('shows the whole page on Overview: tiles and Withings app as Coming later, then every section', async () => {
    fakeApi({ 'GET /api/auth/session': ADMIN, ...OVERVIEW })
    renderAt('/admin')
    for (const name of ['Accounts', 'Jobs', 'Flags', 'Audit log']) {
      expect(await screen.findByRole('heading', { level: 2, name })).toBeInTheDocument()
    }
    expect(screen.getAllByText('Coming later')).toHaveLength(2)
    expect(screen.getByText('queued')).toBeInTheDocument()
    expect(screen.getByText('Sign-up')).toBeInTheDocument()
    expect(await screen.findByText('flag.set')).toBeInTheDocument()
  })

  it('shows one section on the other tabs', async () => {
    fakeApi({ 'GET /api/auth/session': ADMIN, 'GET /api/admin/jobs': JOBS })
    renderAt('/admin')
    await userEvent.click(await screen.findByRole('link', { name: 'Jobs' }))
    expect(await screen.findByRole('table')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Jobs' })).toHaveAttribute('aria-current', 'page')
    expect(screen.queryByRole('heading', { level: 2, name: 'Flags' })).toBeNull()
  })
})

describe('the Jobs view', () => {
  it('shows counts per state and nothing from the failures', async () => {
    fakeApi({ 'GET /api/auth/session': ADMIN, 'GET /api/admin/jobs': JOBS })
    renderAt('/admin?tab=jobs')
    const table = await screen.findByRole('table')
    expect(within(table).getByText('queued').closest('tr')).toHaveTextContent('12')
    expect(within(table).getByText('dead_letter').closest('tr')).toHaveTextContent('1')
    expect(screen.getByText(/no ids, kinds, owners or job input/)).toBeInTheDocument()
    expect(screen.getByText(/ownvitals jobs replay/)).toBeInTheDocument()
    expect(within(table).getByText('1')).toHaveAttribute('data-attn', 'true')
    expect(within(table).getByText('12')).not.toHaveAttribute('data-attn')
    expect(document.body).not.toHaveTextContent('synthetic_secret_kind')
    expect(document.body).not.toHaveTextContent('synthetic_category')
  })

  it('says why when the read fails', async () => {
    fakeApi({ 'GET /api/auth/session': ADMIN, 'GET /api/admin/jobs': reason(503, 'admin_unavailable') })
    renderAt('/admin?tab=jobs')
    expect(await screen.findByRole('alert')).toBeInTheDocument()
    expect(screen.queryByRole('table')).toBeNull()
  })
})

describe('the Flags view', () => {
  it('shows each flag with its effective value and source, and no controls', async () => {
    fakeApi({
      'GET /api/auth/session': ADMIN,
      'GET /api/admin/settings': {
        flags: [
          flagRow('signup_mode', 'database', 'invite'),
          flagRow('ai_insights', 'environment', false),
          flagRow('experimental_findings', 'environment', 'off', false),
          flagRow('banner', 'environment', null),
        ],
      },
    })
    renderAt('/admin?tab=flags')
    const signup = (await screen.findByText('Sign-up')).closest('.ov-setting')
    expect(signup).toHaveTextContent('Invite only')
    expect(signup).toHaveTextContent('Stored value in use.')
    expect(screen.getByText('AI insights').closest('.ov-setting')).toHaveTextContent('Off')
    expect(screen.getByText('Experimental findings').closest('.ov-setting')).toHaveTextContent('safe default')
    expect(screen.getByText('Banner').closest('.ov-setting')).toHaveTextContent('None')
    const card = screen.getByText('Sign-up').closest('section')
    expect(card).not.toBeNull()
    expect(within(card as HTMLElement).queryByRole('button')).toBeNull()
    expect(screen.queryByRole('switch')).toBeNull()
    expect(document.body).not.toHaveTextContent('synthetic-admin-id')
  })
})

describe('the Flags view in Swedish', () => {
  it('translates the flag names, sources and known values, and shows an unknown value as sent', async () => {
    fakeApi({
      'GET /api/auth/session': ADMIN,
      'GET /api/admin/settings': {
        flags: [flagRow('signup_mode', 'database', 'invite'), flagRow('ai_insights', 'environment', false), flagRow('banner', 'environment', { text: 'Hej', level: 'info' })],
      },
    })
    await setLocale('sv')
    try {
      renderAt('/admin?tab=flags')
      const signup = (await screen.findByText('Registrering')).closest('.ov-setting')
      expect(signup).toHaveTextContent('Endast inbjudan')
      expect(screen.getByText('AI-insikter').closest('.ov-setting')).toHaveTextContent('Av')
      expect(screen.getByText('Banderoll').closest('.ov-setting')).toHaveTextContent('info: Hej')
    } finally {
      await setLocale('en')
    }
  })
})

describe('the Audit log view', () => {
  it('shows when, action, target and result, never params or the request id, and pages back', async () => {
    const api = fakeApi({
      'GET /api/auth/session': ADMIN,
      'GET /api/admin/audit': {
        rows: [auditRow(5, 'flag.set', 'ok', 'setting', 'signup_mode'), auditRow(4, 'admin.request', 'refused', null, null)],
        next_before: 4,
      },
      'GET /api/admin/audit?before=4': { rows: [auditRow(3, 'job.list', 'error', null, null)], next_before: null },
    })
    renderAt('/admin?tab=audit')
    const table = await screen.findByRole('table')
    const first = within(table).getByText('flag.set').closest('tr')
    expect(first).toHaveTextContent('setting signup_mode')
    expect(first).toHaveTextContent('Done')
    expect(within(table).getByText('admin.request').closest('tr')).toHaveTextContent('Refused')
    expect(document.body).not.toHaveTextContent('synthetic-param')
    expect(document.body).not.toHaveTextContent('synthetic-request')
    expect(screen.getByText(/Append-only/)).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'Show older entries' }))
    expect(await within(table).findByText('job.list')).toBeInTheDocument()
    expect(within(table).getByText('job.list').closest('tr')).toHaveTextContent('Failed')
    expect(screen.queryByRole('button', { name: 'Show older entries' })).toBeNull()
    expect(api.calls.filter((c) => c.path.startsWith('/api/admin/audit'))).toHaveLength(2)
  })

  it('shows the date with the time', async () => {
    fakeApi({ 'GET /api/auth/session': ADMIN, 'GET /api/admin/audit': { rows: [auditRow(1, 'job.list', 'ok', null, null)], next_before: null } })
    renderAt('/admin?tab=audit')
    const row = (await screen.findByText('job.list')).closest('tr')
    expect(row).toHaveTextContent(/2026.*\d{2}:\d{2}/)
  })

  it('retries a failed load of older entries', async () => {
    let attempts = 0
    fakeApi({
      'GET /api/auth/session': ADMIN,
      'GET /api/admin/audit': { rows: [auditRow(5, 'flag.set', 'ok', null, null)], next_before: 5 },
      'GET /api/admin/audit?before=5': () => (++attempts === 1 ? reason(503, 'admin_unavailable') : { rows: [auditRow(4, 'job.list', 'ok', null, null)], next_before: null }),
    })
    renderAt('/admin?tab=audit')
    await userEvent.click(await screen.findByRole('button', { name: 'Show older entries' }))
    await userEvent.click(await screen.findByRole('button', { name: 'Try again' }))
    expect(await screen.findByText('job.list')).toBeInTheDocument()
    expect(attempts).toBe(2)
  })

  it('offers a retry when the first load fails', async () => {
    fakeApi({ 'GET /api/auth/session': ADMIN, 'GET /api/admin/audit': reason(503, 'admin_unavailable') })
    renderAt('/admin?tab=audit')
    expect(await screen.findByRole('button', { name: 'Try again' })).toBeInTheDocument()
  })

  it('shows an empty log', async () => {
    fakeApi({ 'GET /api/auth/session': ADMIN, 'GET /api/admin/audit': { rows: [], next_before: null } })
    renderAt('/admin?tab=audit')
    expect(await screen.findByText('No entries.')).toBeInTheDocument()
  })
})
