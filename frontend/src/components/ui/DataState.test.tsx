import { render, screen, within } from '@testing-library/react'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { describe, expect, it } from 'vitest'
import { ROOT } from '../../test/sourceScan'
import { DATA_STATE_KINDS, DataState, StatePill, dataStateLabel, type DataStateKind } from './DataState'

// The data states of the design (docs/design/mockups/src/Main.dc.html, "Data states"): one
// component and one wording for the ten of them.

const WORDING: Record<DataStateKind, string> = {
  'up-to-date': 'Up to date',
  stale: 'Stale',
  importing: 'Importing 64%',
  'incomplete-import': 'Incomplete import',
  'insufficient-data': 'Insufficient data',
  'not-supported': 'Not supported',
  'not-shared': 'Not shared',
  'not-available': 'Not available',
  'needs-reconnect': 'Needs reconnect',
  'withings-plus': 'Withings+ needed',
}

// The tone each state has in the mock-up: its class, and the dot in front of it, if any.
const LOOK: Record<DataStateKind, { tone: string; dot: string | null }> = {
  'up-to-date': { tone: 'ov-state--ok', dot: 'ov-dot--ok' },
  stale: { tone: 'ov-state--attn', dot: 'ov-dot--attn' },
  importing: { tone: 'ov-state', dot: null },
  'incomplete-import': { tone: 'ov-state--attn', dot: null },
  'insufficient-data': { tone: 'ov-state', dot: null },
  'not-supported': { tone: 'ov-state--off', dot: null },
  'not-shared': { tone: 'ov-state--off', dot: null },
  'not-available': { tone: 'ov-state--off', dot: null },
  'needs-reconnect': { tone: 'ov-state--urgent', dot: 'ov-dot--urgent' },
  'withings-plus': { tone: 'ov-state--info', dot: null },
}

function pill(kind: DataStateKind) {
  return kind === 'importing' ? <DataState kind={kind} progress={64} /> : <DataState kind={kind} />
}

describe('DataState', () => {
  it('knows the ten states of the design', () => {
    expect([...DATA_STATE_KINDS].sort()).toEqual(Object.keys(WORDING).sort())
    expect(DATA_STATE_KINDS).toHaveLength(10)
  })

  it.each(DATA_STATE_KINDS)('shows %s with the design\'s wording and tone', (kind) => {
    const { container } = render(pill(kind))
    const shown = container.querySelector('.ov-state')
    expect(shown).toHaveTextContent(new RegExp(`^${WORDING[kind].replace('+', '\\+')}$`))
    expect(shown).toHaveClass(LOOK[kind].tone)
    const dot = shown?.querySelector('.ov-dot')
    if (LOOK[kind].dot === null) expect(dot).toBeNull()
    else expect(dot).toHaveClass(LOOK[kind].dot)
    // The dot is decoration: the wording says it.
    if (dot) expect(dot).toHaveAttribute('aria-hidden', 'true')
  })

  it('has the wording of the mock-up, word for word', () => {
    const mockup = readFileSync(join(ROOT, '..', 'docs/design/mockups/src/Main.dc.html'), 'utf8')
    for (const kind of DATA_STATE_KINDS) {
      const label = dataStateLabel(kind, 64)
      expect(mockup, label).toContain(`${label}</span>`)
    }
  })

  it('gives the importing percentage in the label and the bar, as whole numbers from 0 to 100', () => {
    expect(dataStateLabel('importing', 64.4)).toBe('Importing 64%')
    expect(dataStateLabel('importing', 64.6)).toBe('Importing 65%')
    expect(dataStateLabel('importing', 0)).toBe('Importing 0%')
    expect(dataStateLabel('importing', 250)).toBe('Importing 100%')
    expect(dataStateLabel('importing', -3)).toBe('Importing 0%')
    expect(dataStateLabel('importing', Number.NaN)).toBe('Importing 0%')
    expect(dataStateLabel('stale', 64)).toBe('Stale')
    render(<DataState kind="importing" progress={120} />)
    expect(screen.getByRole('progressbar', { name: 'Import progress' })).toHaveAttribute('value', '100')
  })

  it('draws a bar for importing and for insufficient data with progress, and for no other state', () => {
    const { container, rerender } = render(<DataState kind="importing" progress={64} />)
    const bar = screen.getByRole('progressbar', { name: 'Import progress' })
    expect(bar).toHaveAttribute('value', '64')
    expect(bar).toHaveAttribute('max', '100')
    rerender(<DataState kind="insufficient-data" progress={75} />)
    expect(screen.getByRole('progressbar', { name: 'Progress toward the minimum' })).toHaveAttribute('value', '75')
    rerender(<DataState kind="insufficient-data" />)
    expect(container.querySelector('progress')).toBeNull()
    for (const kind of DATA_STATE_KINDS.filter((k) => k !== 'importing' && k !== 'insufficient-data')) {
      rerender(<DataState kind={kind}>Some detail</DataState>)
      expect(container.querySelector('progress'), kind).toBeNull()
    }
  })

  it('shows its detail under the pill, and a bare pill when there is none', () => {
    const { container, rerender } = render(<DataState kind="stale" />)
    expect(container.querySelector('.ov-data-state')).toBeNull()
    expect(container.firstElementChild).toHaveClass('ov-state')
    rerender(<DataState kind="stale">No weigh-in for 14 days. The last value is shown with its date, greyed.</DataState>)
    const block = container.querySelector('.ov-data-state')
    expect(block).not.toBeNull()
    expect(within(block as HTMLElement).getByText('Stale')).toBeInTheDocument()
    expect(within(block as HTMLElement).getByText(/No weigh-in for 14 days/)).toBeInTheDocument()
  })

  it('needs a progress for importing (a type error otherwise) and says only "Importing" without one', () => {
    // @ts-expect-error importing has to say how far it is
    render(<DataState kind="importing" />)
    expect(screen.getByText('Importing')).toBeInTheDocument()
    expect(document.querySelector('progress')).toBeNull()
  })

  it('has a bar with no inline style: its width is the value of a progress element', () => {
    const { container } = render(<DataState kind="importing" progress={64}>ECG history: 142 of 220 recordings.</DataState>)
    expect(container.querySelector('[style]')).toBeNull()
  })
})

describe('StatePill', () => {
  it('is the pill of "Coming later" too: dashed, with no dot', () => {
    const { container } = render(<StatePill tone="off">Coming later</StatePill>)
    expect(container.firstElementChild).toHaveClass('ov-state', 'ov-state--off')
    expect(container.querySelector('.ov-dot')).toBeNull()
  })
})
