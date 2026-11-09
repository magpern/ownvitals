import { describe, expect, it } from 'vitest'
import { decodeStatus, type MetricTile } from '../../api/overview'
import { t } from '../../i18n'
import { isMessageKey } from '../../i18n/store'
import { fakeStatus, fakeStream } from '../../test/overviewFixtures'
import {
  agoText,
  bloodPressure,
  detailKey,
  freshness,
  parseRange,
  railStatus,
  showsValue,
  stateKind,
} from './overviewModel'

// Synthetic data throughout (src/test/overviewFixtures.ts).

const NOW = new Date('2026-11-01T12:00:00Z')
const status = (more: Record<string, unknown> = {}) => decodeStatus(fakeStatus(more))

function metric(key: string, state: string, value: number | null): MetricTile {
  return {
    key,
    area: 'heart',
    unit: 'mmHg',
    stream: 'measures',
    state,
    progressPercent: null,
    progress: null,
    latest: value === null ? null : { day: '2026-11-01', value, n: 1 },
    typicalRange: null,
    sparkline: [],
  }
}

describe('parseRange', () => {
  it('reads the four ranges and falls back to 90 days', () => {
    expect(parseRange('30d')).toBe('30d')
    expect(parseRange('all')).toBe('all')
    expect(parseRange('6mo')).toBe('90d')
    expect(parseRange(null)).toBe('90d')
  })
})

describe('the state of a tile', () => {
  it.each([
    ['up_to_date', null],
    ['stale', 'stale'],
    ['importing', 'importing'],
    ['incomplete', 'incomplete-import'],
    ['insufficient_data', 'insufficient-data'],
    ['not_supported', 'not-supported'],
    ['not_shared', 'not-shared'],
    ['not_available', 'not-available'],
    ['needs_reconnect', 'needs-reconnect'],
    ['withings_plus_needed', 'withings-plus'],
    ['not_connected', 'not-available'],
    ['something_new', 'not-available'],
  ])('%s is %s', (state, kind) => {
    expect(stateKind(state)).toBe(kind)
  })

  it('shows a number only for a state with readings, and only when the API sent one', () => {
    for (const state of ['up_to_date', 'stale', 'incomplete']) expect(showsValue(metric('weight', state, 80))).toBe(true)
    for (const state of [
      'importing',
      'insufficient_data',
      'not_supported',
      'not_shared',
      'not_available',
      'needs_reconnect',
      'withings_plus_needed',
      'not_connected',
    ]) {
      expect(showsValue(metric('weight', state, 80))).toBe(false)
    }
    expect(showsValue(metric('weight', 'up_to_date', null))).toBe(false)
  })

  it('words every state the API can name, and has a sentence for an unknown one', () => {
    for (const state of ['not_supported', 'not_shared', 'not_available', 'needs_reconnect', 'withings_plus_needed', 'not_connected', 'importing', 'incomplete', 'stale']) {
      expect(isMessageKey(detailKey(state))).toBe(true)
    }
    expect(isMessageKey(detailKey('something_new'))).toBe(true)
  })
})

describe('blood pressure', () => {
  it('is one tile of the two values, only when both are known', () => {
    const both = bloodPressure([metric('systolic_bp', 'up_to_date', 118.4), metric('diastolic_bp', 'up_to_date', 76.2)])
    expect(both?.value).toBe('118/76')
    expect(both?.tile.key).toBe('blood_pressure')
    const half = bloodPressure([metric('systolic_bp', 'up_to_date', 118), metric('diastolic_bp', 'up_to_date', null)])
    expect(half?.value).toBeNull()
    expect(half?.tile.latest).toBeNull()
    expect(bloodPressure([metric('weight', 'up_to_date', 80)])).toBeNull()
  })
})

describe('agoText', () => {
  it.each([
    ['2026-11-01T11:59:40Z', 'just now'],
    ['2026-11-01T11:30:00Z', '30 min ago'],
    ['2026-11-01T07:00:00Z', '5 h ago'],
    ['2026-10-29T12:00:00Z', '3 d ago'],
    ['2026-11-02T12:00:00Z', 'just now'],
  ])('%s is "%s"', (from, text) => {
    expect(agoText(from, NOW, t)).toBe(text)
  })
})

describe('the freshness line', () => {
  const line = (s = status()) => freshness(s, NOW, t, isMessageKey)

  it('says when Withings last synced, and the last reading', () => {
    expect(line().lead.map((i) => [i.tone, i.text])).toEqual([
      ['ok', 'Withings synced 30 min ago'],
      ['ok', 'Last reading 5 h ago'],
    ])
  })

  it('calls a sync older than two days stale', () => {
    const [first] = line(status({ last_sync_at: '2026-10-29T12:00:00Z' })).lead
    expect(first).toMatchObject({ tone: 'attn', text: 'Withings last synced 3 d ago' })
  })

  it('says so when there has been no sync', () => {
    expect(line(status({ last_sync_at: null })).lead[0]).toMatchObject({ tone: 'attn', text: 'Withings: no sync yet' })
  })

  it('shows importing streams with their percentage, and incomplete imports', () => {
    const s = status({
      streams: [
        fakeStream('measures', 'importing', { progress_percent: 64 }),
        fakeStream('activity_daily', 'incomplete'),
        fakeStream('ecg'),
      ],
    })
    const texts = line(s).lead.map((i) => i.text)
    expect(texts).toContain('Body measures: importing 64%')
    expect(texts).toContain('Activity: the import is incomplete')
  })

  it.each([
    ['not_connected', 'attn', "Withings isn't connected"],
    ['needs_reconnect', 'urgent', 'The Withings connection needs to be renewed'],
    ['withings_plus_needed', 'info', 'Withings+ is needed for some of your data'],
  ])('shows a %s connection and offers no last reading', (connection, tone, text) => {
    const { lead } = line(status({ connection }))
    expect(lead).toHaveLength(1)
    expect(lead[0]).toMatchObject({ tone, text })
  })

  it('puts the streams that cannot give data behind the details, never as "no data"', () => {
    const s = status({
      streams: [fakeStream('ecg', 'not_supported'), fakeStream('sleep_summary', 'not_shared'), fakeStream('workouts', 'not_available')],
    })
    const { lead, details } = line(s)
    expect(lead.map((i) => i.id)).not.toContain('cap-ecg')
    expect(details.map((i) => i.text)).toEqual([
      'ECG: not supported by this account',
      'Sleep: not shared with ownvitals',
      'Workouts: no data from Withings yet',
    ])
  })

  it('leaves out a stream this screen has no name for', () => {
    expect(line(status({ streams: [fakeStream('mystery', 'importing')] })).lead).toHaveLength(2)
  })
})

describe('the rail marks', () => {
  it('shows the connection as a dot, and greys an area whose stream cannot be read', () => {
    const marks = railStatus(status({ streams: [fakeStream('ecg', 'not_supported'), fakeStream('sleep_summary', 'up_to_date')] }), t)
    expect(marks.connections?.badge).toEqual({ kind: 'dot', tone: 'ok', label: 'Withings is connected' })
    expect(marks.heart).toEqual({ dim: true, badge: { kind: 'text', text: 'n/a' } })
    expect(marks.sleep).toBeUndefined()
  })

  it('marks a connection that needs a look', () => {
    expect(railStatus(status({ connection: 'needs_reconnect' }), t).connections?.badge).toMatchObject({ tone: 'attn' })
    expect(railStatus(status({ broken_connections: ['withings'] }), t).connections?.badge).toMatchObject({ tone: 'attn' })
  })
})
