import { screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import { SIGNED_IN, fakeApi } from '../test/fakeApi'
import { BACKFILL_TREND, EXPERIMENTAL, LIVE_DEVICE, LIVE_NOT_EVALUATED, LIVE_TREND, detail, page } from '../test/findingsFixtures'
import { syntheticMetric } from '../test/metricFixtures'
import { renderAt } from '../test/render'

// uPlot needs a canvas, which jsdom has not.
vi.mock('uplot', () => {
  class FakePlot {
    static pxRatio = 1
    destroy() {}
    setSize() {}
  }
  return { default: FakePlot }
})

const METRIC = { 'GET /api/metrics/weight?range=90d': syntheticMetric() }
const routes = (extra: Record<string, unknown> = {}) => ({
  'GET /api/auth/session': SIGNED_IN,
  ...METRIC,
  'GET /api/findings?tab=live&offset=0': page([LIVE_TREND, LIVE_DEVICE, LIVE_NOT_EVALUATED]),
  'GET /api/findings/f-trend': detail(LIVE_TREND, { window_days: 3, gain_kg: 2.1 }),
  'GET /api/findings/f-device': detail(LIVE_DEVICE, { count: 2, total: 5 }),
  'GET /api/findings/f-e4': detail(LIVE_NOT_EVALUATED),
  ...extra,
})

describe('the findings screen', () => {
  it('lists live findings with tier marks and shows the first one in full', async () => {
    fakeApi(routes())
    renderAt('/findings')
    const list = await screen.findByRole('list', { name: 'Findings' })
    expect(within(list).getByText('Weight gain over three days')).toBeTruthy()
    expect(within(list).getByText('Trend')).toBeTruthy()
    expect(await screen.findByText('Why you’re seeing this')).toBeTruthy()
    expect(screen.getByText('What it isn’t')).toBeTruthy()
    expect(screen.getByRole('link', { name: 'Open Weight' })).toBeTruthy()
    expect(screen.getByRole('link', { name: /Readings on/ })).toBeTruthy()
  })

  it('attributes a device finding to Withings and marks an unevaluated one', async () => {
    fakeApi(routes())
    renderAt('/findings')
    const list = await screen.findByRole('list', { name: 'Findings' })
    expect(within(list).getAllByText(/Withings’ own results/)).not.toHaveLength(0)
    expect(within(list).getByText(/Not evaluated by ownvitals/)).toBeTruthy()
  })

  it('keeps backfill findings under History only, even if a live list holds one', async () => {
    fakeApi(
      routes({
        'GET /api/findings?tab=live&offset=0': page([LIVE_TREND, BACKFILL_TREND]),
        'GET /api/findings?tab=history&offset=0': page([BACKFILL_TREND]),
        'GET /api/findings/f-old': detail(BACKFILL_TREND, { window_days: 170 }),
        'GET /api/metrics/weight?range=1y': syntheticMetric(),
      }),
    )
    renderAt('/findings')
    const list = await screen.findByRole('list', { name: 'Findings' })
    expect(within(list).queryByText('Weight falling over six months')).toBeNull()
    await userEvent.click(screen.getByRole('link', { name: 'History' }))
    const history = await screen.findByText('Weight falling over six months', { selector: '.ov-fcard__name' })
    expect(history).toBeTruthy()
    expect(window.location.search).toContain('tab=history')
    expect(screen.getAllByText('From history').length).toBeGreaterThan(0)
  })

  it('puts a tier filter in the address and reads it back on a deep link', async () => {
    fakeApi(
      routes({
        'GET /api/findings?tab=live&tier=device&offset=0': page([LIVE_DEVICE]),
      }),
    )
    renderAt('/findings')
    await screen.findByRole('list', { name: 'Findings' })
    await userEvent.click(within(screen.getByRole('group', { name: 'Tier' })).getByRole('button', { name: 'Device' }))
    expect(window.location.search).toContain('tier=device')
    const list = await screen.findByRole('list', { name: 'Findings' })
    expect(await within(list).findByText('Several recent ECGs inconclusive or noisy')).toBeTruthy()
    expect(within(list).queryByText('Weight gain over three days')).toBeNull()
  })

  it('opens a deep link with the tier and the chosen finding', async () => {
    fakeApi(
      routes({
        'GET /api/findings?tab=live&tier=device&offset=0': page([LIVE_DEVICE, LIVE_NOT_EVALUATED]),
      }),
    )
    renderAt('/findings?tier=device&id=f-e4')
    expect(await screen.findByText(/the signal quality check did not pass/)).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Device' }).getAttribute('aria-pressed')).toBe('true')
  })

  it('shows an experimental finding only when the API sends it', async () => {
    fakeApi(routes())
    renderAt('/findings')
    await screen.findByRole('list', { name: 'Findings' })
    expect(screen.queryByText('Only you see this')).toBeNull()
  })

  it('shows an experimental finding the API sends, marked as only yours', async () => {
    fakeApi(
      routes({
        'GET /api/findings?tab=live&offset=0': page([EXPERIMENTAL]),
        'GET /api/findings/f-exp': detail(EXPERIMENTAL, { window_days: 7 }),
      }),
    )
    renderAt('/findings')
    expect(await screen.findByText('Only you see this')).toBeTruthy()
  })

  it('says so when there is nothing to look at', async () => {
    fakeApi(routes({ 'GET /api/findings?tab=live&offset=0': page([]) }))
    renderAt('/findings')
    expect(await screen.findByText(/No live findings/)).toBeTruthy()
  })
})
