import { get } from './client'
import { list, object, optionalText, strings, text } from './decode'

// The admin abuse view (issue 461, ADR 0020): recent session anomalies across accounts. The server
// sends signal codes, the account and the coarse origin; never an address. Synthetic in tests.

export const ANOMALIES_PATH = '/api/admin/anomalies'

export interface SessionAnomaly {
  id: string
  at: string
  /** Null once the account is deleted. */
  email: string | null
  signals: string[]
  browser: string | null
  os: string | null
  place: string | null
}

export interface AnomalyCursor {
  beforeAt: string
  beforeId: string
}

export interface AnomalyPage {
  rows: SessionAnomaly[]
  next: AnomalyCursor | null
}

function anomaly(value: unknown): SessionAnomaly {
  const row = object(value, 'anomaly')
  return {
    id: text(row, 'id'),
    at: text(row, 'at'),
    email: optionalText(row, 'email'),
    signals: strings(row, 'signals'),
    browser: optionalText(row, 'browser'),
    os: optionalText(row, 'os'),
    place: optionalText(row, 'place'),
  }
}

function cursor(value: unknown): AnomalyCursor | null {
  if (value === null || value === undefined) return null
  const next = object(value, 'next')
  return { beforeAt: text(next, 'before_at'), beforeId: text(next, 'before_id') }
}

export async function listSessionAnomalies(after: AnomalyCursor | null = null): Promise<AnomalyPage> {
  const query =
    after === null
      ? ''
      : `?${new URLSearchParams({ before_at: after.beforeAt, before_id: after.beforeId }).toString()}`
  const body = object(await get(`${ANOMALIES_PATH}${query}`), 'anomalies')
  return { rows: list(body, 'rows').map(anomaly), next: cursor(body.next) }
}
