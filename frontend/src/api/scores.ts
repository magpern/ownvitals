import { isObject, list, object, optionalText, text, type JsonObject } from './decode'

// GET /api/scores (backend/src/ownvitals/views/scores_routes.py, ADR 0012). Only what the overview
// readout shows is read: the Health Index Score (`his`), its status, its value and its areas.
// The server lists `his` only while the owner-only switch is on, so an absent entry means "show
// nothing". A value that may be missing stays `null`.

export const HIS_AREAS = ['body', 'heart', 'activity', 'sleep'] as const
export type HisArea = (typeof HIS_AREAS)[number]

export interface HisScore {
  /** `computed`, `insufficient_data`, `not_available`, `not_supported` or `not_set`. */
  status: string
  /** The algorithm version, `his/1`. */
  algoVersion: string
  /** Set only when computed. */
  value: number | null
  /** A short reason code when there is no value (`one_vertical`, `birth_year_unset` ...). */
  reason: string | null
  /** Each area's integer score, or `null` for "n/a". */
  areas: Record<HisArea, number | null>
}

function areaScore(verticals: JsonObject, key: string): number | null {
  const v = verticals[key]
  return typeof v === 'number' && Number.isFinite(v) ? v : null
}

function decodeHis(entry: JsonObject): HisScore {
  const raw = entry.value
  const components = isObject(entry.components) ? entry.components : {}
  const verticals = isObject(components.verticals) ? components.verticals : {}
  return {
    status: text(entry, 'status'),
    algoVersion: text(entry, 'algo_version'),
    value: typeof raw === 'number' && Number.isFinite(raw) ? raw : null,
    reason: optionalText(components, 'reason'),
    areas: {
      body: areaScore(verticals, 'body'),
      heart: areaScore(verticals, 'heart'),
      activity: areaScore(verticals, 'activity'),
      sleep: areaScore(verticals, 'sleep'),
    },
  }
}

/** The user's `his` score, or `null` when the API sends none (flag off, nothing computed yet). */
export function decodeHisScore(raw: unknown): HisScore | null {
  const body = object(raw, 'scores')
  const entry = list(body, 'scores')
    .map((s) => object(s, 'score'))
    .find((s) => s.kind === 'his')
  return entry === undefined ? null : decodeHis(entry)
}
