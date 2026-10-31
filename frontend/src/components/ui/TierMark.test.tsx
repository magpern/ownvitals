import { render, screen } from '@testing-library/react'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { describe, expect, it } from 'vitest'
import { ROOT } from '../../test/sourceScan'
import { SourceMark, TIER_LABELS, TierGlyph, TierMark, type Source, type Tier } from './TierMark'

// The tier marks (Device, Trend, Experimental, System) and the source marks of the three value
// styles, as in docs/design/mockups/src/Main.dc.html.

const TIERS: Tier[] = ['device', 'trend', 'experimental', 'system']

describe('TierMark', () => {
  it('names the four tiers as the design does', () => {
    expect(TIER_LABELS).toEqual({ device: 'Device', trend: 'Trend', experimental: 'Experimental', system: 'System' })
    const mockup = readFileSync(join(ROOT, '..', 'docs/design/mockups/src/Main.dc.html'), 'utf8')
    for (const label of Object.values(TIER_LABELS)) expect(mockup, label).toContain(`</svg>${label}</span>`)
  })

  it.each(TIERS)('shows the %s badge: its name, its own class and a glyph that is not read out', (tier) => {
    const { container } = render(<TierMark tier={tier} />)
    expect(container.firstElementChild).toHaveClass('ov-tier', `ov-tier--${tier}`)
    expect(screen.getByText(TIER_LABELS[tier])).toBeInTheDocument()
    const glyph = container.querySelector('svg')
    expect(glyph).toHaveAttribute('aria-hidden', 'true')
    expect(glyph).toHaveAttribute('viewBox', '0 0 12 12')
  })

  it('draws each tier with its own shape: a solid square, a band with a dot, a dashed diamond, a ring', () => {
    const shapes = (tier: Tier) => {
      const { container } = render(<TierGlyph tier={tier} />)
      return [...container.querySelectorAll('svg > *')].map((el) => `${el.localName}.${el.getAttribute('class') ?? ''}`)
    }
    expect(shapes('device')).toEqual(['rect.ov-glyph-solid'])
    expect(shapes('trend')).toEqual(['rect.ov-glyph-soft', 'circle.ov-glyph-solid'])
    expect(shapes('experimental')).toEqual(['path.ov-glyph-dashed'])
    expect(shapes('system')).toEqual(['circle.ov-ic'])
  })

  it('has no colour of its own: the glyphs take the text colour', () => {
    const { container } = render(
      <>
        {TIERS.map((tier) => (
          <TierMark key={tier} tier={tier} />
        ))}
      </>,
    )
    expect(container.querySelector('[fill], [stroke], [style]')).toBeNull()
  })
})

describe('SourceMark', () => {
  const cases: [Source, string, Tier][] = [
    ['withings', 'Reported by Withings', 'device'],
    ['ownvitals', 'Calculated by ownvitals', 'trend'],
    ['estimate', 'Estimated by ownvitals', 'experimental'],
  ]

  it.each(cases)('says who produced a value: %s', (source, label, tier) => {
    const { container } = render(<SourceMark source={source} />)
    expect(container.firstElementChild).toHaveTextContent(new RegExp(`^${label}$`))
    // The same glyph as the tier of that kind.
    const expected = render(<TierGlyph tier={tier} />).container.innerHTML
    expect(container.querySelector('svg')?.outerHTML).toBe(expected)
  })

  it('adds the device or version after a dot', () => {
    render(<SourceMark source="withings" detail="Body Scan" />)
    expect(screen.getByText('Reported by Withings · Body Scan')).toBeInTheDocument()
    render(<SourceMark source="ownvitals" detail="analysis 1.2" />)
    expect(screen.getByText('Calculated by ownvitals · analysis 1.2')).toBeInTheDocument()
  })

  it('sets an estimate apart', () => {
    const { container } = render(<SourceMark source="estimate" detail="experimental · only you see this" />)
    expect(container.firstElementChild).toHaveClass('ov-src', 'ov-src--estimate')
    expect(screen.getByText('Estimated by ownvitals · experimental · only you see this')).toBeInTheDocument()
    const plain = render(<SourceMark source="withings" />)
    expect(plain.container.firstElementChild).not.toHaveClass('ov-src--estimate')
  })
})
