import { describe, expect, it } from 'vitest'
import type { Locale } from '../i18n/locale'
import {
  browserZone,
  formatDateOfInstant,
  formatDateTime,
  formatDay,
  formatDuration,
  formatNumber,
  formatTime,
} from './format'

// Table tests for en and sv. Spaces in Swedish output (before units, in thousands) are no-break
// spaces and the minus is U+2212, as Intl writes them.
const NBSP = ' '
const MINUS = '−'

describe('formatNumber', () => {
  it.each<[Locale, number, number, string]>([
    ['en', 78.4, 1, '78.4'],
    ['sv', 78.4, 1, '78,4'],
    ['en', 78, 1, '78.0'],
    ['sv', 78, 1, '78,0'],
    ['en', 1234.5, 1, '1,234.5'],
    ['sv', 1234.5, 1, `1${NBSP}234,5`],
    ['en', 0.456, 2, '0.46'],
    ['sv', 0.456, 2, '0,46'],
    ['en', 7, 0, '7'],
    ['sv', -3.5, 1, `${MINUS}3,5`],
  ])('%s: %d with %d decimals is %s', (locale, value, digits, expected) => {
    expect(formatNumber(locale, value, digits)).toBe(expected)
  })
})

describe('formatDay', () => {
  it.each<[Locale, string, 'long' | 'short', string]>([
    ['en', '2026-10-14', 'long', '14 Oct 2026'],
    ['sv', '2026-10-14', 'long', '14 okt. 2026'],
    ['en', '2026-03-01', 'short', '1 Mar'],
    ['sv', '2026-03-01', 'short', '1 mars'],
    ['en', '2026-12-31', 'long', '31 Dec 2026'],
  ])('%s: %s (%s) is %s', (locale, day, style, expected) => {
    expect(formatDay(locale, day, style)).toBe(expected)
  })

  it('shows a day as the API sent it, in whatever zone the browser is in', () => {
    // Midnight UTC is still the previous evening west of Greenwich: the day must not move.
    for (const zone of ['Pacific/Honolulu', 'Asia/Tokyo', 'UTC']) {
      const text = new Intl.DateTimeFormat('en-GB', { timeZone: zone }).format(new Date('2026-10-14T00:00:00Z'))
      expect(text).toMatch(/1[34]\/10\/2026/)
      expect(formatDay('en', '2026-10-14')).toBe('14 Oct 2026')
    }
  })

  it('returns text that is not a day unchanged, and refuses a day that does not exist', () => {
    expect(formatDay('en', 'yesterday')).toBe('yesterday')
    expect(formatDay('en', '2026-02-30')).toBe('2026-02-30')
    expect(formatDay('en', '2026-13-01')).toBe('2026-13-01')
  })
})

describe('a reading\'s time in a passed zone', () => {
  // Europe/Stockholm changes to summer time on 2026-03-29 at 01:00 UTC (02:00 becomes 03:00)
  // and back on 2026-10-25 at 01:00 UTC.
  it.each<[string, string, string]>([
    ['2026-03-28T23:30:00Z', 'Europe/Stockholm', '00:30'],
    ['2026-03-29T00:30:00Z', 'Europe/Stockholm', '01:30'],
    ['2026-03-29T01:30:00Z', 'Europe/Stockholm', '03:30'],
    ['2026-10-24T23:30:00Z', 'Europe/Stockholm', '01:30'],
    ['2026-10-25T00:30:00Z', 'Europe/Stockholm', '02:30'],
    ['2026-10-25T01:30:00Z', 'Europe/Stockholm', '02:30'],
    ['2026-03-29T01:30:00Z', 'UTC', '01:30'],
    ['2026-03-29T01:30:00Z', 'America/New_York', '21:30'],
  ])('%s in %s is %s', (instant, zone, expected) => {
    expect(formatTime('en', instant, zone)).toBe(expected)
    expect(formatTime('sv', instant, zone)).toBe(expected)
  })

  it('does not depend on the zone of the machine', () => {
    expect(formatTime('en', new Date('2026-07-01T05:00:00Z'), 'Europe/Stockholm')).toBe('07:00')
    expect(formatTime('en', new Date('2026-07-01T05:00:00Z'), 'Asia/Tokyo')).toBe('14:00')
  })

  it('gives the date and time, and the date of an instant, in that zone', () => {
    expect(formatDateTime('en', '2026-10-14T05:04:00Z', 'Europe/Stockholm')).toBe('14 Oct 2026, 07:04')
    expect(formatDateTime('sv', '2026-10-14T05:04:00Z', 'Europe/Stockholm')).toBe('14 okt. 2026 07:04')
    expect(formatDateOfInstant('en', '2026-10-14T23:30:00Z', 'Europe/Stockholm')).toBe('15 Oct')
    expect(formatDateOfInstant('en', '2026-10-14T23:30:00Z', 'UTC')).toBe('14 Oct')
  })
})

describe('formatDuration', () => {
  it.each<[Locale, number, string]>([
    ['en', 27000, '7 hrs 30 mins'],
    ['sv', 27000, '7 tim 30 min'],
    ['en', 3600, '1 hr'],
    ['en', 2700, '45 mins'],
    ['sv', 2700, '45 min'],
    ['en', 0, '0 secs'],
    ['en', 42, '42 secs'],
    ['sv', 42, '42 s'],
    ['en', 59.6, '1 min'],
    ['en', -5, '0 secs'],
  ])('%s: %d seconds is %s', (locale, seconds, expected) => {
    expect(formatDuration(locale, seconds)).toBe(expected)
  })
})

describe('browserZone', () => {
  it('is an IANA zone name', () => {
    expect(browserZone()).toMatch(/^[A-Za-z_]+(\/[A-Za-z_+-]+)*$|^UTC$/)
  })
})
