import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { SIGNED_IN, fakeApi, reason } from '../test/fakeApi'
import {
  NO_VALUE,
  fakeFinding,
  fakeFindings,
  fakeMetric,
  fakeMetrics,
  fakeStatus,
  fakeStream,
} from '../test/overviewFixtures'
import { fakeProfile } from '../test/profileFixtures'
import { renderAt } from '../test/render'

// The overview, the area pages and the freshness line, end to end against synthetic API answers
// (test/overviewFixtures.ts): no network, no real account.

// A synthetic `his` entry of GET /api/scores (not a real person's score).
function fakeScore(o: { status?: string; value?: number | null; reason?: string }) {
  return {
    kind: 'his',
    day: '2026-11-01',
    status: o.status ?? 'computed',
    value: o.value === undefined ? 71 : o.value,
    tier: 'experimental',
    stale: false,
    algo_version: 'his/1',
    how_key: 'score.his.how',
    components: {
      synthetic: true,
      ...(o.reason === undefined ? {} : { reason: o.reason }),
      verticals: { body: 78, heart: 70, activity: 64, sleep: null },
    },
  }
}

const FINDINGS = 'GET /api/findings?tab=live&limit=20'

function open(
  url: string,
  more: { metrics?: unknown[]; status?: unknown; findings?: unknown; profile?: object; scores?: unknown[] } = {},
) {
  fakeApi({
    'GET /api/auth/session': SIGNED_IN,
    'GET /api/metrics': fakeMetrics(more.metrics ?? [fakeMetric('weight')]),
    'GET /api/status': more.status ?? fakeStatus(),
    [FINDINGS]: more.findings ?? fakeFindings(),
    'GET /api/scores': { scores: more.scores ?? [] },
    ...(more.profile === undefined ? {} : { 'GET /api/profile': more.profile }),
  })
  return renderAt(url)
}

const tile = async (name: string) => {
  const title = await screen.findByRole('link', { name })
  return title.closest('.ov-card') as HTMLElement
}

