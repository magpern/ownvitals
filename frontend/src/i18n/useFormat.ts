import { useDisplay } from '../display/DisplayContext'
import { convertQuantity, formatDayAs, unitLabel } from '../lib/display'
import { browserZone, formatDateTime, formatDuration, formatNumber, formatTime } from '../lib/format'
import { useLocale } from './useI18n'

/** The `format.ts` functions bound to the current language; the component re-renders on a change. */
export function useFormat() {
  const locale = useLocale()
  const { settings } = useDisplay()
  return {
    locale,
    number: (value: number, digits?: number) => formatNumber(locale, value, digits),
    day: (day: string, style?: 'long' | 'short') => formatDayAs(locale, settings.datetimeFormat, day, style),
    /** A value in the API's unit, shown in the saved unit (kg, lb or st; C or F). */
    quantity: (value: number, unit: string, digits?: number) => {
      const shown = convertQuantity(settings, value, unit)
      return formatNumber(locale, shown.value, digits)
    },
    unit: (unit: string) => unitLabel(settings, unit),
    /** The saved units, for code that converts a series. */
    settings,
    time: (instant: Date | string, zone: string) => formatTime(locale, instant, zone),
    dateTime: (instant: Date | string, zone: string) => formatDateTime(locale, instant, zone),
    duration: (seconds: number) => formatDuration(locale, seconds),
    browserZone,
  }
}
