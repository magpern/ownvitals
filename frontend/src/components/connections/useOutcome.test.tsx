import { act, renderHook } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { OUTCOME_SHOWN_MS, useOutcome } from './useOutcome'

// Issue 304: the Withings outcome sentence goes away on a timer. Synthetic; fake timers.

afterEach(() => {
  vi.useRealTimers()
})

describe('useOutcome', () => {
  it('shows the sentence, then dismisses it after the timer', () => {
    vi.useFakeTimers()
    const { result } = renderHook(() => useOutcome('?withings=state_rejected'))
    expect(result.current).toContain('expired or was already used')
    act(() => {
      vi.advanceTimersByTime(OUTCOME_SHOWN_MS - 1)
    })
    expect(result.current).not.toBeNull()
    act(() => {
      vi.advanceTimersByTime(1)
    })
    expect(result.current).toBeNull()
  })

  it('shows a different outcome afresh', () => {
    vi.useFakeTimers()
    const { result, rerender } = renderHook(({ s }) => useOutcome(s), { initialProps: { s: '?withings=failed' } })
    act(() => {
      vi.advanceTimersByTime(OUTCOME_SHOWN_MS)
    })
    expect(result.current).toBeNull()
    rerender({ s: '?withings=conflict' })
    expect(result.current).toContain('Another connection change')
  })
})
