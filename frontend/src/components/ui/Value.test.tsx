import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { Mono, Num, Value, ValueCard } from './Value'

// The value styles: a number and its unit, a raw value in mono, and the estimate that never looks
// like a certified value (docs/design/mockups/src/Main.dc.html).

describe('Value', () => {
  it('shows the number and its unit, as two parts', () => {
    const { container } = render(<Value value="78.4" unit="kg" />)
    expect(container.querySelector('.ov-value__num')).toHaveTextContent('78.4')
    expect(container.querySelector('.ov-value__unit')).toHaveTextContent('kg')
    expect(container.firstElementChild).toHaveClass('ov-value')
    expect(container.firstElementChild).not.toHaveClass('ov-value--large', 'ov-value--estimate')
  })

  it('shows a number with no unit (an index, a score)', () => {
    const { container } = render(<Value value="7" />)
    expect(container.querySelector('.ov-value__unit')).toBeNull()
    expect(container).toHaveTextContent('7')
  })

  it('keeps a range as given', () => {
    render(<Value value="77.6–79.3" unit="kg" size="large" />)
    expect(screen.getByText('77.6–79.3')).toBeInTheDocument()
  })

  it('has a large size for a headline value', () => {
    const { container } = render(<Value value="401" unit="ms" size="large" />)
    expect(container.firstElementChild).toHaveClass('ov-value', 'ov-value--large')
  })

  it('marks an estimate: "est." in front, and its own style, so it never reads as a certified value', () => {
    const { container } = render(<Value value="41" unit="ms" estimated />)
    expect(container.firstElementChild).toHaveClass('ov-value--estimate')
    expect(container.firstElementChild).toHaveTextContent(/^est\. 41ms$/)
    expect(container.querySelector('.ov-value__est')).toHaveTextContent('est.')
    const certified = render(<Value value="41" unit="ms" />)
    expect(certified.container).not.toHaveTextContent('est.')
  })

  it('uses a class for every style, not an inline style', () => {
    const { container } = render(<Value value="41" unit="ms" size="large" estimated />)
    expect(container.querySelector('[style]')).toBeNull()
  })
})

describe('Num and Mono', () => {
  it('give a number tabular figures', () => {
    render(<Num>1,234</Num>)
    expect(screen.getByText('1,234')).toHaveClass('ov-num')
  })

  it('set ids, versions and raw values in mono', () => {
    render(<Mono>grp 4812 · daily 1.0.0 · rev 4812</Mono>)
    expect(screen.getByText('grp 4812 · daily 1.0.0 · rev 4812')).toHaveClass('ov-mono')
  })
})

describe('ValueCard', () => {
  it('says who produced a reported value, and is not boxed', () => {
    const { container } = render(<ValueCard source="reported" detail="Body Scan" value="401" unit="ms" label="QTc" />)
    expect(container).toHaveTextContent('Reported by Withings · Body Scan')
    expect(container.querySelector('.ov-card--experimental')).toBeNull()
    expect(container.querySelector('.ov-value--estimate')).toBeNull()
  })

  it('says a calculated value is ownvitals’s', () => {
    const { container } = render(<ValueCard source="calculated" detail="analysis 1.2" value="77.6–79.3" unit="kg" label="Typical range, 90 days">How it was made</ValueCard>)
    expect(container).toHaveTextContent('Calculated by ownvitals · analysis 1.2')
    expect(container).toHaveTextContent('How it was made')
    expect(container.querySelector('.ov-card--experimental')).toBeNull()
  })

  it('shows an estimate italic, dotted, with "est.", in a dashed box and its own source line', () => {
    const { container } = render(<ValueCard source="estimated" detail="experimental · only you see this" value="41" unit="ms" label="RMSSD from the ECG signal" />)
    expect(container).toHaveTextContent('Estimated by ownvitals · experimental · only you see this')
    expect(container.querySelector('.ov-card--experimental')).not.toBeNull()
    expect(container.querySelector('.ov-value--estimate')).toHaveTextContent('est. 41')
  })
})
