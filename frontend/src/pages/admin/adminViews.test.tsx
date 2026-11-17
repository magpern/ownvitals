import { screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'
import { setLocale } from '../../i18n'
import { fakeApi, reason } from '../../test/fakeApi'
import { renderAt } from '../../test/render'

// The admin tabs, the Overview tiles, the Withings app card and the read-only Jobs, Flags and Audit
// log views. Synthetic data only (synthetic: true); the job, audit and app fixtures carry fields
// the screens must never show.

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

const TILES = {
  accounts: { total: 4, second_factor_on: 3, signup: 'invite' },
  connections: { healthy: 2, needs_reconnect: 1, not_connected: 1 },
  jobs: { queued: 12, running: 2, dead_letter: 1 },
}

const APP = {
  configured: true,
  client_hint: '…a91f',
  api_host: 'wbsapi.withings.net',
  // Never sent by the server; the card must not show it even if it were.
  client_secret: 'synthetic-client-secret',
}

const OVERVIEW = {
  'GET /api/admin/users': { users: [], next_offset: null },
  'GET /api/admin/overview': TILES,
  'GET /api/admin/withings-app': APP,
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

  it('shows the whole page on Overview: the tiles, then every section', async () => {
    fakeApi({ 'GET /api/auth/session': ADMIN, ...OVERVIEW })
    renderAt('/admin')
    for (const name of ['Accounts', 'Jobs', 'Flags', 'Audit log']) {
      expect(await screen.findByRole('heading', { level: 2, name })).toBeInTheDocument()
    }
    expect(await screen.findByText('Job queue')).toBeInTheDocument()
    expect(screen.getByRole('heading', { level: 2, name: 'Withings app' })).toBeInTheDocument()
    expect(screen.queryByText('Coming later')).toBeNull()
    expect(within(screen.getAllByRole('table')[0] as HTMLElement).getByText('queued')).toBeInTheDocument()
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

function tile(label: string): HTMLElement {
  const found = screen.getAllByText(label).map((node) => node.closest('.ov-admin-tile')).filter((node) => node !== null)
  expect(found).toHaveLength(1)
  return found[0] as HTMLElement
}

describe('the Overview tiles', () => {
  it('show the four counts of the mock-up, with dots on what needs a look', async () => {
    fakeApi({ 'GET /api/auth/session': ADMIN, ...OVERVIEW })
    renderAt('/admin')
    await screen.findByText('Job queue')
    expect(tile('Accounts')).toHaveTextContent('4Household · invite only')
    expect(tile('Connections')).toHaveTextContent('2 healthy1 needs reconnect')
    expect(tile('Connections').querySelector('.ov-dot--urgent')).not.toBeNull()
    expect(tile('Job queue')).toHaveTextContent('12 queued2 running · 1 dead-letter')
    expect(tile('Job queue').querySelector('.ov-dot--attn')).not.toBeNull()
    expect(tile('Second factor')).toHaveTextContent('3 of 4 set up1 setup pending')
    expect(screen.getByText(/server metrics in Prometheus/)).toBeInTheDocument()
  })

  it('show no dot for a zero and say so in words', async () => {
    const calm = {
      accounts: { total: 1, second_factor_on: 1, signup: 'closed' },
      connections: { healthy: 1, needs_reconnect: 0, not_connected: 0 },
      jobs: { queued: 0, running: 0, dead_letter: 0 },
    }
    fakeApi({ 'GET /api/auth/session': ADMIN, ...OVERVIEW, 'GET /api/admin/overview': calm })
    renderAt('/admin')
    await screen.findByText('Job queue')
    expect(tile('Accounts')).toHaveTextContent('sign-up closed')
    expect(tile('Connections')).toHaveTextContent('0 need reconnect')
    expect(tile('Second factor')).toHaveTextContent('0 setup pending')
    expect(document.querySelectorAll('.ov-admin-tile .ov-dot')).toHaveLength(0)
  })

  it('say why when the read fails', async () => {
    fakeApi({ 'GET /api/auth/session': ADMIN, ...OVERVIEW, 'GET /api/admin/overview': reason(503, 'admin_unavailable') })
    renderAt('/admin')
    expect((await screen.findAllByRole('alert')).length).toBeGreaterThan(0)
    expect(screen.queryByText('Job queue')).toBeNull()
  })

  it('are in Swedish too', async () => {
    fakeApi({ 'GET /api/auth/session': ADMIN, ...OVERVIEW })
    await setLocale('sv')
    try {
      renderAt('/admin')
      await screen.findByText('Jobbkö')
      expect(tile('Anslutningar')).toHaveTextContent('2 fungerar1 behöver anslutas igen')
      expect(tile('Andra faktorn')).toHaveTextContent('3 av 4 konfigurerade')
    } finally {
      await setLocale('en')
    }
  })
})

describe('the Withings app card', () => {
  it('shows the app, the last four characters of the client id and the API host, nothing else', async () => {
    fakeApi({ 'GET /api/auth/session': ADMIN, 'GET /api/admin/withings-app': APP })
    renderAt('/admin?tab=withings')
    const app = (await screen.findByText('App')).closest('dl') as HTMLElement
    expect(app).toHaveTextContent('ownvitals · client …a91f')
    expect(within(app).getByText('wbsapi.withings.net')).toHaveClass('ov-mono')
    expect(screen.getByText(/never shown here/)).toBeInTheDocument()
    expect(document.body).not.toHaveTextContent('synthetic-client-secret')
    expect(within(app.closest('section') as HTMLElement).queryByRole('button')).toBeNull()
  })

  it('says when no app is configured', async () => {
    fakeApi({
      'GET /api/auth/session': ADMIN,
      'GET /api/admin/withings-app': { configured: false, client_hint: null, api_host: 'wbsapi.withings.net' },
    })
    renderAt('/admin?tab=withings')
    expect(await screen.findByText(/No Withings app is configured yet/)).toBeInTheDocument()
    expect(screen.queryByText('App')).toBeNull()
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
  it('shows each flag with its effective value and source', async () => {
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
    // Both switches are off in the server settings here, so neither can be turned on (issue 550).
    for (const name of ['AI insights, off', 'Experimental findings, off']) {
      expect(screen.getByRole('switch', { name })).toBeDisabled()
    }
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
