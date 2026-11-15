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

// The AI consent of an account without AI (the flag off, no consent): synthetic.
export const AI_CONSENT_NONE = {
  enabled: false,
  consent: { state: 'none', version: null, given_at: null },
  consent_text: { version: 1, locale: 'en', text: 'Synthetic consent text.', sha256: '0'.repeat(64) },
}

// The email preferences of an account that switched nothing off: synthetic.
export const EMAIL_PREFS_ON = {
  kinds: ['findings_waiting', 'connection_attention', 'ai_digest_ready'].map((kind) => ({ kind, enabled: true })),
}

export const PROFILE_ROUTES = {
  'GET /api/profile': fakeProfile(),
  'GET /api/ai/consent?locale=en': AI_CONSENT_NONE,
  // The export card (issue 211): no export yet.
  'GET /api/exports': { exports: [] },
  // The email preference per kind (issue 459): every kind on.
  'GET /api/notify/email': EMAIL_PREFS_ON,
}
