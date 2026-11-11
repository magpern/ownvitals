import { screen, within } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { SIGNED_IN, fakeApi } from '../test/fakeApi'
import { fakeFinding, fakeFindings, fakeMetric, fakeMetrics, fakeStatus, NO_VALUE } from '../test/overviewFixtures'
import { renderAt } from '../test/render'

// The nerve health page against synthetic API answers (synthetic: true): no network, no real
// account, no real health data.

vi.mock('uplot', () => {
  class FakePlot {
    static pxRatio = 1
    destroy() {}
    setSize() {}
  }
  return { default: FakePlot }
})

const FINDINGS = 'GET /api/findings?tab=live&area=nerve&offset=0'

const nerveMetric = (key: string, more: Record<string, unknown> = {}) =>
  fakeMetric(key, { area: 'nerve', unit: 'score', latest: { day: '2026-11-01', value: 71.6, n: 1, method: 'last' }, ...more })

const monthRow = (key: string, days: number, median: number | null, minimum: number | null, maximum: number | null) => ({
  metric_key: key,
  days_with_data: days,
  median,
  minimum,
  maximum,
})

const summary = (months: unknown[]) => ({ as_of: '2026-11-01T12:00:00Z', metrics: [], months })

function open(metrics: unknown[], months: unknown[] = [], findings: object = fakeFindings()) {
  fakeApi({
    'GET /api/auth/session': SIGNED_IN,
    'GET /api/metrics': fakeMetrics(metrics),
    'GET /api/status': fakeStatus(),
    'GET /api/nerve/summary': summary(months),
    [FINDINGS]: findings,
  })
  return renderAt('/nerve')
}

describe('the nerve health page', () => {
  it('shows the three tiles as whole numbers, linked to the metric detail', async () => {
    open([nerveMetric('nerve_health_score'), nerveMetric('nerve_response_score'), nerveMetric('esc', { unit: 'µS' })])
    const link = await screen.findByRole('link', { name: 'Nerve health score' })
    expect(link.getAttribute('href')).toBe('/m/nerve_health_score?range=7d')
    const card = link.closest('.ov-card') as HTMLElement
    expect(within(card).getByText('72')).toBeTruthy()
    expect(screen.getByRole('link', { name: 'Nerve response score' })).toBeTruthy()
    expect(screen.getByRole('heading', { name: 'Nerve measures' })).toBeTruthy()
  })

  it('shows insufficient data with its progress (N1)', async () => {
    open([
      nerveMetric('nerve_health_score', {
        state: 'insufficient_data',
        progress: { days_with_data: 3, days_required: 4 },
        ...NO_VALUE,
      }),
    ])
    const card = (await screen.findByRole('link', { name: 'Nerve health score' })).closest('.ov-card') as HTMLElement
    expect(within(card).getByText('Insufficient data')).toBeTruthy()
    expect(within(card).queryByText('72')).toBeNull()
  })

  it('shows a metric the API left out as not available, and the foot card as not supported', async () => {
    open([nerveMetric('nerve_health_score')])
    const title = await screen.findByRole('heading', { name: 'Electrochemical skin conductance' })
    expect(within(title.closest('.ov-card') as HTMLElement).getByText('Not available')).toBeTruthy()
    const foot = screen.getByRole('heading', { name: 'Left and right foot' })
    const card = foot.closest('.ov-card') as HTMLElement
    expect(within(card).getByText('Not supported')).toBeTruthy()
    expect(within(card).getByText(/doesn't offer this/)).toBeTruthy()
  })

  it('shows no ESC literature band', async () => {
    open([nerveMetric('esc', { unit: 'µS' })])
    await screen.findByRole('link', { name: 'Electrochemical skin conductance' })
    expect(screen.queryByText(/literature|reference range|< ?40|40.60/i)).toBeNull()
  })

  it('shows the months newest first, whole numbers, and "No readings" for a month without data', async () => {
    open(
      [nerveMetric('nerve_health_score')],
      [
        { month: '2026-11', metrics: [monthRow('nerve_health_score', 5, 71.6, 64.2, 80.4), monthRow('esc', 0, null, null, null)], findings: [] },
        { month: '2026-10', metrics: [], findings: [] },
      ],
    )
    const list = await screen.findByRole('list', { name: 'Monthly summary' })
    const items = within(list).getAllByRole('listitem')
    expect(items[0]?.getAttribute('data-month')).toBe('2026-11')
    expect(within(items[0] as HTMLElement).getByText(/median 72, lowest 64, highest 80, 5 days with data/)).toBeTruthy()
    expect(within(items[0] as HTMLElement).queryByText(/Electrochemical/)).toBeNull()
    expect(within(items[1] as HTMLElement).getByText('No readings')).toBeTruthy()
  })

  it('lists N2, N3 and N5 findings and says so when there are none', async () => {
    const { unmount } = open(
      [nerveMetric('nerve_health_score')],
      [],
      fakeFindings([
        fakeFinding({ rule_id: 'N5', area: 'nerve' }),
        fakeFinding({ id: 'g', rule_id: 'N2:guided', area: 'nerve' }),
        fakeFinding({ id: 'x', rule_id: 'B2', area: 'body' }),
      ]),
    )
    const list = await screen.findByRole('list', { name: 'Nerve findings' })
    expect(within(list).getByText('Foot nerve difference between sides')).toBeTruthy()
    expect(within(list).getByText('Nerve health reading below your usual range')).toBeTruthy()
    expect(within(list).queryByText('Weight falling over six months')).toBeNull()
    unmount()
    open([nerveMetric('nerve_health_score')])
    expect(await screen.findByText('No nerve findings right now.')).toBeTruthy()
  })
})
