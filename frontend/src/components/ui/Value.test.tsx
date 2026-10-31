import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { Mono, Num, Value } from './Value'

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
