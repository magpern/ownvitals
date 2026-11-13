import { fireEvent, render, screen, within } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { HisScore } from '../../api/scores'
import { decodeHisScore } from '../../api/scores'
import { setLocale } from '../../i18n'
import { RouterProvider } from '../../router/Router'
import { ScoreCard } from './ScoreCard'
import { ScorePanel } from './ScorePanel'
import { bandWord } from './scorePanelModel'

// Synthetic score values only (nothing here is a real person's data).

const SIDE = { perfect: 25, good: 27, average: 30, bad: 35 }
const LOW = { perfect: 18.5, good: 17, average: 15, bad: 13 }

function entry(
  o: {
    status?: string
    value?: number | null
    reason?: string
    stale?: boolean
  } = {},
) {
  return {
    scores: [
      {
        kind: 'his',
        day: '2026-11-01',
        status: o.status ?? 'computed',
        value: o.value === undefined ? 71 : o.value,
        stale: o.stale ?? false,
        algo_version: 'his/2',
        input_revision: '123456789012345678',
        input_fingerprint: 'ab'.repeat(32),
        table_sha: 'cd'.repeat(32),
        components: {
          ...(o.reason === undefined ? {} : { reason: o.reason }),
          verticals: { body: 78, heart: 70, activity: null, sleep: null },
          vertical_reasons: {
            activity: 'insufficient_data',
            sleep: 'a_code_nobody_knows',
          },
          table: { id: 'withings_card_v1', version: '1', source: 'synthetic' },
          criteria: {
            bmi: {
              status: 'computed',
              vertical: 'body',
              unit: 'kg/m2',
              value: 22,
              score: 100,
              low: LOW,
              high: SIDE,
            },
            fat_ratio: {
              status: 'computed',
              vertical: 'body',
              unit: '%',
              value: 28,
              score: 62.5,
              low: null,
              high: SIDE,
            },
            night_hr: {
              status: 'not_available',
              vertical: 'heart',
              reason: 'no_band_row',
            },
            daily_steps: {
              status: 'insufficient_data',
              vertical: 'activity',
              reason: 'insufficient_data',
            },
            sleep_ahi: {
              status: 'not_available',
              vertical: 'sleep',
              reason: 'a_code_nobody_knows',
            },
          },
        },
      },
    ],
  }
}

function score(o?: Parameters<typeof entry>[0]): HisScore {
  const his = decodeHisScore(entry(o))
  if (his === null) throw new Error('no his')
  return his
}

beforeEach(async () => {
  await setLocale('en')
})

describe('bandWord', () => {
  it('names the band from the boundaries', () => {
    expect(bandWord(22, LOW, SIDE)).toBe('perfect')
    expect(bandWord(26, LOW, SIDE)).toBe('good')
    expect(bandWord(28, LOW, SIDE)).toBe('average')
    expect(bandWord(31, LOW, SIDE)).toBe('bad')
    expect(bandWord(16, LOW, SIDE)).toBe('average')
    expect(bandWord(12, LOW, SIDE)).toBe('bad')
    expect(bandWord(5, null, null)).toBeNull()
  })

  it('puts a value exactly on a boundary in the better band, on both sides', () => {
    expect(bandWord(25, LOW, SIDE)).toBe('perfect')
    expect(bandWord(18.5, LOW, SIDE)).toBe('perfect')
    expect(bandWord(27, LOW, SIDE)).toBe('good')
    expect(bandWord(17, LOW, SIDE)).toBe('good')
    expect(bandWord(30, LOW, SIDE)).toBe('average')
    expect(bandWord(15, LOW, SIDE)).toBe('average')
    expect(bandWord(35, LOW, SIDE)).toBe('bad')
    expect(bandWord(13, LOW, SIDE)).toBe('bad')
    expect(bandWord(30.01, null, SIDE)).toBe('bad')
    expect(bandWord(15, LOW, null)).toBe('average')
  })
})

