import { useRef, type ClipboardEvent, type KeyboardEvent } from 'react'

// The six boxes of an authenticator code (docs/design/mockups/src/Auth.dc.html and
// Security.dc.html, `.otp`): one digit per box. Typing moves to the next box, Backspace on an
// empty box goes back, and pasting a whole code fills them all. The value is the digits typed so
// far, at most six.

export const CODE_LENGTH = 6

interface CodeInputProps {
  value: string
  onChange: (value: string) => void
  /** The group's accessible name. */
  label?: string
  /** Its id, for a `<label htmlFor>` (the setup page's "Code from the app"). */
  id?: string
  autoFocus?: boolean
  disabled?: boolean
}

function digitsOf(text: string): string {
  return text.replace(/\D/g, '').slice(0, CODE_LENGTH)
}

export function CodeInput({ value, onChange, label = '6-digit code', id, autoFocus = false, disabled }: CodeInputProps) {
  const boxes = useRef<(HTMLInputElement | null)[]>([])
  const focus = (index: number) => {
    boxes.current[Math.max(0, Math.min(index, CODE_LENGTH - 1))]?.focus()
  }

  // The value has no holes: a digit typed in a box past the end goes to the end.
  const setDigit = (index: number, typed: string) => {
    const at = Math.min(index, value.length)
    const digits = digitsOf(typed)
    if (digits === '') {
      onChange(value.slice(0, at))
      return
    }
    // Several digits at once (autofill, a fast typist) fill from this box on.
    const next = digitsOf(value.slice(0, at) + digits + value.slice(at + digits.length))
    onChange(next)
    focus(at + digits.length)
  }

  const onKey = (index: number) => (event: KeyboardEvent<HTMLInputElement>) => {
    if (event.key === 'Backspace' && (value[index] ?? '') === '' && index > 0) {
      event.preventDefault()
      onChange(value.slice(0, index - 1))
      focus(index - 1)
    } else if (event.key === 'ArrowLeft') {
      focus(index - 1)
    } else if (event.key === 'ArrowRight') {
      focus(index + 1)
    }
  }

  const onPaste = (event: ClipboardEvent<HTMLInputElement>) => {
    const digits = digitsOf(event.clipboardData.getData('text'))
    if (digits === '') return
    event.preventDefault()
    onChange(digits)
    focus(digits.length)
  }

  return (
    <div className="ov-otp" role="group" aria-label={label} id={id}>
      {Array.from({ length: CODE_LENGTH }, (_, index) => (
        <input
          key={index}
          ref={(element) => {
            boxes.current[index] = element
          }}
          aria-label={`Digit ${String(index + 1)}`}
          inputMode="numeric"
          autoComplete={index === 0 ? 'one-time-code' : 'off'}
          // A whole code may arrive in the first box (autofill); the handler spreads it.
          maxLength={index === 0 ? CODE_LENGTH : 1}
          autoFocus={autoFocus && index === 0}
          disabled={disabled}
          value={value[index] ?? ''}
          onChange={(event) => {
            setDigit(index, event.target.value)
          }}
          onKeyDown={onKey(index)}
          onPaste={onPaste}
        />
      ))}
    </div>
  )
}
