import { request } from './client'
import { flag, object, optionalFlag, optionalCount, optionalText, strings, text, DecodeError, type JsonObject } from './decode'
import type { ThemePreference } from '../theme/theme'

// GET and PUT /api/profile (issue 203): the analysis profile, the display options, the overview
// layout and the notification choices of the signed-in user. The server validates every field; the
// screen only offers values it accepts. `experimentalAvailable` and `aiAvailable` are read-only
// states of the account (the admin's switch and the AI flag).

export type Sex = 'female' | 'male'
export type DayAssignment = 'reading_tz' | 'fixed_tz'
export type WeightUnit = 'kg' | 'lb' | 'st'
export type TemperatureUnit = 'c' | 'f'
export type DatetimeFormat = 'd_mon_y' | 'mon_d_y' | 'iso'
export type ChartRange = '30d' | '90d' | '6mo' | '1y'
export type Area = 'body' | 'heart' | 'nerve' | 'activity'
export type Tier = 'device' | 'trend' | 'experimental' | 'system'

export const AREAS: readonly Area[] = ['body', 'heart', 'nerve', 'activity']
export const TIERS: readonly Tier[] = ['device', 'trend', 'system', 'experimental']

/** What is saved (PUT body): every field, as one document. */
export interface ProfileFields {
  birth_year: number | null
  sex: Sex | null
  day_assignment: DayAssignment
  day_tz: string | null
  theme: ThemePreference
  weight_unit: WeightUnit
  temperature_unit: TemperatureUnit
  datetime_format: DatetimeFormat
  chart_range: ChartRange
  overview_order: Area[]
  show_unsupported: boolean
  show_experimental: boolean
  notify_email: boolean
  notify_weekly: boolean
  notify_tiers: Tier[]
}

export interface Profile extends ProfileFields {
  experimentalAvailable: boolean
  aiAvailable: boolean
}

function oneOf<T extends string>(source: JsonObject, key: string, allowed: readonly T[]): T {
  const value = text(source, key)
  const found = allowed.find((item) => item === value)
  if (found === undefined) throw new DecodeError(key)
  return found
}

function manyOf<T extends string>(source: JsonObject, key: string, allowed: readonly T[]): T[] {
  return strings(source, key).map((value) => {
    const found = allowed.find((item) => item === value)
    if (found === undefined) throw new DecodeError(key)
    return found
  })
}

function optionalOneOf<T extends string>(source: JsonObject, key: string, allowed: readonly T[]): T | null {
  const value = optionalText(source, key)
  if (value === null) return null
  const found = allowed.find((item) => item === value)
  if (found === undefined) throw new DecodeError(key)
  return found
}

export function decodeProfile(raw: unknown): Profile {
  const p = object(raw, 'profile')
  return {
    birth_year: optionalCount(p, 'birth_year'),
    sex: optionalOneOf(p, 'sex', ['female', 'male']),
    day_assignment: oneOf(p, 'day_assignment', ['reading_tz', 'fixed_tz']),
    day_tz: optionalText(p, 'day_tz'),
    theme: oneOf(p, 'theme', ['system', 'light', 'dark']),
    weight_unit: oneOf(p, 'weight_unit', ['kg', 'lb', 'st']),
    temperature_unit: oneOf(p, 'temperature_unit', ['c', 'f']),
    datetime_format: oneOf(p, 'datetime_format', ['d_mon_y', 'mon_d_y', 'iso']),
    chart_range: oneOf(p, 'chart_range', ['30d', '90d', '6mo', '1y']),
    overview_order: manyOf(p, 'overview_order', AREAS),
    show_unsupported: flag(p, 'show_unsupported'),
    show_experimental: flag(p, 'show_experimental'),
    notify_email: flag(p, 'notify_email'),
    notify_weekly: flag(p, 'notify_weekly'),
    notify_tiers: manyOf(p, 'notify_tiers', TIERS),
    experimentalAvailable: flag(p, 'experimental_available'),
    aiAvailable: flag(p, 'ai_available'),
  }
}

export function fieldsOf(profile: Profile): ProfileFields {
  const { experimentalAvailable: _e, aiAvailable: _a, ...fields } = profile
  void _e
  void _a
  return fields
}

export async function saveProfile(fields: ProfileFields): Promise<Profile> {
  return decodeProfile(await request('PUT', '/api/profile', { body: fields }))
}

/** Delete the account (recent auth and a step-up code; the caller runs it through `protect`). Resolves to whether revoking the Withings grant failed. */
export async function deleteAccount(): Promise<boolean> {
  const answer = await request('DELETE', '/api/auth/account')
  return optionalFlag(object(answer, 'account'), 'revoke_failed') === true
}
