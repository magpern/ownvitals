import { describe, expect, it } from 'vitest'
import { sourceTag } from './ScoreCard'

describe('sourceTag', () => {
  it('names a listed algorithm version from the table', () => {
    expect(sourceTag('his/1')).toBe('ownvitals 1.0')
  })

  it('falls back to the raw version string', () => {
    expect(sourceTag('his/2')).toBe('ownvitals his/2')
  })
})
