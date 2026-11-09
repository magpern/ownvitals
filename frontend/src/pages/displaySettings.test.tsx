import { fireEvent, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { SIGNED_IN, fakeApi } from '../test/fakeApi'
import { syntheticMetric } from '../test/metricFixtures'
import { fakeFindings, fakeMetric, fakeMetrics, fakeStatus } from '../test/overviewFixtures'
import { fakeProfile } from '../test/profileFixtures'
import { renderAt } from '../test/render'
import { setThemePreference } from '../theme/theme'

// The saved display settings in the screens beside Settings (issue 305). Synthetic answers only.

vi.mock('uplot', () => {
  class FakePlot {
    static pxRatio = 1
    destroy() {}
    setSize() {}
  }
  return { default: FakePlot }
})

afterEach(() => {
  setThemePreference('system')
  window.localStorage.clear()
  vi.unstubAllGlobals()
})

const SESSION = { 'GET /api/auth/session': SIGNED_IN }
const OVERVIEW = {
  'GET /api/status': fakeStatus(),
  'GET /api/findings?tab=live&limit=20': fakeFindings(),
  'GET /api/metrics': fakeMetrics([fakeMetric('weight')]),
}

describe('the saved units and date format', () => {
  it('shows a weight in pounds when that is the saved unit', async () => {
    fakeApi({ ...SESSION, 'GET /api/profile': fakeProfile({ weight_unit: 'lb' }), 'GET /api/metrics/weight?range=90d': syntheticMetric() })
    renderAt('/m/weight')
    // 78.3 kg is 172.6 lb; the unit label follows.
    expect((await screen.findAllByText('172.6')).length).toBeGreaterThan(0)
    expect(screen.queryByText('78.3')).not.toBeInTheDocument()
  })

  it('shows a weight in stone, and keeps kilograms when that is saved', async () => {
    fakeApi({ ...SESSION, 'GET /api/profile': fakeProfile({ weight_unit: 'st' }), 'GET /api/metrics/weight?range=90d': syntheticMetric() })
    renderAt('/m/weight')
    expect((await screen.findAllByText('12.3')).length).toBeGreaterThan(0)
  })

  it('writes days in the saved date format', async () => {
    fakeApi({ ...SESSION, 'GET /api/profile': fakeProfile({ datetime_format: 'iso' }), 'GET /api/metrics/weight?range=90d': syntheticMetric() })
    renderAt('/m/weight')
    expect(await screen.findByText(/2026-10-03 · 1 reading/)).toBeInTheDocument()
  })

  it('shows the overview tile in pounds too', async () => {
    fakeApi({ ...SESSION, ...OVERVIEW, 'GET /api/profile': fakeProfile({ weight_unit: 'lb' }) })
    renderAt('/')
    expect(await screen.findByText('lb')).toBeInTheDocument()
  })
})

describe('the saved range', () => {
  it('opens a metric on the saved range, and an address that names one wins', async () => {
    const api = fakeApi({
      ...SESSION,
      'GET /api/profile': fakeProfile({ chart_range: '1y' }),
      'GET /api/metrics/weight?range=1y': syntheticMetric({ range: '1y' }),
      'GET /api/metrics/weight?range=30d': syntheticMetric({ range: '30d' }),
    })
    renderAt('/m/weight')
    await screen.findByText('Latest')
    await waitFor(() => expect(api.calls.map((c) => c.path)).toContain('/api/metrics/weight?range=1y'))
    expect(api.calls.map((c) => c.path)).not.toContain('/api/metrics/weight?range=90d')
  })

  it('passes the chosen range on to /api/metrics from the overview', async () => {
    const api = fakeApi({ ...SESSION, ...OVERVIEW, 'GET /api/profile': fakeProfile({ chart_range: '1y' }) })
    renderAt('/?range=30d')
    await screen.findByRole('link', { name: 'Weight' })
    expect(api.calls.map((c) => c.path)).toContain('/api/metrics?range=30d')
  })

  it('starts the overview on the saved range when the address has none', async () => {
    const api = fakeApi({ ...SESSION, ...OVERVIEW, 'GET /api/profile': fakeProfile({ chart_range: '1y' }) })
    renderAt('/')
    await waitFor(() => expect(api.calls.map((c) => c.path)).toContain('/api/metrics?range=1y'))
  })
})

describe('an out-of-order profile answer', () => {
  it('does not bring an old theme back after a newer save', async () => {
    let stored = fakeProfile({ theme: 'light' })
    const api = fakeApi({
      ...SESSION,
      'GET /api/profile': () => stored,
      'PUT /api/profile': (body: unknown) => {
        stored = { ...stored, ...(body as object) }
        return stored
      },
    })
    // The second read of the profile (the shell's) answers late, with what was stored before the save.
    const real = globalThis.fetch
    let reads = 0
    let release: () => void = () => undefined
    const gate = new Promise<void>((resolve) => {
      release = resolve
    })
    vi.stubGlobal('fetch', (input: RequestInfo | URL, init?: RequestInit) => {
      const answer = real(input, init)
      const isRead = input === '/api/profile' && (init?.method ?? 'GET') === 'GET'
      if (isRead && ++reads === 2) return gate.then(() => answer)
      return answer
    })
    renderAt('/settings/profile')
    fireEvent.click(await screen.findByRole('button', { name: 'Dark' }))
    await waitFor(() => expect(api.to('PUT', '/api/profile')).toHaveLength(1))
    await waitFor(() => expect(document.documentElement.getAttribute('data-theme')).toBe('dark'))
    expect(reads).toBe(2)
    release()
    await new Promise((resolve) => setTimeout(resolve, 20))
    expect(document.documentElement.getAttribute('data-theme')).toBe('dark')
  })
})
