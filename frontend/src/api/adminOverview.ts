import { DecodeError, count, flag, object, optionalText, text } from './decode'

// The admin Overview tiles and the Withings app card (issue 549). Counts and three non-secret
// facts only: the server sends no id, address, token, secret or health value, and only the fields
// the screens show are decoded.

export const OVERVIEW_PATH = '/api/admin/overview'
export const WITHINGS_APP_PATH = '/api/admin/withings-app'

/** An account's Withings connection, in the words of the user's own Connections page. */
export type WithingsState = 'healthy' | 'needs_reconnect' | 'not_connected'

export function withingsState(source: Record<string, unknown>, key: string): WithingsState {
  const value = source[key]
  if (value === 'healthy' || value === 'needs_reconnect' || value === 'not_connected') return value
  throw new DecodeError(key)
}

export interface AdminOverview {
  accounts: { total: number; secondFactorOn: number; signup: 'invite' | 'closed' }
  connections: { healthy: number; needsReconnect: number; notConnected: number }
  jobs: { queued: number; running: number; deadLetter: number }
}

export function decodeOverview(raw: unknown): AdminOverview {
  const body = object(raw, 'overview')
  const accounts = object(body.accounts, 'accounts')
  const connections = object(body.connections, 'connections')
  const jobs = object(body.jobs, 'jobs')
  const signup = text(accounts, 'signup')
  if (signup !== 'invite' && signup !== 'closed') throw new DecodeError('signup')
  return {
    accounts: { total: count(accounts, 'total'), secondFactorOn: count(accounts, 'second_factor_on'), signup },
    connections: {
      healthy: count(connections, 'healthy'),
      needsReconnect: count(connections, 'needs_reconnect'),
      notConnected: count(connections, 'not_connected'),
    },
    jobs: { queued: count(jobs, 'queued'), running: count(jobs, 'running'), deadLetter: count(jobs, 'dead_letter') },
  }
}

export interface WithingsApp {
  configured: boolean
  /** "…a91f": the last four characters of the client id, never the whole id. */
  clientHint: string | null
  apiHost: string
}

export function decodeWithingsApp(raw: unknown): WithingsApp {
  const body = object(raw, 'withings app')
  return {
    configured: flag(body, 'configured'),
    clientHint: optionalText(body, 'client_hint'),
    apiHost: text(body, 'api_host'),
  }
}
