import { render, screen } from '@testing-library/react'
import { beforeEach, describe, expect, it } from 'vitest'
import type { HisScore } from '../../api/scores'
import { setLocale } from '../../i18n'
import { ScoreCard, reasonKey, sourceTag } from './ScoreCard'

// Synthetic score values only.

function score(reasons: HisScore['areaReasons']): HisScore {
  return {
    status: 'computed',
    algoVersion: 'his/2',
    value: 71,
    reason: null,
    areas: { body: null, heart: 70, activity: null, sleep: null },
    areaReasons: reasons,
  }
}

beforeEach(async () => {
  await setLocale('en')
})

describe('sourceTag', () => {
  it('names a listed algorithm version from the table', () => {
    expect(sourceTag('his/2')).toBe('ownvitals 2.0')
  })

  it('falls back to the raw version string', () => {
    expect(sourceTag('his/9')).toBe('ownvitals his/9')
  })
})

describe('the reason of an area without a score', () => {
  it('words each area its own code in English', () => {
    render(
      <ScoreCard score={score({ body: 'no_height', heart: null, activity: 'insufficient_data', sleep: 'not_supported' })} />,
    )
    expect(
      screen.getByText(
        'Body: no height recorded yet · Activity: not enough data yet · Sleep: not supported by this account',
      ),
    ).toBeInTheDocument()
  })

  it('words it in Swedish', async () => {
    await setLocale('sv')
    render(<ScoreCard score={score({ body: 'no_height', heart: null, activity: null, sleep: null })} />)
    expect(screen.getByText(/Kropp: ingen längd registrerad ännu/)).toBeInTheDocument()
  })

  it('reads an unknown or missing code with the generic text', () => {
    expect(reasonKey('some_future_code')).toBe('overview.score.reason.fallback')
    render(<ScoreCard score={score({ body: 'some_future_code', heart: null, activity: null, sleep: null })} />)
    expect(screen.getByText(/Body: not computed yet · Activity: not computed yet/)).toBeInTheDocument()
  })
})
