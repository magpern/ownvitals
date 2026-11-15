import type { MessageKey } from '../../i18n/en'
import type { IntervalKey } from '../../api/ecg'

// What the screens call Withings' values. A documented code has its meaning (withings-findings
// section 3); an undocumented one is "Withings code n", never given a meaning; a missing value is
// "not reported", never 0 or "normal". A reason code of ours is never a label: `reasonKey` maps
// the ones the gate can give to words and returns null for the rest.

const AFIB: Record<number, MessageKey> = {
  0: 'ecg.afib.0',
  1: 'ecg.afib.1',
  2: 'ecg.afib.2',
}

const DETAILS: Record<number, MessageKey> = {
  [-1]: 'ecg.details.-1',
  0: 'ecg.details.0',
  1: 'ecg.details.1',
  2: 'ecg.details.2',
  5: 'ecg.details.5',
  6: 'ecg.details.6',
  7: 'ecg.details.7',
  9: 'ecg.details.9',
  10: 'ecg.details.10',
  11: 'ecg.details.11',
  12: 'ecg.details.12',
  13: 'ecg.details.13',
}

export const afibKey = (code: number): MessageKey | null => AFIB[code] ?? null
export const detailsKey = (code: number): MessageKey | null => DETAILS[code] ?? null

/** E1: a positive classification is shown in urgent red. */
export const isPositive = (code: number | null): boolean => code === 1

export const INTERVAL_LABELS: Record<IntervalKey, MessageKey> = {
  qrs_duration: 'ecg.interval.qrs',
  pr_interval: 'ecg.interval.pr',
  qt_interval: 'ecg.interval.qt',
  qtc_interval: 'ecg.interval.qtc',
}

const REASONS: Record<string, MessageKey> = {
  too_short: 'ecg.reason.too_short',
  flat_line: 'ecg.reason.flat_line',
  amplitude_out_of_range: 'ecg.reason.amplitude',
  clipping: 'ecg.reason.clipping',
  detector_disagreement: 'ecg.reason.detectors',
  implausible_rate: 'ecg.reason.rate',
  beat_gap: 'ecg.reason.gap',
  low_template_correlation: 'ecg.reason.template',
}

export const reasonKey = (code: string): MessageKey | null => REASONS[code] ?? null

export type GateState = 'pass' | 'fail' | 'not_evaluated'
export const gateState = (gate: string | null): GateState => (gate === 'pass' ? 'pass' : gate === 'fail' ? 'fail' : 'not_evaluated')

export const GATE_KEYS: Record<GateState, MessageKey> = {
  pass: 'ecg.gate.pass',
  fail: 'ecg.gate.fail',
  not_evaluated: 'ecg.gate.not_evaluated',
}
