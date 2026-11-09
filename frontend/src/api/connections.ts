import { request } from './client'
import { flag, list, object, optionalFlag, optionalText, text, DecodeError, type JsonObject } from './decode'

// The Withings connection of the signed-in user (issue 196). GET /api/connections lists the rows
// (no tokens ever); DELETE /api/connections/withings disconnects (issue 163; recent sign-in and a
// step-up code, so the caller runs it through `protect`). Starting the connect is
// POST /api/connections/withings/start (issue 296, same step-up), which answers the Withings
// authorize address; the callback returns to /onboarding?withings=<outcome>
// (docs/dev/connections.md).

export type ConnectionStatus = 'connected' | 'needs_reconnect' | 'disconnected'

export interface Connection {
  id: string
  status: ConnectionStatus
  /** The category of the last failure (`revoke_failed`, ...), when there is one. */
  lastError: string | null
  connectedAt: string
  lastUsedAt: string | null
}

function connection(raw: unknown): Connection {
  const c: JsonObject = object(raw, 'connection')
  const status = text(c, 'status')
  if (status !== 'connected' && status !== 'needs_reconnect' && status !== 'disconnected') {
    throw new DecodeError('status')
  }
  return {
    id: text(c, 'id'),
    status,
    lastError: optionalText(c, 'last_error'),
    connectedAt: text(c, 'connected_at'),
    lastUsedAt: optionalText(c, 'last_used_at'),
  }
}

/** The user's live Withings row, or `null` when there is none (a disconnected row is not live). */
export function decodeWithings(raw: unknown): Connection | null {
  const rows = list(object(raw, 'connections'), 'connections')
    .map((row) => ({ row: object(row, 'connection'), parsed: connection(row) }))
    .filter(({ row, parsed }) => text(row, 'provider') === 'withings' && parsed.status !== 'disconnected')
  return rows[0]?.parsed ?? null
}

/** Where to send the browser: https only, never a script or relative address. */
export function decodeAuthorizeUrl(raw: unknown): string {
  const value = text(object(raw, 'start'), 'authorize_url')
  let url: URL
  try {
    url = new URL(value)
  } catch {
    throw new DecodeError('authorize_url')
  }
  if (url.protocol !== 'https:') throw new DecodeError('authorize_url')
  return url.toString()
}

export async function startWithingsConnect(): Promise<string> {
  return decodeAuthorizeUrl(await request('POST', '/api/connections/withings/start', { body: {} }))
}

export interface Disconnected {
  /** Withings could not be told: the person removes access in their Withings account. */
  revokeFailed: boolean
  importedDeleted: boolean
}

export async function disconnectWithings(deleteImported: boolean): Promise<Disconnected> {
  const path = `/api/connections/withings${deleteImported ? '?delete_imported=true' : ''}`
  const answer = object(await request('DELETE', path), 'disconnect')
  return { revokeFailed: flag(answer, 'revoke_failed'), importedDeleted: optionalFlag(answer, 'imported_deleted') === true }
}

/** The one place that leaves the app for another address (a test replaces `go`). */
export const leave = {
  go: (url: string): void => {
    window.location.assign(url)
  },
}
