import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { SIGNED_IN, fakeApi } from '../../test/fakeApi'
import { renderAt } from '../../test/render'

// The doctor report row in Settings → Your data (issue 210). Synthetic: made-up ids and a metric
// with one made-up reading; no network, no account, no health data.

const TILE = {
  key: 'weight',
  area: 'body',
  unit: 'kg',
  stream: 'scale',
  state: 'ready',
  progress_percent: null,
  progress: null,
  latest: { day: '2026-11-01', value: 70, n: 1 },
  typical_range: null,
  sparkline: [],
}

const item = (state: string) => ({
  id: 'synthetic-doctor-1',
  state,
  created_at: '2026-11-09T10:00:00Z',
  ready_at: null,
  expires_at: state === 'ready' ? '2026-11-10T10:00:00Z' : null,
  size: null,
  failure: null,
  largest: null,
  attempts_left: 3,
})

const base = {
  'GET /api/auth/session': SIGNED_IN,
  'GET /api/metrics?range=1y': { metrics: [TILE] },
  'GET /api/ecg?offset=0': { recordings: [], next_offset: null },
}

describe('the doctor report row', () => {
  it('states the typical-range and no-estimates note and needs a metric first', async () => {
    fakeApi({ ...base, 'GET /api/reports/doctor': { exports: [] } })
    renderAt('/settings/data')
    expect(await screen.findByText(/not a clinical reference range/)).toBeTruthy()
    expect(screen.getByRole('button', { name: /Create report…/ })).toBeDisabled()
  })

  it('sends only the period, metric keys and recording ids once a metric is chosen', async () => {
    const api = fakeApi({
      ...base,
      'GET /api/reports/doctor': { exports: [] },
      'POST /api/reports/doctor': () => ({ export: item('building') }),
    })
    renderAt('/settings/data')
    fireEvent.click(await screen.findByRole('button', { name: 'Weight' }))
    const button = screen.getByRole('button', { name: /Create report…/ })
    await waitFor(() => {
      expect(button).toBeEnabled()
    })
    fireEvent.click(button)
    await waitFor(() => {
      expect(api.to('POST', '/api/reports/doctor')).toHaveLength(1)
    })
    const sent = JSON.stringify(api.to('POST', '/api/reports/doctor')[0])
    expect(sent).toContain('"metrics":["weight"]')
    expect(sent).toContain('"recordings":[]')
  })

  it('does not offer the segmental tiles, which the report cannot take', async () => {
    const segment = { ...TILE, key: 'segmental_muscle_mass:left_arm', unit: 'kg' }
    fakeApi({
      ...base,
      'GET /api/metrics?range=1y': { metrics: [TILE, segment] },
      'GET /api/reports/doctor': { exports: [] },
    })
    renderAt('/settings/data')
    await screen.findByRole('button', { name: 'Weight' })
    const group = screen.getByRole('group', { name: 'Metrics' })
    expect(within(group).getAllByRole('button')).toHaveLength(1)
  })

  it('says it is preparing while a report builds', async () => {
    fakeApi({ ...base, 'GET /api/reports/doctor': { exports: [item('building')] } })
    renderAt('/settings/data')
    expect(await screen.findByText(/Preparing your report/)).toBeTruthy()
  })

  it('offers the download of a ready report', async () => {
    fakeApi({ ...base, 'GET /api/reports/doctor': { exports: [item('ready')] } })
    renderAt('/settings/data')
    expect(await screen.findByText(/Your report is ready/)).toBeTruthy()
    expect(screen.getAllByRole('button', { name: 'Download' }).length).toBeGreaterThan(0)
  })
})
