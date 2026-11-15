import { act, render, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { fakeProfile } from '../test/profileFixtures'
import { getThemePreference, setThemePreference } from '../theme/theme'
import { DisplayProvider } from './DisplayContext'

// Issue 441: the profile read lands after the person chose a theme on this device. Synthetic
// profile, a fetch held by hand so the order is fixed instead of left to timing.

afterEach(() => {
  vi.unstubAllGlobals()
  setThemePreference('system')
  window.localStorage.clear()
})

describe('DisplayProvider and the theme', () => {
  it('does not undo a theme chosen on this device while the profile was still loading', async () => {
    let answer: (response: Response) => void = () => undefined
    vi.stubGlobal(
      'fetch',
      vi.fn(() => new Promise<Response>((resolve) => (answer = resolve))),
    )
    render(<DisplayProvider>{null}</DisplayProvider>)
    act(() => {
      setThemePreference('dark')
    })
    await act(async () => {
      answer(new Response(JSON.stringify(fakeProfile({ theme: 'system' })), { status: 200, headers: { 'content-type': 'application/json' } }))
      await Promise.resolve()
    })
    await waitFor(() => {
      expect(getThemePreference()).toBe('dark')
    })
    expect(document.documentElement.getAttribute('data-theme')).toBe('dark')
  })
})
