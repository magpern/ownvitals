import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { leave } from '../api/connections'
import { setLocale } from '../i18n'
import { SIGNED_IN, fakeApi, reason } from '../test/fakeApi'
import { fakeStatus, fakeStream } from '../test/overviewFixtures'
import { renderAt } from '../test/render'

// The Connections screen and Onboarding against synthetic API answers (no network, no account).

const ROW = {
  id: 'c1',
  provider: 'withings',
  status: 'connected',
  last_error: null,
  connected_at: '2026-10-20T08:00:00Z',
  last_used_at: '2026-11-01T11:30:00Z',
}
const connections = (...rows: object[]) => ({ connections: rows })
const STEP_UP = "Confirm it's you"

function typeCode(code: string) {
  const dialog = screen.getByRole('dialog', { name: STEP_UP })
  const boxes = within(within(dialog).getByRole('group', { name: '6-digit code' })).getAllByRole('textbox')
  fireEvent.change(boxes[0] as HTMLElement, { target: { value: code } })
  fireEvent.click(within(dialog).getByRole('button', { name: 'Confirm' }))
}

function open(url: string, status: object, rows: object[] = [ROW], more: Record<string, unknown> = {}) {
  const api = fakeApi({
    'GET /api/auth/session': SIGNED_IN,
    'GET /api/status': status,
    'GET /api/connections': connections(...rows),
    ...more,
  })
  renderAt(url)
  return api
}

afterEach(async () => {
  await setLocale('en')
  vi.restoreAllMocks()
})

describe('Connections: states', () => {
  it('shows a connected account with its streams and how updates arrive', async () => {
    open('/connections', fakeStatus({ streams: [fakeStream('measures'), fakeStream('ecg', 'not_supported')] }))
    expect((await screen.findAllByText('Connected')).length).toBeGreaterThan(0)
    expect(screen.getByText(/A safety check runs every 6 hours/)).toBeInTheDocument()
    const table = screen.getByRole('table')
    expect(within(table).getAllByRole('row')).toHaveLength(3)
    expect(within(table).getByText('Not supported')).toBeInTheDocument()
    expect(screen.getByText('Coming later')).toBeInTheDocument()
  })

  it('offers Connect when nothing is connected', async () => {
    open('/connections', fakeStatus({ connection: 'not_connected', streams: [] }), [])
    expect(await screen.findByText('Not connected.')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Connect Withings' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Disconnect…' })).toBeNull()
  })

  it('says Needs reconnect with a Reconnect button, and keeps the card', async () => {
    open('/connections', fakeStatus({ connection: 'needs_reconnect' }), [{ ...ROW, status: 'needs_reconnect' }])
    expect(await screen.findByText('Needs reconnect.')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Reconnect' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Disconnect…' })).toBeInTheDocument()
  })

  it('explains Withings+ with no Connect button', async () => {
    open('/connections', fakeStatus({ connection: 'withings_plus_needed', streams: [] }), [])
    expect(await screen.findByText('Withings+ needed.')).toBeInTheDocument()
    expect(screen.getByText(/ownvitals itself is free/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Connect Withings' })).toBeNull()
  })

  it('names the stream that was not shared and offers to reconnect for it', async () => {
    open('/connections', fakeStatus({ streams: [fakeStream('measures'), fakeStream('sleep_summary', 'not_shared')] }))
    expect(await screen.findByText('Not shared.')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Reconnect to share/ })).toBeInTheDocument()
  })

  it('never draws an unreadable stream as zero or as a date', async () => {
    open('/connections', fakeStatus({ streams: [fakeStream('ecg', 'not_available')] }))
    const table = await screen.findByRole('table')
    const row = within(table).getAllByRole('row')[1] as HTMLElement
    expect(within(row).getByText('No')).toBeInTheDocument()
    expect(within(row).getByText('Not yet')).toBeInTheDocument()
  })

  it('shows the reason when the status cannot be read', async () => {
    open('/connections', reason(500, 'server_error'))
    expect(await screen.findByRole('alert')).toBeInTheDocument()
  })

  it('says why Withings did not connect when it returns with an outcome', async () => {
    open('/connections?withings=linked_elsewhere', fakeStatus())
    expect(await screen.findByText(/already connected to another ownvitals account/)).toBeInTheDocument()
  })

  it('reads in Swedish', async () => {
    await setLocale('sv')
    open('/connections', fakeStatus({ connection: 'not_connected', streams: [] }), [])
    expect(await screen.findByRole('button', { name: 'Anslut Withings' })).toBeInTheDocument()
  })
})

