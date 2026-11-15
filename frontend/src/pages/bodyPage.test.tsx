import { screen, within } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { SIGNED_IN, fakeApi } from '../test/fakeApi'
import { fakeFinding, fakeFindings, fakeMetric, fakeMetrics, fakeStatus, NO_VALUE } from '../test/overviewFixtures'
import { renderAt } from '../test/render'

// The body composition page against synthetic API answers (synthetic: true): no network, no real
// account, no real health data.

vi.mock('uplot', () => {
  class FakePlot {
    static pxRatio = 1
    destroy() {}
    setSize() {}
  }
  return { default: FakePlot }
})

const FINDINGS = 'GET /api/findings?tab=live&area=body&offset=0'

function open(metrics: unknown[], findings: object = fakeFindings()) {
  fakeApi({
    'GET /api/auth/session': SIGNED_IN,
    'GET /api/metrics': fakeMetrics(metrics),
    'GET /api/status': fakeStatus(),
    [FINDINGS]: findings,
  })
  return renderAt('/body')
}

const segment = (position: string, value: number, more: Record<string, unknown> = {}) =>
  fakeMetric(`segmental_muscle_mass:${position}`, {
    latest: { day: '2026-11-01', value, n: 1, method: 'last' },
    typical_range: null,
    sparkline: [],
    ...more,
  })

describe('the body composition page', () => {
  it('shows the tiles with their values, linked to the metric detail', async () => {
    open([fakeMetric('weight'), fakeMetric('fat_ratio', { unit: '%' })])
    const link = await screen.findByRole('link', { name: 'Weight' })
    expect(link.getAttribute('href')).toBe('/m/weight?range=90d')
    expect(screen.getByRole('link', { name: 'Fat ratio' })).toBeTruthy()
    expect(screen.getByRole('heading', { name: 'Body composition' })).toBeTruthy()
  })

  it('shows a metric the API left out as not available, never empty', async () => {
    open([fakeMetric('weight')])
    const title = await screen.findByRole('heading', { name: 'Bone mass' })
    const card = title.closest('.ov-card') as HTMLElement
    expect(within(card).getByText('Not available')).toBeTruthy()
    expect(within(card).getByRole('link', { name: 'Bone mass' }).getAttribute('href')).toBe('/m/bone_mass?range=90d')
    expect(screen.getByRole('heading', { name: 'Metabolic age' })).toBeTruthy()
  })

  it('shows a metric the device cannot measure as not supported with the reason', async () => {
    open([fakeMetric('weight'), fakeMetric('extracellular_water', { state: 'not_supported', ...NO_VALUE })])
    const title = await screen.findByRole('link', { name: 'Extracellular water' })
    const card = title.closest('.ov-card') as HTMLElement
    expect(within(card).getByText('Not supported')).toBeTruthy()
    expect(within(card).getByText(/doesn't offer this/)).toBeTruthy()
  })

  it('renders the other data states of a tile', async () => {
    open([
      fakeMetric('weight', { state: 'stale' }),
      fakeMetric('muscle_mass', { state: 'importing', progress_percent: 40, ...NO_VALUE }),
      fakeMetric('bone_mass', { state: 'insufficient_data', progress: { days_with_data: 3, days_required: 14 }, ...NO_VALUE }),
      fakeMetric('fat_mass', { state: 'not_shared', ...NO_VALUE }),
    ])
    expect(await screen.findByText('Stale')).toBeTruthy()
    expect(screen.getByText('Importing 40%')).toBeTruthy()
    expect(screen.getByText('Insufficient data')).toBeTruthy()
    expect(screen.getByText('Not shared')).toBeTruthy()
  })

  it('shows the segmental values and how far left and right differ', async () => {
    open([
      segment('left_arm', 3.3),
      segment('right_arm', 2.7),
      segment('left_leg', 9.0, { state: 'stale' }),
      segment('right_leg', 9.0),
    ])
    const list = await screen.findByRole('list', { name: 'Muscle mass by arm and leg' })
    expect(within(list).getByText('3.3')).toBeTruthy()
    expect(screen.getByText('Left and right arms differ by 20.0 %.')).toBeTruthy()
    expect(screen.getByText('Left and right legs differ by 0.0 %.')).toBeTruthy()
  })

  it('shows segmental rows as not supported or not available, never a number', async () => {
    open([segment('left_arm', 0, { state: 'not_supported', ...NO_VALUE })])
    const list = await screen.findByRole('list', { name: 'Muscle mass by arm and leg' })
    expect(within(list).getByText('Not supported')).toBeTruthy()
    expect(within(list).getAllByText('Not available')).toHaveLength(3)
    expect(screen.getAllByText(/not enough values to compare/)).toHaveLength(2)
  })

  it('lists body findings and says so when there are none', async () => {
    const { unmount } = open([fakeMetric('weight')], fakeFindings([fakeFinding({ rule_id: 'B2', tier: 'trend' }), fakeFinding({ id: 'x', rule_id: 'C1', area: 'heart' })]))
    const list = await screen.findByRole('list', { name: 'Body findings' })
    expect(within(list).getByText('Weight falling over six months')).toBeTruthy()
    expect(within(list).queryByText(/Resting pulse/)).toBeNull()
    unmount()
    open([fakeMetric('weight')])
    expect(await screen.findByText('No body findings right now.')).toBeTruthy()
  })

  it('shows no experimental rule unless the API sent it', async () => {
    open([fakeMetric('weight')], fakeFindings([fakeFinding({ rule_id: 'B6', tier: 'trend' })]))
    const list = await screen.findByRole('list', { name: 'Body findings' })
    expect(within(list).getByText('Arm or leg muscle imbalance')).toBeTruthy()
    expect(within(list).queryByText('Weight gain over three days')).toBeNull()
    expect(within(list).queryByText('Extracellular water ratio')).toBeNull()
  })

  it('filters the body findings to rules B1 to B6 whatever the API sends (issue 325)', async () => {
    const stray = [
      fakeFinding({ id: 'a', rule_id: 'B7', tier: 'experimental' }),
      fakeFinding({ id: 'b', rule_id: 'E4', area: 'body', tier: 'device' }),
      fakeFinding({ id: 'c', rule_id: 'B2', tier: 'trend' }),
    ]
    open([fakeMetric('weight')], fakeFindings(stray))
    const list = await screen.findByRole('list', { name: 'Body findings' })
    expect(within(list).getAllByRole('listitem')).toHaveLength(1)
    expect(within(list).getByText('Weight falling over six months')).toBeTruthy()
    expect(within(list).queryByText(/ECG interval/)).toBeNull()
  })
})
