import { isObject, type JsonObject } from './decode'

// What the Health score's "How it's computed" panel reads from a `his` entry of GET /api/scores
// (issue 493): the per-criterion outcomes in `components.criteria`, the band table's id, version and SHA,
// and the provenance of the stored row. Everything may be missing (a row stored before the server
// sent the bands), so each field falls back to `null` and the panel shows what it has.

export const HIS_AREA_KEYS = ['body', 'heart', 'activity', 'sleep'] as const
export type HisAreaKey = (typeof HIS_AREA_KEYS)[number]

/** One side of a criterion's bands: the boundary where the points are 100, 75, 50 and 0. */
export interface HisBand {
  perfect: number
  good: number
  average: number
  bad: number
}

export interface HisCriterion {
  /** The server's key (`bmi`, `daily_steps` ...); an unknown one is shown as sent. */
  key: string
  area: HisAreaKey | null
  /** `computed` or the status of why it has no value. */
  status: string
  /** A reason code when there is no value (G4: a code, never a sentence). */
  reason: string | null
  value: number | null
  unit: string
  /** 0 to 100, set only with a value. */
  points: number | null
  low: HisBand | null
  high: HisBand | null
}

export interface HisDetail {
  criteria: HisCriterion[]
  table: { id: string; version: string; sha: string | null } | null
  /** The day the score was computed for (`YYYY-MM-DD`), or `null`. */
  day: string | null
  stale: boolean
  inputRevision: string | null
  inputFingerprint: string | null
}

function finite(v: unknown): number | null {
  return typeof v === 'number' && Number.isFinite(v) ? v : null
}

function str(v: unknown): string | null {
  return typeof v === 'string' && v !== '' ? v : null
}

function band(v: unknown): HisBand | null {
  if (!isObject(v)) return null
  const perfect = finite(v.perfect)
  const good = finite(v.good)
  const average = finite(v.average)
  const bad = finite(v.bad)
  return perfect === null || good === null || average === null || bad === null ? null : { perfect, good, average, bad }
}

function criterion(key: string, raw: JsonObject): HisCriterion {
  const area = HIS_AREA_KEYS.find((a) => a === raw.vertical) ?? null
  return {
    key,
    area,
    status: str(raw.status) ?? 'not_available',
    reason: str(raw.reason),
    value: finite(raw.value),
    unit: str(raw.unit) ?? '',
    points: finite(raw.score),
    low: band(raw.low),
    high: band(raw.high),
  }
}

/** Criteria in the order the panel lists them: by area, then by key. */
function criteria(components: JsonObject): HisCriterion[] {
  const raw = isObject(components.criteria) ? components.criteria : {}
  const rank = (c: HisCriterion) => (c.area === null ? HIS_AREA_KEYS.length : HIS_AREA_KEYS.indexOf(c.area))
  return Object.entries(raw)
    .filter((e): e is [string, JsonObject] => isObject(e[1]))
    .map(([key, value]) => criterion(key, value))
    .sort((a, b) => rank(a) - rank(b) || a.key.localeCompare(b.key))
}

export function decodeHisDetail(entry: JsonObject, components: JsonObject): HisDetail {
  const table = isObject(components.table) ? components.table : null
  const id = table === null ? null : str(table.id)
  return {
    criteria: criteria(components),
    table: table === null || id === null ? null : { id, version: str(table.version) ?? '', sha: str(entry.table_sha) },
    day: str(entry.day),
    stale: entry.stale === true,
    inputRevision: str(entry.input_revision),
    inputFingerprint: str(entry.input_fingerprint),
  }
}