describe('the overview: tiles', () => {
  it('shows a value, the typical range and the sparkline for an up-to-date metric', async () => {
    open('/overview', { metrics: [fakeMetric('weight')] })
    const card = await tile('Weight')
    expect(within(card).getByText(/78\.4/, { selector: '.ov-sr' })).toBeInTheDocument()
    expect(within(card).getAllByText(/77\.6/).length).toBeGreaterThan(0)
    expect(card.querySelector('.ov-tile__spark')).not.toBeNull()
    // The phone's range bar is in the markup; the stylesheet decides which one shows.
    expect(card.querySelector('.ov-tile__bar')).not.toBeNull()
  })

  it('writes blood pressure as one value only when both numbers are there', async () => {
    open('/overview', {
      metrics: [
        fakeMetric('systolic_bp', { area: 'heart', unit: 'mmHg', latest: { day: '2026-11-01', value: 118, n: 1, method: 'last' } }),
        fakeMetric('diastolic_bp', { area: 'heart', unit: 'mmHg', latest: { day: '2026-11-01', value: 76, n: 1, method: 'last' } }),
      ],
    })
    expect(await screen.findByText(/118\/76/)).toBeInTheDocument()
  })

  it('shows no number for blood pressure when the diastolic value is missing', async () => {
    open('/overview', {
      metrics: [
        fakeMetric('systolic_bp', { area: 'heart', unit: 'mmHg', latest: { day: '2026-11-01', value: 118, n: 1, method: 'last' } }),
        fakeMetric('diastolic_bp', { area: 'heart', unit: 'mmHg', ...NO_VALUE }),
      ],
    })
    await screen.findByText('Blood pressure')
    expect(screen.queryByText(/118/)).not.toBeInTheDocument()
  })

  it('shows the progress and no number for insufficient data', async () => {
    open('/overview', {
      metrics: [
        fakeMetric('fat_ratio', {
          state: 'insufficient_data',
          progress_percent: 40,
          progress: { days_with_data: 4, days_required: 10 },
          ...NO_VALUE,
        }),
      ],
    })
    const card = await tile('Fat ratio')
    expect(within(card).getByText('Insufficient data')).toBeInTheDocument()
    expect(within(card).getByText(/4 of 10 days/)).toBeInTheDocument()
    expect(card.textContent).not.toMatch(/\d+\.\d/)
  })

  it('shows the percentage while the history imports, and an incomplete import', async () => {
    open('/overview', {
      metrics: [
        fakeMetric('weight', { state: 'importing', progress_percent: 35, ...NO_VALUE }),
        fakeMetric('muscle_mass', { state: 'incomplete', ...NO_VALUE }),
      ],
    })
    const importing = await tile('Weight')
    expect(within(importing).getByText('Importing 35%')).toBeInTheDocument()
    const incomplete = await tile('Muscle mass')
    expect(within(incomplete).getByText('Incomplete import')).toBeInTheDocument()
  })

  it.each([
    ['not_supported', 'Not supported', /doesn't offer this/],
    ['not_shared', 'Not shared', /haven't allowed/],
    ['not_available', 'Not available', /no data of this kind/],
    ['needs_reconnect', 'Needs reconnect', /has to be renewed/],
    ['withings_plus_needed', 'Withings+ needed', /only with Withings\+/],
  ])('shows %s as a state with its explanation and no number', async (state, label, detail) => {
    open('/overview', { metrics: [fakeMetric('visceral_fat', { state, ...NO_VALUE })] })
    const card = await tile('Visceral fat')
    expect(within(card).getByText(label)).toBeInTheDocument()
    expect(within(card).getByText(detail)).toBeInTheDocument()
    expect(card.textContent).not.toMatch(/\d/)
  })

  it('shows the stale state without hiding the last value', async () => {
    open('/overview', { metrics: [fakeMetric('weight', { state: 'stale' })] })
    const card = await tile('Weight')
    expect(within(card).getByText(/78\.4/, { selector: '.ov-sr' })).toBeInTheDocument()
  })

  it('links a tile to its detail screen and carries the range', async () => {
    open('/overview?range=90d', { metrics: [fakeMetric('weight')] })
    const link = await screen.findByRole('link', { name: 'Weight' })
    expect(link).toHaveAttribute('href', '/m/weight?range=90d')
  })

  it('shows no score card when the API sends none (flag off) or the score is not set', async () => {
    open('/overview')
    await screen.findByText('Nerve, activity, sleep and score')
    expect(screen.queryByText('Health score')).toBeNull()
  })

  it('shows no score card for a not_set score', async () => {
    open('/overview', { scores: [fakeScore({ status: 'not_set', value: null, reason: 'no_band_table' })] })
    await screen.findByText('Nerve, activity, sleep and score')
    expect(screen.queryByText('Health score')).toBeNull()
  })

  it('shows the computed score with its areas and n/a for an area without data', async () => {
    open('/overview', { scores: [fakeScore({})] })
    const card = (await screen.findByText('Health score')).closest('.ov-card') as HTMLElement
    expect(within(card).getByText('71', { selector: '.ov-sr' })).toBeInTheDocument()
    expect(within(card).getByText('ownvitals 1.0')).toBeInTheDocument()
    expect(within(card).getByText(/Body 78 · Heart 70 · Activity 64 · Sleep n\/a/)).toBeInTheDocument()
  })

  it('stays quiet: no streaks, rings or nudges', async () => {
    open('/overview', { metrics: [fakeMetric('weight')] })
    await screen.findByText('Needs a look')
    expect(document.body.textContent).not.toMatch(/streak|ring|goal|keep it up|congrat/i)
  })
})

describe('the overview: needs a look', () => {
  it('shows nothing-to-see when there are no live findings', async () => {
    open('/overview')
    expect(await screen.findByText('Nothing needs a look right now.')).toBeInTheDocument()
  })

  it('lists a finding with its tier, whether it is new, its confidence and the link to all of them', async () => {
    const findings = [1, 2, 3, 4].map((i) =>
      fakeFinding({ id: `00000000-0000-4000-8000-00000000000${i}`, seen_at: i === 1 ? null : '2026-11-01T08:00:00Z' }),
    )
    open('/overview', { findings: fakeFindings(findings) })
    const section = (await screen.findByText('Needs a look')).closest('section') as HTMLElement
    expect(within(section).getAllByText(/Confidence: medium/)).toHaveLength(3)
    expect(within(section).getAllByText('new')).toHaveLength(1)
    expect(within(section).getByRole('link', { name: 'All live findings (4)' })).toBeInTheDocument()
  })

  it('shows the link to all findings when the server has more', async () => {
    open('/overview', { findings: fakeFindings([fakeFinding()], 20) })
    expect(await screen.findByRole('link', { name: /All live findings/ })).toBeInTheDocument()
  })
})

describe('the overview: range', () => {
  it('marks the chosen range and links the others', async () => {
    open('/overview?range=1y')
    const nav = await screen.findByRole('navigation', { name: 'Range' })
    expect(within(nav).getByRole('link', { name: '1 year' })).toHaveAttribute('aria-current', 'true')
    expect(within(nav).getByRole('link', { name: '30 days' })).toHaveAttribute('href', '/overview?range=30d')
  })

  it('offers the six ranges, each as a deep link', async () => {
    open('/overview?range=7d')
    const nav = await screen.findByRole('navigation', { name: 'Range' })
    const links = within(nav).getAllByRole('link')
    expect(links.map((l) => l.textContent)).toEqual(['7 days', '30 days', '3 months', '6 months', '1 year', 'All'])
    expect(links.map((l) => l.getAttribute('href'))).toEqual(
      ['7d', '30d', '90d', '6mo', '1y', 'all'].map((r) => `/overview?range=${r}`),
    )
    expect(links[0]).toHaveAttribute('aria-current', 'true')
  })

  it('falls back to the saved range, else 7 days, for a range it does not know', async () => {
    open('/overview?range=forever', { profile: fakeProfile({ chart_range: '7d' }) })
    const nav = await screen.findByRole('navigation', { name: 'Range' })
    await waitFor(() => expect(within(nav).getByRole('link', { name: '7 days' })).toHaveAttribute('aria-current', 'true'))
  })

  it('falls back to a saved range other than 7 days for a range it does not know', async () => {
    open('/overview?range=forever', { profile: fakeProfile({ chart_range: '1y' }) })
    const nav = await screen.findByRole('navigation', { name: 'Range' })
    await waitFor(() => expect(within(nav).getByRole('link', { name: '1 year' })).toHaveAttribute('aria-current', 'true'))
    expect(within(nav).getByRole('link', { name: '7 days' })).not.toHaveAttribute('aria-current')
  })

  it('falls back to a saved 3 months for a range it does not know', async () => {
    open('/overview?range=forever', { profile: fakeProfile({ chart_range: '90d' }) })
    const nav = await screen.findByRole('navigation', { name: 'Range' })
    await waitFor(() => expect(within(nav).getByRole('link', { name: '3 months' })).toHaveAttribute('aria-current', 'true'))
  })

  it('lets a saved range win over the default when the address has none', async () => {
    open('/overview', { profile: fakeProfile({ chart_range: '6mo' }) })
    const nav = await screen.findByRole('navigation', { name: 'Range' })
    await waitFor(() => expect(within(nav).getByRole('link', { name: '6 months' })).toHaveAttribute('aria-current', 'true'))
  })
})

describe('the freshness line', () => {
  const line = async () => within(await screen.findByRole('region', { name: 'Data freshness' }))

  it('says when Withings last synced and where the last reading is from', async () => {
    open('/overview')
    const fresh = await line()
    expect(fresh.getByText(/Withings synced/)).toBeInTheDocument()
    expect(fresh.getByText(/Last reading/)).toBeInTheDocument()
  })

  it('is on every app screen, not only the overview', async () => {
    open('/heart')
    expect(await screen.findByRole('region', { name: 'Data freshness' })).toBeInTheDocument()
  })

  it('shows a sync that is more than two days old as stale', async () => {
    open('/overview', { status: fakeStatus({ last_sync_at: '2026-10-28T11:30:00Z' }) })
    const fresh = await line()
    expect(fresh.getByText(/Withings last synced/)).toBeInTheDocument()
  })

  it('shows an import in progress with its percentage', async () => {
    open('/overview', {
      status: fakeStatus({
        streams: [fakeStream('measures', 'importing', { progress_percent: 42 })],
        importing: ['measures'],
      }),
    })
    const fresh = await line()
    expect(fresh.getByText('Body measures: importing 42%')).toBeInTheDocument()
  })

  it('shows an import that stopped', async () => {
    open('/overview', {
      status: fakeStatus({ streams: [fakeStream('measures', 'incomplete')], incomplete: ['measures'] }),
    })
    const fresh = await line()
    expect(fresh.getByText('Body measures: the import is incomplete')).toBeInTheDocument()
  })

  it('shows a connection that needs renewing, with a way to fix it', async () => {
    open('/overview', {
      status: fakeStatus({ connection: 'needs_reconnect', broken_connections: ['withings'] }),
    })
    const fresh = await line()
    expect(fresh.getByText('The Withings connection needs to be renewed')).toBeInTheDocument()
    expect(fresh.getByRole('link', { name: 'Connect' })).toHaveAttribute('href', '/connections')
  })

  it('keeps what a stream cannot give behind Details', async () => {
    open('/overview', {
      status: fakeStatus({ streams: [fakeStream('ecg', 'not_supported')] }),
    })
    const fresh = await line()
    const summary = fresh.getByText('Details')
    expect(fresh.getByText('ECG: not supported by this account')).not.toBeVisible()
    fireEvent.click(summary)
    expect(fresh.getByText('ECG: not supported by this account')).toBeVisible()
  })

  it('is left out when the status cannot be read, and the page still works', async () => {
    fakeApi({
      'GET /api/auth/session': SIGNED_IN,
      'GET /api/status': () => reason(500, 'boom'),
      'GET /api/metrics': fakeMetrics([fakeMetric('weight')]),
      [FINDINGS]: fakeFindings(),
    })
    renderAt('/')
    expect(await screen.findByText('Needs a look')).toBeInTheDocument()
    expect(screen.queryByRole('region', { name: 'Data freshness' })).not.toBeInTheDocument()
  })
})

describe('the rail', () => {
  it('shows the connection as a dot, and marks an area whose stream cannot be read as n/a', async () => {
    open('/overview', {
      status: fakeStatus({
        streams: [fakeStream('ecg', 'not_supported'), fakeStream('sleep_summary', 'not_shared')],
      }),
    })
    const rail = await screen.findByRole('navigation', { name: 'Main' })
    expect(await within(rail).findAllByText('n/a')).toHaveLength(2)
    expect(within(rail).getByLabelText('Withings is connected')).toBeInTheDocument()
  })

  it('flags a connection that needs a look', async () => {
    open('/overview', { status: fakeStatus({ connection: 'needs_reconnect', broken_connections: ['withings'] }) })
    const rail = await screen.findByRole('navigation', { name: 'Main' })
    expect(await within(rail).findByLabelText('Withings needs a look')).toBeInTheDocument()
  })
})

describe('the area pages', () => {
  it('shows the heart tiles', async () => {
    open('/heart', {
      metrics: [
        fakeMetric('heart_pulse', { area: 'heart', unit: 'bpm', latest: { day: '2026-11-01', value: 61, n: 1, method: 'last' } }),
        fakeMetric('vascular_age', { area: 'heart', unit: 'y', state: 'not_supported', ...NO_VALUE }),
      ],
    })
    expect(await screen.findByRole('link', { name: 'Heart rate' })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Vascular age' })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'ECG recordings' })).toHaveAttribute('href', '/heart/ecg')
    expect(screen.queryByRole('link', { name: 'Weight' })).not.toBeInTheDocument()
  })

  it('shows sleep as a state only, never a number', async () => {
    open('/sleep', { status: fakeStatus({ streams: [fakeStream('sleep_summary', 'not_supported')] }) })
    const heading = await screen.findByRole('heading', { name: 'Sleep', level: 2 })
    expect(heading).toBeInTheDocument()
    expect(screen.getAllByText('Not supported').length).toBeGreaterThan(0)
  })

  it('shows activity as a state only', async () => {
    open('/activity', { status: fakeStatus({ streams: [fakeStream('activity_daily', 'importing', { progress_percent: 10 })] }) })
    expect(await screen.findByRole('heading', { name: 'Activity', level: 2 })).toBeInTheDocument()
    expect(screen.getAllByText(/Importing/).length).toBeGreaterThan(0)
  })
})
