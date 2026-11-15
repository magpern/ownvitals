import { fireEvent, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { SIGNED_IN, fakeApi, reason } from '../../test/fakeApi'
import { renderAt } from '../../test/render'

// "Export everything" in Settings → Your data (issue 211). Synthetic: made-up export ids, sizes
// and a few bytes standing in for the archive; no network, no account, no health data.

function item(state: string, extra: Record<string, unknown> = {}) {
  return {
    id: 'synthetic-export-1',
    state,
    created_at: '2026-11-09T10:00:00Z',
    ready_at: state === 'ready' ? '2026-11-09T10:01:00Z' : null,
    expires_at: state === 'ready' ? '2026-11-10T10:01:00Z' : null,
    size: state === 'ready' ? 2_500_000 : null,
    failure: null,
    largest: null,
    attempts_left: 3,
    ...extra,
  }
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('the export row', () => {
  it('requests an export through the step-up dialog and shows it preparing', async () => {
    let exports: unknown[] = []
    let asked = 0
    const api = fakeApi({
      'GET /api/auth/session': SIGNED_IN,
      'GET /api/exports': () => ({ exports }),
      'POST /api/exports': () => {
        asked += 1
        if (asked === 1) return reason(403, 'step_up_required')
        exports = [item('building')]
        return { export: exports[0] }
      },
      'POST /api/auth/stepup': { ok: true },
    })
    renderAt('/settings/data')
    const button = await screen.findByRole('button', { name: /Export…/ })
    fireEvent.click(button)
    // The shared dialog opens for the code; the action is not repeated until it is confirmed.
    expect(await screen.findByRole('dialog')).toBeTruthy()
    expect(api.to('POST', '/api/exports')).toHaveLength(1)
  })

  it('says it is preparing while an export builds', async () => {
    fakeApi({ 'GET /api/auth/session': SIGNED_IN, 'GET /api/exports': { exports: [item('building')] } })
    renderAt('/settings/data')
    expect(await screen.findByText(/Preparing your export/)).toBeTruthy()
    expect(screen.getByRole('button', { name: /Export…/ })).toBeDisabled()
  })

  it('downloads a ready export as a same-origin fetch of its id only', async () => {
    let exports = [item('ready')]
    const created = vi.fn(() => 'blob:synthetic')
    const revoked = vi.fn()
    vi.stubGlobal('URL', Object.assign(URL, { createObjectURL: created, revokeObjectURL: revoked }))
    const api = fakeApi({
      'GET /api/auth/session': SIGNED_IN,
      'GET /api/exports': () => ({ exports }),
      'GET /api/exports/synthetic-export-1/file': () => {
        exports = [item('expired')]
        return new Response('PK synthetic', {
          status: 200,
          headers: {
            'Content-Type': 'application/zip',
            'Content-Disposition': 'attachment; filename="ownvitals-export-2026-11-09.zip"',
          },
        })
      },
    })
    renderAt('/settings/data')
    expect(await screen.findByText(/Your export is ready \(2\.5 MB\)/)).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: /Download/ }))
    await waitFor(() => {
      expect(created).toHaveBeenCalled()
    })
    // The blob stays readable while the browser saves it; it is revoked a minute later.
    expect(revoked).not.toHaveBeenCalled()
    expect(await screen.findByText(/Downloaded\. The file is now deleted/)).toBeTruthy()
    const call = api.to('GET', '/api/exports/synthetic-export-1/file')[0]
    expect(call).toBeDefined()
    expect(call?.path).toBe('/api/exports/synthetic-export-1/file')
    expect(await screen.findByRole('button', { name: /Export…/ })).toBeTruthy()
  })

  it('saves nothing when the body is shorter than its Content-Length', async () => {
    const created = vi.fn(() => 'blob:synthetic')
    vi.stubGlobal('URL', Object.assign(URL, { createObjectURL: created, revokeObjectURL: vi.fn() }))
    fakeApi({
      'GET /api/auth/session': SIGNED_IN,
      'GET /api/exports': { exports: [item('ready')] },
      'GET /api/exports/synthetic-export-1/file': () =>
        new Response('PK synth', {
          status: 200,
          headers: { 'Content-Type': 'application/zip', 'Content-Length': '2500000' },
        }),
    })
    renderAt('/settings/data')
    fireEvent.click(await screen.findByRole('button', { name: /Download/ }))
    expect(await screen.findByText(/The server could not be reached/)).toBeTruthy()
    expect(created).not.toHaveBeenCalled()
  })

  it('saves a compressed body whose Content-Length counts the encoded bytes', async () => {
    const created = vi.fn(() => 'blob:synthetic')
    vi.stubGlobal('URL', Object.assign(URL, { createObjectURL: created, revokeObjectURL: vi.fn() }))
    fakeApi({
      'GET /api/auth/session': SIGNED_IN,
      'GET /api/exports': { exports: [item('ready')] },
      'GET /api/exports/synthetic-export-1/file': () =>
        new Response('PK synthetic, decoded', {
          status: 200,
          headers: {
            'Content-Type': 'application/zip',
            'Content-Encoding': 'gzip',
            'Content-Length': '12',
          },
        }),
    })
    renderAt('/settings/data')
    fireEvent.click(await screen.findByRole('button', { name: /Download/ }))
    await waitFor(() => {
      expect(created).toHaveBeenCalled()
    })
  })

  it('names the largest part when the export was too large', async () => {
    fakeApi({
      'GET /api/auth/session': SIGNED_IN,
      'GET /api/exports': {
        exports: [item('failed', { failure: 'export_too_large', largest: 'ecg_signal' })],
      },
    })
    renderAt('/settings/data')
    expect(await screen.findByText(/The largest part was ecg_signal/)).toBeTruthy()
    expect(screen.getByRole('button', { name: /Export…/ })).not.toBeDisabled()
  })

  it('words a refused download from its code', async () => {
    fakeApi({
      'GET /api/auth/session': SIGNED_IN,
      'GET /api/exports': { exports: [item('ready')] },
      'GET /api/exports/synthetic-export-1/file': reason(410, 'export_expired'),
    })
    renderAt('/settings/data')
    fireEvent.click(await screen.findByRole('button', { name: /Download/ }))
    expect(await screen.findByText(/This export has expired or was already downloaded/)).toBeTruthy()
  })
})
