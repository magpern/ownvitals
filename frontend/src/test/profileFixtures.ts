// A synthetic /api/profile answer for the Settings tests: made up, not read from any account.

export function fakeProfile(more: Record<string, unknown> = {}) {
  return {
    birth_year: 1982,
    sex: null,
    day_assignment: 'reading_tz',
    day_tz: null,
    theme: 'system',
    weight_unit: 'kg',
    temperature_unit: 'c',
    datetime_format: 'd_mon_y',
    chart_range: '90d',
    overview_order: ['body', 'heart', 'nerve', 'activity'],
    show_unsupported: true,
    show_experimental: false,
    notify_email: true,
    notify_weekly: false,
    notify_tiers: ['device', 'trend', 'system'],
    experimental_available: false,
    ai_available: false,
    ...more,
  }
}

export const PROFILE_ROUTES = { 'GET /api/profile': fakeProfile() }
