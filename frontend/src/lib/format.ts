import { INTL_TAG, type Locale } from '../i18n/locale'

// Numbers, days, times and durations in the user's language (ADR 0015 section 4), all on `Intl`.
// Units are SI and never converted. A day is the API's date as it is: it is never moved to
// another zone, so a day on a chart is the day the analysis used. A reading's time is shown in the
// zone the caller passes (the day setting's zone); the browser's zone is only for times that
// belong to no reading (`browserZone`). Pure: the locale and the zone come in as arguments.

/** A number with the locale's decimal separator and a fixed number of decimals. */
export function formatNumber(locale: Locale, value: number, digits = 0): string {
  return new Intl.NumberFormat(INTL_TAG[locale], {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  }).format(value)
}

const ISO_DAY = /^(\d{4})-(\d{2})-(\d{2})$/

/** "14 Oct 2026" from "2026-10-14". Text that is not a day is returned unchanged. */
export function formatDay(
  locale: Locale,
  day: string,
  style: 'long' | 'short' = 'long',
  tag: string = INTL_TAG[locale],
): string {
  const match = ISO_DAY.exec(day)
  if (match === null) return day
  const [year, month, date] = [Number(match[1]), Number(match[2]), Number(match[3])]
  // Noon UTC, formatted in UTC: the same calendar day wherever the browser is.
  const at = new Date(Date.UTC(year, month - 1, date, 12))
  if (at.getUTCMonth() !== month - 1 || at.getUTCDate() !== date) return day
  return new Intl.DateTimeFormat(tag, {
    timeZone: 'UTC',
    day: 'numeric',
    month: 'short',
    ...(style === 'long' ? { year: 'numeric' } : {}),
  }).format(at)
}

function instantOf(value: Date | string): Date {
  return typeof value === 'string' ? new Date(value) : value
}

/** The clock time of an instant in `zone` (an IANA name), such as "07:30". */
export function formatTime(locale: Locale, instant: Date | string, zone: string): string {
  return new Intl.DateTimeFormat(INTL_TAG[locale], {
    timeZone: zone,
    hour: '2-digit',
    minute: '2-digit',
  }).format(instantOf(instant))
}

/** The calendar day of an instant in `zone`, such as "27 Oct". */
export function formatDateOfInstant(locale: Locale, instant: Date | string, zone: string): string {
  return new Intl.DateTimeFormat(INTL_TAG[locale], { timeZone: zone, day: 'numeric', month: 'short' }).format(
    instantOf(instant),
  )
}

/** Day and time of an instant in `zone`, such as "14 Oct 2026, 07:04". */
export function formatDateTime(locale: Locale, instant: Date | string, zone: string): string {
  return new Intl.DateTimeFormat(INTL_TAG[locale], {
    timeZone: zone,
    day: 'numeric',
    month: 'short',
    year: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  }).format(instantOf(instant))
}

/** "7 hr 30 min" or, in Swedish, "7 tim 30 min". Under a minute it counts seconds. */
export function formatDuration(locale: Locale, totalSeconds: number): string {
  const seconds = Math.max(0, Math.round(totalSeconds))
  const unit = (value: number, name: 'hour' | 'minute' | 'second') =>
    new Intl.NumberFormat(INTL_TAG[locale], { style: 'unit', unit: name, unitDisplay: 'short' }).format(value)
  if (seconds < 60) return unit(seconds, 'second')
  const minutes = Math.round(seconds / 60)
  const [hours, rest] = [Math.floor(minutes / 60), minutes % 60]
  const parts: string[] = []
  if (hours > 0) parts.push(unit(hours, 'hour'))
  if (rest > 0 || hours === 0) parts.push(unit(rest, 'minute'))
  return parts.join(' ')
}

/** The browser's own zone, for times that belong to no reading (sessions, for example). */
export function browserZone(): string {
  return new Intl.DateTimeFormat().resolvedOptions().timeZone
}
