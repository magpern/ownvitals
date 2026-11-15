import { describe, expect, it } from 'vitest'
import { decodeRecordingDetail } from './ecg'
import { syntheticDetail, syntheticEstimate, syntheticSignal } from '../test/ecgFixtures'

describe('decodeRecordingDetail (synthetic payloads)', () => {
  it('has no estimate when the API sends null or omits it', () => {
    expect(decodeRecordingDetail(syntheticDetail(null)).estimate).toBeNull()
    const without: Record<string, unknown> = { ...syntheticDetail(null) }
    delete without.ownvitals_estimate
    expect(decodeRecordingDetail(without).estimate).toBeNull()
  })

  it('keeps the estimate when the API sends it', () => {
    expect(decodeRecordingDetail(syntheticDetail(syntheticEstimate())).estimate?.gate).toBe('pass')
  })

  it('has no signal for an unusable sampling rate', () => {
    const detail = { ...syntheticDetail(), signal: { ...syntheticSignal(), sampling_hz: 0 } }
    expect(decodeRecordingDetail(detail).signal).toBeNull()
  })

  it('reads reported intervals and leaves a missing one out', () => {
    const { recording } = decodeRecordingDetail(syntheticDetail())
    expect(recording.intervals.qrs_duration).toBe(94)
    expect(recording.intervals.qt_interval).toBeUndefined()
  })
})
