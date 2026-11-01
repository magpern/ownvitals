import { useId } from 'react'
import { CodeInput, CODE_LENGTH } from './CodeInput'

// What the user types as a second factor: the six boxes of an authenticator code, or, behind
// "Use a recovery code instead", one field for a recovery code (Auth and Security mock-ups). The
// code prompt after sign-in and the "Confirm it's you" dialog share it.

export interface SecondFactor {
  recovery: boolean
  code: string
}

export const NO_CODE: SecondFactor = { recovery: false, code: '' }

/** Whether the value can be sent: six digits, or a recovery code. */
export function complete(value: SecondFactor): boolean {
  return value.recovery ? value.code.trim() !== '' : value.code.length === CODE_LENGTH
}

export function SecondFactorField({
  value,
  onChange,
  autoFocus = false,
  disabled,
}: {
  value: SecondFactor
  onChange: (value: SecondFactor) => void
  autoFocus?: boolean
  disabled?: boolean
}) {
  const id = useId()
  return (
    <>
      {value.recovery ? (
        <div className="field">
          <label htmlFor={id}>Recovery code</label>
          <input
            id={id}
            className="ov-input"
            autoComplete="off"
            spellCheck={false}
            autoFocus={autoFocus}
            disabled={disabled}
            value={value.code}
            onChange={(event) => {
              onChange({ recovery: true, code: event.target.value })
            }}
          />
        </div>
      ) : (
        <CodeInput
          value={value.code}
          autoFocus={autoFocus}
          disabled={disabled}
          onChange={(code) => {
            onChange({ recovery: false, code })
          }}
        />
      )}
      <button
        className="ov-linkbutton"
        type="button"
        onClick={() => {
          onChange({ recovery: !value.recovery, code: '' })
        }}
      >
        {value.recovery ? 'Use the authenticator app instead' : 'Use a recovery code instead'}
      </button>
    </>
  )
}
