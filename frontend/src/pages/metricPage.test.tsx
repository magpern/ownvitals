import { act, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { SIGNED_IN, fakeApi, reason } from '../test/fakeApi'
import { FINGERPRINT, syntheticDay, syntheticTrendNotReady, syntheticInsufficient, syntheticMetric, syntheticReading } from '../test/metricFixtures'
import { fakeProfile } from '../test/profileFixtures'
import { renderAt } from '../test/render'

// uPlot needs a canvas, which jsdom has not: the chart is checked through the data it is given.
const made: { data: (number | null)[][] }[] = []
vi.mock('uplot', () => {
  class FakePlot {
    static pxRatio = 1
    constructor(_options: unknown, data: (number | null)[][]) {
      made.push({ data })
    }
    destroy() {}
    setSize() {}
  }
  return { default: FakePlot }
})

beforeEach(() => {
  made.length = 0
})

const SESSION = { 'GET /api/auth/session': SIGNED_IN }

describe('the metric screen', () => {
  it('shows the latest value, the range and the trend of a metric with enough data', async () => {
    fakeApi({ ...SESSION, 'GET /api/metrics/weight?range=90d': syntheticMetric() })
    renderAt('/m/weight')
    expect(await screen.findByRole('heading', { level: 2, name: 'Weight' })).toBeTruthy()
    expect(await screen.findByText(/77\.5/)).toBeTruthy()
  })

  it('shows the progress toward the minimum, never a trend, when data is insufficient', async () => {
    fakeApi({ ...SESSION, 'GET /api/metrics/weight?range=90d': syntheticInsufficient() })
    renderAt('/m/weight')
    expect(await screen.findAllByText(/2 of 14 days with data/)).not.toHaveLength(0)
    expect(screen.queryByText(/a month/)).toBeNull()
    expect(screen.queryByText(/Increasing/)).toBeNull()
    expect(screen.queryByText(/77\.5/)).toBeNull()
  })

  it('shows no trend value while the trend is not ready, even if the API sent a slope', async () => {
    fakeApi({ ...SESSION, 'GET /api/metrics/weight?range=90d': syntheticTrendNotReady() })
    renderAt('/m/weight')
    expect(await screen.findAllByText(/5 of 14 days with data/)).not.toHaveLength(0)
    expect(screen.queryByText(/a month/)).toBeNull()
    expect(screen.queryByText(/Increasing/)).toBeNull()
  })

  it('shows the trend of a ready metric', async () => {
    fakeApi({ ...SESSION, 'GET /api/metrics/weight?range=90d': syntheticMetric() })
    renderAt('/m/weight')
    expect(await screen.findByText(/a month/)).toBeTruthy()
  })

  it('shows only the state of a metric that cannot be read', async () => {
    fakeApi({ ...SESSION, 'GET /api/metrics/weight?range=90d': syntheticMetric({ state: 'not_shared' }) })
    renderAt('/m/weight')
    expect(await screen.findByText(/cannot be read right now/)).toBeTruthy()
    expect(screen.queryByRole('navigation', { name: /range/i })).toBeNull()
  })

  it('leaves an unconfirmed reading out of the chart and lists its day', async () => {
    fakeApi({ ...SESSION, 'GET /api/metrics/weight?range=90d': syntheticMetric() })
    renderAt('/m/weight')
    const section = await screen.findByRole('region', { name: /unconfirmed/i })
    expect(within(section).getByText(/1 unconfirmed reading/)).toBeTruthy()
    await waitFor(() => expect(made.length).toBeGreaterThan(0))
    const values = made.flatMap((m) => m.data.flat())
    expect(values).not.toContain(99.9)
    expect(made.at(-1)?.data.some((series) => series.includes(null))).toBe(true)
  })

  it('asks for the range in the address and links the others', async () => {
    const api = fakeApi({ ...SESSION, 'GET /api/metrics/weight?range=1y': syntheticMetric({ range: '1y' }) })
    renderAt('/m/weight?range=1y')
    await screen.findByRole('heading', { level: 2, name: 'Weight' })
    expect(api.to('GET', '/api/metrics/weight?range=1y')).toHaveLength(1)
    const nav = screen.getByRole('navigation', { name: /range/i })
    expect(within(nav).getByRole('link', { name: /1 ?y/i }).getAttribute('aria-current')).toBe('true')
  })

  it('offers the six ranges, each as a deep link', async () => {
    fakeApi({ ...SESSION, 'GET /api/metrics/weight?range=7d': syntheticMetric({ range: '7d' }) })
    renderAt('/m/weight?range=7d')
    await screen.findByRole('heading', { level: 2, name: 'Weight' })
    const links = within(screen.getByRole('navigation', { name: /range/i })).getAllByRole('link')
    expect(links.map((l) => l.textContent)).toEqual(['7 days', '30 days', '3 months', '6 months', '1 year', 'All'])
    expect(links.map((l) => l.getAttribute('href'))).toEqual(
      ['7d', '30d', '90d', '6mo', '1y', 'all'].map((r) => `/m/weight?range=${r}`),
    )
    expect(links[0]?.getAttribute('aria-current')).toBe('true')
  })

  it('opens on 7 days for a user whose saved range is the new default', async () => {
    const api = fakeApi({
      ...SESSION,
      'GET /api/profile': fakeProfile({ chart_range: '7d' }),
      'GET /api/metrics/weight?range=7d': syntheticMetric({ range: '7d' }),
    })
    renderAt('/m/weight')
    await screen.findByRole('heading', { level: 2, name: 'Weight' })
    expect(api.to('GET', '/api/metrics/weight?range=7d')).toHaveLength(1)
  })

  it('opens on 7 days when the profile cannot be read and the address has no range', async () => {
    const api = fakeApi({
      ...SESSION,
      'GET /api/profile': reason(500, 'failed'),
      'GET /api/metrics/weight?range=7d': syntheticMetric({ range: '7d' }),
    })
    renderAt('/m/weight')
    await screen.findByRole('heading', { level: 2, name: 'Weight' })
    expect(api.to('GET', '/api/metrics/weight?range=7d')).toHaveLength(1)
  })

  it('lets a saved older range win over the default', async () => {
    const api = fakeApi({
      ...SESSION,
      'GET /api/profile': fakeProfile({ chart_range: '6mo' }),
      'GET /api/metrics/weight?range=6mo': syntheticMetric({ range: '6mo' }),
    })
    renderAt('/m/weight')
    await screen.findByRole('heading', { level: 2, name: 'Weight' })
    expect(api.to('GET', '/api/metrics/weight?range=6mo')).toHaveLength(1)
  })
})

describe('the day view and the reading drawer', () => {
  const routes = {
    ...SESSION,
    'GET /api/days/weight/2026-10-02': syntheticDay(),
    'GET /api/readings/r-used': syntheticReading(),
  }

  it('marks an unconfirmed reading and does not count it', async () => {
    fakeApi(routes)
    renderAt('/m/weight/2026-10-02')
    expect(await screen.findByText('Unconfirmed, not used')).toBeTruthy()
    const rows = document.querySelectorAll('tr[data-unconfirmed="true"]')
    expect(rows).toHaveLength(1)
    expect(document.querySelectorAll('tr[data-used="true"]')).toHaveLength(1)
    expect(screen.getByText(/No value for this day/)).toBeTruthy()
  })

  it('opens a reading from its link and closes it with back', async () => {
    fakeApi(routes)
    renderAt('/m/weight/2026-10-02')
    const user = userEvent.setup()
    const open = await screen.findByRole('link', { name: /07:40/ })
    await user.click(open)
    expect(await screen.findByRole('complementary', { name: /reading/i })).toBeTruthy()
    expect(window.location.search).toBe('?reading=r-used')
    act(() => window.history.back())
    await waitFor(() => expect(screen.queryByRole('complementary')).toBeNull())
  })

  it('opens a reading straight from a deep link', async () => {
    fakeApi(routes)
    renderAt('/m/weight/2026-10-02?reading=r-used')
    const drawer = await screen.findByRole('complementary', { name: /reading/i })
    expect(await within(drawer).findByText(/78\.20/)).toBeTruthy()
    expect(within(drawer).getByText(`${FINGERPRINT.slice(0, 8)}…${FINGERPRINT.slice(-8)}`)).toBeTruthy()
  })
})
