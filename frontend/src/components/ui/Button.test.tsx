import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { Button, type ButtonVariant } from './Button'

// The buttons of the design (docs/design/mockups/src/Main.dc.html, "Controls"), with the lock
// variant of the actions that ask for a fresh authenticator code. The 44 px target is checked on
// the stylesheet in src/styles/tokens.test.ts.

describe('Button', () => {
  it('is a plain button that does not submit a form', () => {
    render(<Button>Sync now</Button>)
    const button = screen.getByRole('button', { name: 'Sync now' })
    expect(button).toHaveAttribute('type', 'button')
    expect(button).toHaveClass('ov-btn')
    expect(button.className).toBe('ov-btn')
  })

  it.each<[ButtonVariant, string]>([
    ['primary', 'ov-btn--primary'],
    ['danger', 'ov-btn--danger'],
    ['danger-solid', 'ov-btn--danger-solid'],
    ['ghost', 'ov-btn--ghost'],
  ])('has the %s look', (variant, className) => {
    render(<Button variant={variant}>Go</Button>)
    expect(screen.getByRole('button')).toHaveClass('ov-btn', className)
  })

  it('can be as wide as its container', () => {
    render(<Button wide>Sign in</Button>)
    expect(screen.getByRole('button')).toHaveClass('ov-btn--wide')
  })

  it('calls onClick, and not when it is disabled', () => {
    const onClick = vi.fn()
    const { rerender } = render(<Button onClick={onClick}>Sync now</Button>)
    fireEvent.click(screen.getByRole('button'))
    expect(onClick).toHaveBeenCalledTimes(1)
    rerender(
      <Button onClick={onClick} disabled>
        Sync now
      </Button>,
    )
    fireEvent.click(screen.getByRole('button'))
    expect(onClick).toHaveBeenCalledTimes(1)
  })

  it('submits a form when it is told to', () => {
    const onSubmit = vi.fn((event: { preventDefault: () => void }) => {
      event.preventDefault()
    })
    render(
      <form onSubmit={onSubmit}>
        <Button type="submit">Confirm</Button>
      </form>,
    )
    fireEvent.click(screen.getByRole('button'))
    expect(onSubmit).toHaveBeenCalledTimes(1)
  })

  describe('with a lock', () => {
    it('shows the lock icon, and says it opens a dialog', () => {
      const { container } = render(
        <Button lock variant="danger">
          Disconnect…
        </Button>,
      )
      const button = screen.getByRole('button', { name: 'Disconnect…' })
      expect(button).toHaveAttribute('aria-haspopup', 'dialog')
      expect(button).toHaveAttribute('data-lock')
      const icon = container.querySelector('svg')
      expect(icon).toHaveAttribute('aria-hidden', 'true')
      expect(icon).toHaveAttribute('viewBox', '0 0 20 20')
      // The icon is decoration: the name is the label alone.
      expect(button).toHaveAccessibleName('Disconnect…')
    })

    it('is a button like any other: it is clicked, and the dialog is the page\'s to open', () => {
      const onClick = vi.fn()
      render(
        <Button lock onClick={onClick}>
          Change…
        </Button>,
      )
      fireEvent.click(screen.getByRole('button', { name: 'Change…' }))
      expect(onClick).toHaveBeenCalledTimes(1)
    })

    it('is not marked when it has no lock', () => {
      const { container } = render(<Button>Sync now</Button>)
      expect(container.querySelector('svg')).toBeNull()
      expect(screen.getByRole('button')).not.toHaveAttribute('aria-haspopup')
      expect(screen.getByRole('button')).not.toHaveAttribute('data-lock')
    })
  })
})
