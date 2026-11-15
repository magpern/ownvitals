import { list, object, optionalText, text, type JsonObject } from './decode'

// GET /api/auth/activity (issue 251): the signed-in user's own security events, newest first. The
// server sends codes and allowlisted fields only (kind, time, method, browser and system family,
// place); the wording is the frontend's catalogue (`security.activity.*`, G4). Path: the newest
// page of at most 50 (the API's maximum); "Last 30 days" is applied by the panel.

export const ACTIVITY_PATH = '/api/auth/activity?limit=50'

export interface ActivityEvent {
  /** A code (`activity_model.SHOWN_KINDS`); the catalogue words it, an unknown one gets a generic line. */
  kind: string
  at: string
  /** `password`, `google`, `passkey` or `qr`; null when the kind has none. */
  method: string | null
  browser: string | null
  os: string | null
  /** "City, CC", "CC" or "Place unknown". */
  place: string
}

function event(raw: unknown): ActivityEvent {
  const e: JsonObject = object(raw, 'event')
  return {
    kind: text(e, 'kind'),
    at: text(e, 'at'),
    method: optionalText(e, 'method'),
    browser: optionalText(e, 'browser'),
    os: optionalText(e, 'os'),
    place: text(e, 'place'),
  }
}

export function decodeActivity(raw: unknown): ActivityEvent[] {
  return list(object(raw, 'activity'), 'events').map(event)
}
