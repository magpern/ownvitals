import { act, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { QR_SVG } from '../test/fakeApi'
import type { QrState } from '../api/qrlogin'
import { QrLoginPanel } from './QrLoginPanel'

// Issue 304: a state the stream reports at once must clear the expiry timer, not be followed by
// it. Synthetic; the API module is faked.

vi.mock('../api/qrlogin', async (importActual) => ({
  ...(await importActual<typeof import('../api/qrlogin')>()),
  startQrLogin: () => Promise.resolve({ id: 'row-1', qrSvg: QR_SVG, expiresIn: 60 }),
  watchQrLogin: (_id: string, onState: (state: QrState) => void) => {
    onState('scanned')
    return () => undefined
  },
}))

afterEach(() => {
  vi.useRealTimers()
})

describe('QrLoginPanel expiry timer', () => {
  it('does not expire a code whose scan was reported before the timer was set', async () => {
    vi.useFakeTimers()
    render(<QrLoginPanel onClose={() => undefined} onSignedIn={() => undefined} />)
    await act(async () => {
      await Promise.resolve()
    })
    expect(screen.getByText('Scanned. Approve the sign-in on your phone.')).toBeInTheDocument()
    act(() => {
      vi.advanceTimersByTime(90_000)
    })
    expect(screen.queryByText('This code has expired.')).toBeNull()
    expect(screen.getByText('Scanned. Approve the sign-in on your phone.')).toBeInTheDocument()
  })
})
