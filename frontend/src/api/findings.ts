import { flag, isObject, list, object, optionalCount, optionalText, strings, text, type JsonObject } from './decode'

// The findings API (issue 200; backend/src/ownvitals/views/findings_routes.py, docs/dev/views.md).
// The server has already left out what this account may not see (hidden findings, the
// experimental tier without the owner-only switch): the screen never assumes either. A missing
// value stays `null`.

export const TABS = ['live', 'history'] as const
export type Tab = (typeof TABS)[number]
export const TIERS = ['device', 'trend', 'experimental', 'system'] as const
export type FindingTier = (typeof TIERS)[number]
export const AREAS = ['body', 'heart', 'nerve', 'activity', 'system'] as const
export type Area = (typeof AREAS)[number]

/** How much of the window had data: "27 of 30 days · coverage 90 % · longest gap 2 days". */
export interface Quality {
  nDays: number | null
  windowDays: number | null
  coverage: number | null
  longestGapDays: number | null
}

export interface Finding {
  id: string
  ruleId: string
  ruleVersion: number
  tier: string
  area: string | null
  confidence: string
  /** `live`, or `backfill` for a finding made while importing history. */
  origin: string
  metricKeys: string[]
  windowStart: string
  windowEnd: string
  referenceDay: string | null
  lastSeen: string
  /** Not yet looked at: the finding is "new". */
  isNew: boolean
  /** Section 11's "not evaluated by ownvitals" on E4 and E6. */
  notEvaluated: boolean
  quality: Quality
}

export interface FindingsPage {
  findings: Finding[]
  nextOffset: number | null
}

export interface FindingDetail {
  finding: Finding
  evidence: JsonObject
}

function decodeQuality(raw: unknown): Quality {
  const q = isObject(raw) ? raw : {}
  const read = (key: string): number | null => {
    const value = q[key]
    return typeof value === 'number' && Number.isFinite(value) ? value : null
  }
  return { nDays: read('n_days'), windowDays: read('window_days'), coverage: read('coverage'), longestGapDays: read('longest_gap_days') }
}

export function decodeFinding(raw: unknown): Finding {
  const x = object(raw, 'finding')
  return {
    id: text(x, 'id'),
    ruleId: text(x, 'rule_id'),
    ruleVersion: typeof x.rule_version === 'number' ? x.rule_version : 0,
    tier: text(x, 'tier'),
    area: optionalText(x, 'area'),
    confidence: text(x, 'confidence'),
    origin: text(x, 'origin'),
    metricKeys: strings(x, 'metric_keys'),
    windowStart: text(x, 'window_start'),
    windowEnd: text(x, 'window_end'),
    referenceDay: optionalText(x, 'reference_day'),
    lastSeen: text(x, 'last_seen'),
    isNew: optionalText(x, 'seen_at') === null,
    notEvaluated: flag(x, 'not_evaluated'),
    quality: decodeQuality(x.data_quality),
  }
}

export function decodeFindingsPage(raw: unknown): FindingsPage {
  const page = object(raw, 'findings')
  return { findings: list(page, 'findings').map(decodeFinding), nextOffset: optionalCount(page, 'next_offset') }
}

export function decodeFindingDetail(raw: unknown): FindingDetail {
  const d = object(raw, 'finding')
  return {
    finding: decodeFinding(d.finding),
    evidence: isObject(d.evidence) ? d.evidence : {},
  }
}

export interface Filters {
  tab: Tab
  tier: FindingTier | null
  area: Area | null
}

export function findingsListPath({ tab, tier, area }: Filters, offset: number): string {
  const query = new URLSearchParams({ tab })
  if (tier !== null) query.set('tier', tier)
  if (area !== null) query.set('area', area)
  query.set('offset', String(offset))
  return `/api/findings?${query.toString()}`
}

export const findingPath = (id: string) => `/api/findings/${encodeURIComponent(id)}`
