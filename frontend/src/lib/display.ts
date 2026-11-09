import type { Locale } from '../i18n/locale'
import { formatDay } from './format'
import type { ChartRange, DatetimeFormat, TemperatureUnit, WeightUnit } from '../api/profile'

// The saved display settings (units, date format) applied to what the screens show (issue 305). The
// stored and analysed values stay SI: only what is shown is converted, and only here. Pure.

export interface DisplaySettings {
  weightUnit: WeightUnit
  temperatureUnit: TemperatureUnit
  datetimeFormat: DatetimeFormat
}

export const DEFAULT_DISPLAY: DisplaySettings = { weightUnit: 'kg', temperatureUnit: 'c', datetimeFormat: 'd_mon_y' }

const LB_PER_KG = 2.2046226218
const LB_PER_STONE = 14

/** A value in the API's unit as the person chose to read it: `{ value, unit }`. Other units pass through. */
export function convertQuantity(
  settings: DisplaySettings,
  value: number,
  unit: string,
): { value: number; unit: string } {
  if (unit === 'kg' && settings.weightUnit === 'lb') return { value: value * LB_PER_KG, unit: 'lb' }
  if (unit === 'kg' && settings.weightUnit === 'st') return { value: (value * LB_PER_KG) / LB_PER_STONE, unit: 'st' }
  if ((unit === '°C' || unit === 'C') && settings.temperatureUnit === 'f') return { value: value * 1.8 + 32, unit: '°F' }
  return { value, unit }
}

/** The unit label a quantity in `unit` shows with these settings. */
export function unitLabel(settings: DisplaySettings, unit: string): string {
  return convertQuantity(settings, 0, unit).unit
}

/** A day in the chosen format: "14 Oct 2026", "Oct 14, 2026" or "2026-10-14". */
export function formatDayAs(
  locale: Locale,
  format: DatetimeFormat,
  day: string,
  style: 'long' | 'short' = 'long',
): string {
  if (format === 'iso') {
    if (!/^\d{4}-\d{2}-\d{2}$/.test(day)) return day
    return style === 'long' ? day : day.slice(5)
  }
  if (format === 'mon_d_y') return formatDay(locale, day, style, 'en-US')
  return formatDay(locale, day, style)
}

/** The range the overview and metric screens open on: the saved one if the screen offers it. */
export function startRange<T extends string>(saved: ChartRange | null, offered: readonly T[], fallback: T): T {
  return offered.find((r) => r === saved) ?? fallback
}
