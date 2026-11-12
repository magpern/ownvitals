import { describe, expect, it } from 'vitest'
import { DecodeError } from './decode'
import { decodeHisScore } from './scores'

// Synthetic API answers only.

describe('decodeHisScore', () => {
  it('is null when the API lists no his entry', () => {
    expect(decodeHisScore({ scores: [] })).toBeNull()
    expect(
      decodeHisScore({
        scores: [{ kind: 'body_path:weight', status: 'computed' }],
      }),
    ).toBeNull()
  })

  it('reads the value, status and areas', () => {
    const his = decodeHisScore({
      scores: [
        {
          kind: 'his',
          algo_version: 'his/1',
          status: 'computed',
          value: 71,
          components: {
            verticals: { body: 78, sleep: null },
            vertical_reasons: { body: null, sleep: 'insufficient_data', heart: '', activity: 7 },
          },
        },
      ],
    })
    expect(his).toEqual({
      algoVersion: 'his/1',
      status: 'computed',
      value: 71,
      reason: null,
      areas: { body: 78, heart: null, activity: null, sleep: null },
      areaReasons: { body: null, heart: null, activity: null, sleep: 'insufficient_data' },
    })
  })

  it('keeps a missing value null and carries the reason', () => {
    const his = decodeHisScore({
      scores: [
        {
          kind: 'his',
          algo_version: 'his/1',
          status: 'not_set',
          value: null,
          components: { reason: 'no_band_table' },
        },
      ],
    })
    expect(his).toMatchObject({
      status: 'not_set',
      value: null,
      reason: 'no_band_table',
    })
  })

  it('refuses a body without a scores list', () => {
    expect(() => decodeHisScore({})).toThrow(DecodeError)
  })
})
