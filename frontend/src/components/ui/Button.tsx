import type { ButtonHTMLAttributes } from 'react'
import { Icon } from './Icon'

// Buttons (docs/design/mockups/src/Main.dc.html, "Controls"). Targets are at least 44 px.
//
// `lock` marks an action on the step-up list (PLAN section 6, ADR 0003 section 4) and every admin
// write: it shows a lock, and its action runs through `useProtected()` (../StepUp.tsx), which
// opens the shared "Confirm it's you" dialog when the server asks for a fresh code.
// A destructive action's label ends with "..." because it opens a confirmation.

export type ButtonVariant = 'default' | 'primary' | 'danger' | 'danger-solid' | 'ghost'

interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: ButtonVariant
  lock?: boolean
  wide?: boolean
}

export function Button({
  variant = 'default',
  lock = false,
  wide = false,
  type = 'button',
  className,
  children,
  ...rest
}: ButtonProps) {
  const classes = ['ov-btn']
  if (variant !== 'default') classes.push(`ov-btn--${variant}`)
  if (wide) classes.push('ov-btn--wide')
  if (className !== undefined) classes.push(className)
  return (
    <button
      aria-haspopup={lock ? 'dialog' : undefined}
      {...rest}
      type={type}
      className={classes.join(' ')}
      data-lock={lock ? '' : undefined}
    >
      {lock ? <Icon name="lock" /> : null}
      {children}
    </button>
  )
}
