import { browserZone, formatDateTime, formatDay, formatDuration, formatNumber, formatTime } from '../lib/format'
import { useLocale } from './useI18n'

/** The `format.ts` functions bound to the current language; the component re-renders on a change. */
export function useFormat() {
  const locale = useLocale()
  return {
    locale,
    number: (value: number, digits?: number) => formatNumber(locale, value, digits),
    day: (day: string, style?: 'long' | 'short') => formatDay(locale, day, style),
    time: (instant: Date | string, zone: string) => formatTime(locale, instant, zone),
    dateTime: (instant: Date | string, zone: string) => formatDateTime(locale, instant, zone),
    duration: (seconds: number) => formatDuration(locale, seconds),
    browserZone,
  }
}
