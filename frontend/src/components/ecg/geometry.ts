// The geometry of the ECG strip, pure (no canvas, no DOM). Paper standard: 25 mm/s and 10 mm/mV
// (ADR 0006 section 3). One pixel size per millimetre, `PX_PER_MM` times the zoom, applies to
// both axes, so zooming never changes what a millimetre means. Gain is a labelled choice (10 or
// 5 mm/mV), never automatic.

export const PX_PER_MM = 4
export const MM_PER_SECOND = 25
export const NOMINAL_GAIN = 10
export const ROW_SECONDS = 10
/** The calibration pulse: 1 mV high, 200 ms (5 mm) wide. */
export const PULSE_MV = 1
export const PULSE_SECONDS = 0.2
/** Room on the left of a row for the pulse and a gap. */
export const LEAD_IN_MM = 10
export const MIN_HEIGHT_MM = 40
const MARGIN_MM = 5

export interface Scale {
  pxPerMm: number
  /** Millimetres per millivolt. */
  gain: number
}

export function scaleOf(zoom: number, gain: number): Scale {
  return { pxPerMm: PX_PER_MM * zoom, gain }
}

/** Horizontal position in px of a time in seconds from the start of the row. */
export function xOf(scale: Scale, seconds: number): number {
  return (LEAD_IN_MM + seconds * MM_PER_SECOND) * scale.pxPerMm
}

/** Vertical offset in px from the baseline; up is positive millivolts. */
export function yOffset(scale: Scale, millivolts: number): number {
  return -millivolts * scale.gain * scale.pxPerMm
}

export function rowWidth(scale: Scale, seconds = ROW_SECONDS): number {
  return Math.ceil(xOf(scale, seconds) + 2 * scale.pxPerMm)
}

/**
 * Row height in whole 5 mm squares, tall enough for the largest excursion of the whole recording
 * at this gain, so a trace is never clipped, whatever the window.
 */
export function rowHeightMm(peakMillivolts: number, gain: number): number {
  const half = peakMillivolts * gain + MARGIN_MM
  return Math.max(MIN_HEIGHT_MM, Math.ceil((2 * half) / 5) * 5)
}

export function rowHeight(scale: Scale, peakMillivolts: number): number {
  return rowHeightMm(peakMillivolts, scale.gain) * scale.pxPerMm
}

export function peakOf(millivolts: readonly number[]): number {
  let peak = 0
  for (const v of millivolts) if (Math.abs(v) > peak) peak = Math.abs(v)
  return peak
}

export const toMillivolts = (microvolts: readonly number[]): number[] => microvolts.map((v) => v / 1000)
