import { render, screen, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { Card, ComingLater, Stack } from './Card'

// The card and the stack of the design, and the "Coming later" row (docs/design/mockups/src/ov.css
// and Security.dc.html).

describe('Card', () => {
  it('is a plain box with no title', () => {
    const { container } = render(<Card>Inside</Card>)
    expect(container.firstElementChild).toHaveClass('ov-card')
    expect(container.firstElementChild?.tagName).toBe('DIV')
    expect(container).toHaveTextContent('Inside')
    expect(screen.queryByRole('heading')).toBeNull()
  })

  it('is a section named by its heading, level 2 by default', () => {
    render(<Card title="How you sign in">Inside</Card>)
    const section = screen.getByRole('region', { name: 'How you sign in' })
    expect(within(section).getByRole('heading', { level: 2, name: 'How you sign in' })).toBeInTheDocument()
    expect(section).toHaveTextContent('Inside')
  })

  it('has a level 3 heading inside a section that has a level 2', () => {
    render(<Card title="Weight" level={3} />)
    expect(screen.getByRole('heading', { level: 3, name: 'Weight' })).toHaveClass('ov-card__title--small')
  })

  it('shows what is beside the title', () => {
    render(<Card title="Second factor" aside={<span>On · required</span>} />)
    expect(screen.getByText('On · required')).toBeInTheDocument()
  })

  it('can be flat, and can be an experimental estimate in a dashed box', () => {
    const { container, rerender } = render(<Card flat>x</Card>)
    expect(container.firstElementChild).toHaveClass('ov-card', 'ov-card--flat')
    rerender(<Card experimental>x</Card>)
    expect(container.firstElementChild).toHaveClass('ov-card', 'ov-card--experimental')
    expect(container.firstElementChild).not.toHaveClass('ov-card--flat')
  })

  it('gives two cards two different heading ids', () => {
    render(
      <>
        <Card title="One" />
        <Card title="Two" />
      </>,
    )
    expect(screen.getByRole('region', { name: 'One' })).toBeInTheDocument()
    expect(screen.getByRole('region', { name: 'Two' })).toBeInTheDocument()
  })
})

describe('Stack', () => {
  it('is a column with the default gap, and a smaller or a larger one', () => {
    const { container, rerender } = render(<Stack>a</Stack>)
    expect(container.firstElementChild).toHaveClass('ov-stack')
    expect(container.firstElementChild).not.toHaveAttribute('data-gap')
    rerender(<Stack gap="sm">a</Stack>)
    expect(container.firstElementChild).toHaveAttribute('data-gap', 'sm')
    rerender(<Stack gap="lg">a</Stack>)
    expect(container.firstElementChild).toHaveAttribute('data-gap', 'lg')
  })

  it('holds its children in order', () => {
    render(
      <Stack>
        <p>first</p>
        <p>second</p>
      </Stack>,
    )
    expect(screen.getAllByText(/first|second/).map((p) => p.textContent)).toEqual(['first', 'second'])
  })
})

describe('ComingLater', () => {
  it('says what the screen is, that it is not built yet, and the line the screen has', () => {
    const { container } = render(<ComingLater label="Passkeys">Touch ID, Windows Hello or a security key.</ComingLater>)
    expect(container.querySelector('.ov-setting__label')).toHaveTextContent('Passkeys')
    expect(screen.getByText('Touch ID, Windows Hello or a security key.')).toBeInTheDocument()
    const pill = screen.getByText('Coming later')
    // The dashed pill of a stream or a screen that is not there (the data states' "off" look).
    expect(pill).toHaveClass('ov-state', 'ov-state--off')
  })
})