describe('the panel of a computed score', () => {
  it('lists every criterion, the area lines, the tier mark, the source and the provenance', () => {
    render(<ScorePanel score={score()} onClose={() => {}} />)
    expect(screen.getByText('How your score became 71')).toBeInTheDocument()
    expect(screen.getByText('ownvitals 2.0')).toBeInTheDocument()
    expect(screen.getByText('Experimental')).toBeInTheDocument()
    const bmi = document.querySelector('[data-criterion="bmi"]') as HTMLElement
    expect(within(bmi).getByText('Perfect')).toBeInTheDocument()
    expect(within(bmi).getByText(/Perfect 18\.5–25 · Good 17–27 · Average 15–30 · Bad: below 13 or above 35/)).toBeInTheDocument()
    expect(within(bmi).getByText('100')).toBeInTheDocument()
    const fat = document.querySelector('[data-criterion="fat_ratio"]') as HTMLElement
    expect(within(fat).getByText('Average')).toBeInTheDocument()
    expect(within(fat).getByText('62.5')).toBeInTheDocument()
    expect(within(fat).getByText(/Bad: above 35$/)).toBeInTheDocument()
    expect(document.querySelectorAll('tbody tr')).toHaveLength(5)
    expect(screen.getByText('Body 78')).toBeInTheDocument()
    expect(screen.getByText('Activity n/a: not enough data yet')).toBeInTheDocument()
    expect(screen.getByText('Band table synthetic (withings_card_v1, version 1).')).toBeInTheDocument()
    expect(screen.getByText('123456789012345678')).toBeInTheDocument()
    expect(screen.getByText('abababab…abababab')).toBeInTheDocument()
    expect(screen.getByText('Current')).toBeInTheDocument()
    expect(
      screen.getByText(
        'This score compares your values with fixed bands for your age and sex; it is not a medical assessment.',
      ),
    ).toBeInTheDocument()
  })

  it('words a criterion without a value by its code, an unknown code generically', () => {
    render(<ScorePanel score={score()} onClose={() => {}} />)
    const hr = document.querySelector('[data-criterion="night_hr"]') as HTMLElement
    expect(within(hr).getByText('no band for your age or sex')).toBeInTheDocument()
    const ahi = document.querySelector('[data-criterion="sleep_ahi"]') as HTMLElement
    expect(within(ahi).getByText('not computed yet')).toBeInTheDocument()
  })

  it('says Stale for a row the data has moved past', () => {
    render(<ScorePanel score={score({ stale: true })} onClose={() => {}} />)
    expect(screen.getByText('Stale')).toBeInTheDocument()
  })

  it('shows the state and its sentence when there is no score, with the table kept', () => {
    render(
      <ScorePanel
        score={score({
          status: 'not_set',
          value: null,
          reason: 'birth_year_unset',
        })}
        onClose={() => {}}
      />,
    )
    expect(screen.getByText('Why there is no score yet')).toBeInTheDocument()
    expect(screen.getByText('set your birth year in your profile')).toBeInTheDocument()
    expect(document.querySelector('[data-criterion="bmi"]')).not.toBeNull()
  })

  it('shows the insufficient-data pill', () => {
    render(
      <ScorePanel
        score={score({
          status: 'insufficient_data',
          value: null,
          reason: 'insufficient_data',
        })}
        onClose={() => {}}
      />,
    )
    expect(screen.getAllByText('not enough data yet').length).toBeGreaterThan(0)
    expect(screen.getByText('Insufficient data')).toBeInTheDocument()
  })

  it('carries the ADR sentence, the table source and the Bad wording in Swedish', async () => {
    await setLocale('sv')
    render(<ScorePanel score={score()} onClose={() => {}} />)
    expect(
      screen.getByText(
        'Poängen jämför dina värden med fasta nivåer för din ålder och ditt kön; det är ingen medicinsk bedömning.',
      ),
    ).toBeInTheDocument()
    expect(screen.getByText('Nivåtabell synthetic (withings_card_v1, version 1).')).toBeInTheDocument()
    expect(screen.getByText(/Dåligt: under 13 eller över 35/)).toBeInTheDocument()
    expect(screen.getByText(/Dåligt: över 35$/)).toBeInTheDocument()
  })
})

describe('keyboard', () => {
  it('closes on Escape and the button, and gives the focus back to the link', () => {
    const close = vi.fn()
    const his = score()
    render(
      <RouterProvider>
        <ScoreCard score={his} openTo="/overview?score=his" />
      </RouterProvider>,
    )
    const { unmount } = render(<ScorePanel score={his} onClose={close} />)
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(close).toHaveBeenCalledTimes(1)
    fireEvent.click(screen.getByRole('button', { name: 'Close the health score' }))
    expect(close).toHaveBeenCalledTimes(2)
    unmount()
    expect(screen.getByRole('link', { name: "How it's computed" })).toHaveFocus()
  })
})