describe('Connections: connect and disconnect', () => {
  it('connects through the step-up dialog, then leaves for Withings', async () => {
    const go = vi.spyOn(leave, 'go').mockImplementation(() => undefined)
    let code = false
    const api = open('/connections', fakeStatus({ connection: 'not_connected', streams: [] }), [], {
      'POST /api/connections/withings/start': () =>
        code ? { authorize_url: 'https://account.withings.example/oauth2_user/authorize2?x=1' } : reason(403, 'step_up_required'),
      'POST /api/auth/mfa/stepup': () => {
        code = true
        return { ok: true }
      },
    })
    fireEvent.click(await screen.findByRole('button', { name: 'Connect Withings' }))
    await screen.findByRole('dialog', { name: STEP_UP })
    typeCode('123456')
    await waitFor(() => {
      expect(go).toHaveBeenCalledWith('https://account.withings.example/oauth2_user/authorize2?x=1')
    })
    expect(api.to('POST', '/api/connections/withings/start')).toHaveLength(2)
  })

  it('refuses an authorize address that is not https', async () => {
    const go = vi.spyOn(leave, 'go').mockImplementation(() => undefined)
    open('/connections', fakeStatus({ connection: 'not_connected', streams: [] }), [], {
      'POST /api/connections/withings/start': { authorize_url: 'javascript:alert(1)' },
    })
    fireEvent.click(await screen.findByRole('button', { name: 'Connect Withings' }))
    expect(await screen.findByRole('alert')).toBeInTheDocument()
    expect(go).not.toHaveBeenCalled()
  })

  async function disconnect(choice: 'keep' | 'delete') {
    let code = false
    let live = true
    const deletes: string[] = []
    fakeApi({
      'GET /api/auth/session': SIGNED_IN,
      'GET /api/status': () => fakeStatus(live ? {} : { connection: 'not_connected', streams: [] }),
      'GET /api/connections': () => connections(...(live ? [ROW] : [])),
      'DELETE /api/connections/withings': () => (code ? ((live = false), { revoke_failed: false }) : reason(403, 'step_up_required')),
      'DELETE /api/connections/withings?delete_imported=true': () => {
        deletes.push('x')
        return code ? ((live = false), { revoke_failed: false, imported_deleted: true }) : reason(403, 'step_up_required')
      },
      'POST /api/auth/mfa/stepup': () => ((code = true), { ok: true }),
    })
    renderAt('/connections')
    fireEvent.click(await screen.findByRole('button', { name: 'Disconnect…' }))
    const dialog = await screen.findByRole('dialog', { name: 'Disconnect Withings?' })
    if (choice === 'delete') fireEvent.click(within(dialog).getByRole('radio', { name: /Delete it too/ }))
    fireEvent.click(within(dialog).getByRole('button', { name: 'Disconnect' }))
    await screen.findByRole('dialog', { name: STEP_UP })
    typeCode('123456')
    return deletes
  }

  it('keeps the data by default, after a step-up', async () => {
    const deletes = await disconnect('keep')
    expect(await screen.findByText('Withings is disconnected.')).toBeInTheDocument()
    expect(deletes).toHaveLength(0)
    expect(await screen.findByText('Not connected.')).toBeInTheDocument()
  })

  it('deletes the imported data when asked', async () => {
    const deletes = await disconnect('delete')
    expect(await screen.findByText('Withings is disconnected and the imported data is deleted.')).toBeInTheDocument()
    expect(deletes.length).toBeGreaterThan(0)
  })

  it('closes with Cancel and sends nothing', async () => {
    const api = open('/connections', fakeStatus())
    fireEvent.click(await screen.findByRole('button', { name: 'Disconnect…' }))
    const dialog = await screen.findByRole('dialog', { name: 'Disconnect Withings?' })
    fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }))
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(api.to('DELETE', '/api/connections/withings')).toHaveLength(0)
  })
})

describe('Onboarding', () => {
  it('offers Connect with what is read and the Withings+ note', async () => {
    open('/onboarding', fakeStatus({ connection: 'not_connected', streams: [] }), [])
    expect(await screen.findByRole('heading', { name: 'Connect your Withings account' })).toBeInTheDocument()
    expect(screen.getByText('It never changes anything in your Withings account.')).toBeInTheDocument()
    expect(screen.getByText(/13 Oct 2026/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Connect Withings' })).toBeInTheDocument()
  })

  it('shows the import per stream and links to the overview', async () => {
    open(
      '/onboarding',
      fakeStatus({ streams: [fakeStream('measures', 'importing', { progress_percent: 40 }), fakeStream('ecg')] }),
    )
    expect(await screen.findByRole('heading', { name: 'Importing your history' })).toBeInTheDocument()
    expect(screen.getByText(/40/)).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Go to the overview' })).toHaveAttribute('href', '/')
  })

  it('says the history is imported once nothing is importing', async () => {
    open('/onboarding', fakeStatus())
    expect(await screen.findByRole('heading', { name: 'Your history is imported' })).toBeInTheDocument()
  })

  it('says why the connect failed after the return from Withings', async () => {
    open('/onboarding?withings=state_rejected', fakeStatus({ connection: 'not_connected', streams: [] }), [])
    expect(await screen.findByText(/expired or was already used/)).toBeInTheDocument()
  })
})
