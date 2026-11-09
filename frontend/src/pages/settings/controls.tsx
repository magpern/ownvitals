import { useId, type ReactNode } from 'react'

// The controls of the Settings mock-up (docs/design/mockups/src/Settings.dc.html): a settings row,
// the segmented choice (`.seg`), the switch (`.toggle`) and the filter chip (`.chip`), as `ov-*`
// classes in styles/settings.css.

/** A row: what it is on the left (label and help), its control on the right. */
export function Row({
  label,
  help,
  htmlFor,
  children,
  below,
}: {
  label: string
  help?: string
  /** The id of the control, when it is a form field the label belongs to. */
  htmlFor?: string
  children?: ReactNode
  below?: ReactNode
}) {
  return (
    <div className="ov-setting">
      <div className="ov-setting__what">
        {htmlFor !== undefined ? (
          <label className="ov-setting__label" htmlFor={htmlFor}>
            {label}
          </label>
        ) : (
          <span className="ov-setting__label">{label}</span>
        )}
        {help !== undefined ? <p>{help}</p> : null}
        {below}
      </div>
      {children}
    </div>
  )
}

export interface Option<T extends string> {
  value: T
  label: string
}

/** One choice of a few: buttons with `aria-pressed`. */
export function Seg<T extends string>({
  label,
  value,
  options,
  disabled = false,
  onChange,
}: {
  label: string
  value: T
  options: readonly Option<T>[]
  disabled?: boolean
  onChange: (value: T) => void
}) {
  return (
    <div className="ov-seg" role="group" aria-label={label}>
      {options.map((option) => (
        <button
          key={option.value}
          type="button"
          aria-pressed={option.value === value}
          disabled={disabled}
          onClick={() => {
            if (option.value !== value) onChange(option.value)
          }}
        >
          {option.label}
        </button>
      ))}
    </div>
  )
}

/** A switch. */
export function Toggle({
  label,
  checked,
  disabled = false,
  onChange,
}: {
  label: string
  checked: boolean
  disabled?: boolean
  onChange?: (checked: boolean) => void
}) {
  const id = useId()
  return (
    <input
      id={id}
      className="ov-toggle"
      type="checkbox"
      role="switch"
      aria-label={label}
      checked={checked}
      disabled={disabled}
      onChange={(event) => {
        onChange?.(event.target.checked)
      }}
    />
  )
}

/** A filter chip that is on or off. */
export function Chip({
  label,
  pressed,
  disabled = false,
  onClick,
}: {
  label: string
  pressed: boolean
  disabled?: boolean
  onClick: () => void
}) {
  return (
    <button type="button" className="ov-chip" aria-pressed={pressed} disabled={disabled} onClick={onClick}>
      {label}
    </button>
  )
}
