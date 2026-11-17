import { get } from './client'
import { count, flag, list, object, optionalText, text } from './decode'

// The read-only admin views (issue 548): job counts, the flags and the audit log. Only the fields
// the screens show are decoded: no job kind or id, no audit params or request id.

export const JOBS_PATH = '/api/admin/jobs'
export const SETTINGS_PATH = '/api/admin/settings'
export const AUDIT_PATH = '/api/admin/audit'

export interface JobStateCount {
  state: string
  count: number
}

export function decodeJobs(raw: unknown): JobStateCount[] {
  const body = object(raw, 'jobs')
  return list(body, 'states').map((item) => {
    const row = object(item, 'job state')
    return { state: text(row, 'state'), count: count(row, 'count') }
  })
}

export interface FlagView {
  key: string
  source: string
  /** The effective value, as words: "invite", "on", "off", a banner as "level: text", or null for none. */
  effective: string | null
  /** The server settings' own value, as words (issue 550: what a stored value may only tighten). */
  environment: string | null
  /** Whether a value is stored, so "Use the server setting" has something to clear. */
  stored: boolean
  storedValid: boolean
}

/** A flag value as the text to show: a string, a boolean as on/off, a banner as "level: text". */
export function flagValue(value: unknown): string | null {
  if (value === null || value === undefined) return null
  if (typeof value === 'string') return value
  if (typeof value === 'boolean') return value ? 'on' : 'off'
  if (typeof value === 'object') {
    const banner = value as Record<string, unknown>
    if (typeof banner.text === 'string') {
      return typeof banner.level === 'string' ? `${banner.level}: ${banner.text}` : banner.text
    }
  }
  return null
}

export function decodeFlags(raw: unknown): FlagView[] {
  return list(object(raw, 'settings'), 'flags').map((item) => {
    const row = object(item, 'flag')
    return {
      key: text(row, 'key'),
      source: text(row, 'source'),
      effective: flagValue(row.effective),
      environment: flagValue(row.environment),
      stored: row.stored !== null && row.stored !== undefined,
      storedValid: flag(row, 'stored_valid'),
    }
  })
}

export interface AuditRow {
  id: number
  at: string
  action: string
  /** "kind id", the kind alone, or null when the action has no target. */
  target: string | null
  outcome: string
}

export interface AuditPage {
  rows: AuditRow[]
  nextBefore: number | null
}

export function decodeAudit(raw: unknown): AuditPage {
  const body = object(raw, 'audit')
  const rows = list(body, 'rows').map((item) => {
    const row = object(item, 'audit row')
    const kind = optionalText(row, 'target_kind')
    const id = optionalText(row, 'target_id')
    const target = kind === null ? id : id === null ? kind : `${kind} ${id}`
    return { id: count(row, 'id'), at: text(row, 'created_at'), action: text(row, 'action'), target, outcome: text(row, 'outcome') }
  })
  const next = body.next_before
  return { rows, nextBefore: typeof next === 'number' ? next : null }
}

export async function listAudit(before: number | null, signal?: AbortSignal): Promise<AuditPage> {
  const query = before === null ? '' : `?before=${String(before)}`
  return decodeAudit(await get(`${AUDIT_PATH}${query}`, signal))
}
