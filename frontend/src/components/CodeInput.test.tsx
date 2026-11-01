import { fireEvent, render, screen, within } from '@testing-library/react'
import { useState } from 'react'
import { describe, expect, it } from 'vitest'
import { CodeInput } from './CodeInput'

function Harness({ start = '' }: { start?: string }) {
  const [value, setValue] = useState(start)
  return (
    <>
      <CodeInput value={value} onChange={setValue} />
      <output>{value}</output>
    </>
  )
}

const boxes = () => within(screen.getByRole('group', { name: '6-digit code' })).getAllByRole('textbox')
const box = (index: number) => boxes()[index] as HTMLInputElement

describe('the six code boxes', () => {
  it('has one labelled box per digit, numeric, the first one for one-time-code autofill', () => {
    render(<Harness />)
    expect(boxes()).toHaveLength(6)
    expect(box(0)).toHaveAttribute('aria-label', 'Digit 1')
    expect(box(0)).toHaveAttribute('autocomplete', 'one-time-code')
    expect(box(5)).toHaveAttribute('inputmode', 'numeric')
  })

  it('moves to the next box as digits are typed, and ignores anything else', () => {
    render(<Harness />)
    fireEvent.change(box(0), { target: { value: '4' } })
    expect(document.activeElement).toBe(box(1))
    fireEvent.change(box(1), { target: { value: 'x' } })
    fireEvent.change(box(1), { target: { value: '8' } })
    expect(screen.getByRole('status')).toHaveTextContent('48')
  })

  it('takes a whole pasted or autofilled code', () => {
    render(<Harness />)
    fireEvent.paste(box(0), { clipboardData: { getData: () => '12 34 56 78' } })
    expect(screen.getByRole('status')).toHaveTextContent('123456')
    fireEvent.change(box(0), { target: { value: '654321' } })
    expect(screen.getByRole('status')).toHaveTextContent('654321')
  })

  it('goes back on Backspace in an empty box', () => {
    render(<Harness start="12" />)
    fireEvent.keyDown(box(2), { key: 'Backspace' })
    expect(screen.getByRole('status')).toHaveTextContent(/^1$/)
    expect(document.activeElement).toBe(box(1))
  })
})
