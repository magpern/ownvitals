import type { Finding } from '../../api/findings'
import type { NerveMetricMonth, NerveMonth } from '../../api/nerve'

// What the nerve health page decides, with no I/O (docs/design/README.md, "Nerve health"): which
// tiles it shows, which findings belong on it and how a month reads. Text and the clock are not here.

/** The tiles of the page, in order; each is the overview's tile. */
export const NERVE_KEYS = ['nerve_health_score', 'nerve_response_score', 'esc'] as const

/** N2, N3 and N5. N1 is not a finding; N4 is not in v1, so nothing of it is ever shown. */
const RULES = ['N2', 'N3', 'N5']

/** The findings of the page; a rule id may carry a variant (`N2:guided`). */
export function nerveFindings(findings: readonly Finding[]): Finding[] {
  return findings.filter((f) => RULES.includes(f.ruleId.split(':')[0] ?? ''))
}

/** A metric's row of a month, or `null` when the month has none. */
export function monthMetric(month: NerveMonth, key: string): NerveMetricMonth | null {
  return month.metrics.find((m) => m.metricKey === key) ?? null
}

/** Whether a month has a day with data for any of the page's metrics. */
export function hasReadings(month: NerveMonth): boolean {
  return NERVE_KEYS.some((k) => (monthMetric(month, k)?.daysWithData ?? 0) > 0)
}
