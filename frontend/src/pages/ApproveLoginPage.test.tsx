import { act, fireEvent, screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { ANONYMOUS, SIGNED_IN, SIGNED_IN_NO_CODE, fakeApi, reason } from '../test/fakeApi'
import { renderAt } from '../test/render'

// The phone's approval screen (/qr#<nonce>; ADR 0011 sections 5, 7 and 8). A made-up nonce and
// request; the server is a fake.

const NONCE = 'n0nce-made-up-for-the-tests_only-synthetic0'
const SCANNED = {
  id: 'row-1',
  desktop: { browser: 'Firefox', os: 'Windows', ip: '203.0.113.24', place: 'Place unknown' },
  requested_at: '2030-01-01T07:30:00Z',
  different_network: false,
  expires_in: 118,
}

function enterCode(code: string) {
  const dialog = screen.getByRole('dialog', { name: "Confirm it's you" })
  const boxes = within(within(dialog).getByRole('group', { name: '6-digit code' })).getAllByRole('textbox')
  fireEvent.change(boxes[0] as HTMLElement, { target: { value: code } })
  fireEvent.click(within(dialog).getByRole('button', { name: 'Confirm' }))
}

describe('approving a sign-in on the phone', () => {
  it('removes the nonce from the address, scans once and shows where the request comes from', async () => {
    expect(NONCE).toHaveLength(43)
    const api = fakeApi({ 'GET /api/auth/session': SIGNED_IN, 'POST /api/auth/qr/scan': SCANNED })
    renderAt(`/qr#${NONCE}`)
    expect(window.location.hash).toBe('')
    expect(window.location.pathname).toBe('/qr')
    expect(await screen.findByRole('heading', { name: 'Approve a sign-in?' })).toBeInTheDocument()
    expect(await screen.findByText('Firefox on Windows, as reported by that browser')).toBeInTheDocument()
    expect(screen.getByText('203.0.113.24')).toBeInTheDocument()
    expect(screen.getByText('Place unknown')).toBeInTheDocument()
    expect(
      screen.getByText(
        'Only approve if you are signing in on a computer in front of you right now. ownvitals never asks you to scan a code it sent you.',
      ),
    ).toBeInTheDocument()
    expect(screen.queryByText('This computer is on a different network than your phone.')).toBeNull()
    expect(api.to('POST', '/api/auth/qr/scan')).toHaveLength(1)
    expect(api.to('POST', '/api/auth/qr/scan')[0]?.body).toEqual({ nonce: NONCE })
    expect(screen.getByRole('button', { name: 'Approve…' })).toHaveAttribute('aria-haspopup', 'dialog')
  })

  it('starts over when a new code is opened in the same tab (only the fragment changes)', async () => {
    let scans = 0
    const api = fakeApi({
      'GET /api/auth/session': SIGNED_IN,
      'POST /api/auth/qr/scan': () => {
        scans += 1
        return { ...SCANNED, id: `row-${String(scans)}` }
      },
      'POST /api/auth/qr/row-1/deny': { status: 'denied' },
    })
    renderAt(`/qr#${NONCE}`)
    fireEvent.click(await screen.findByRole('button', { name: 'Deny' }))
    expect(await screen.findByRole('heading', { name: 'Sign-in denied' })).toBeInTheDocument()
    const next = 'B'.repeat(42) + 'Q'
    act(() => {
      window.history.replaceState(null, '', `/qr#${next}`)
      window.dispatchEvent(new HashChangeEvent('hashchange'))
    })
    expect(await screen.findByRole('heading', { name: 'Approve a sign-in?' })).toBeInTheDocument()
    expect(window.location.hash).toBe('')
    expect(api.to('POST', '/api/auth/qr/scan').map((call) => call.body)).toEqual([{ nonce: NONCE }, { nonce: next }])
  })

  it('warns when the computer is on another network', async () => {
    fakeApi({ 'GET /api/auth/session': SIGNED_IN, 'POST /api/auth/qr/scan': { ...SCANNED, different_network: true } })
    renderAt(`/qr#${NONCE}`)
    expect(await screen.findByText('This computer is on a different network than your phone.')).toBeInTheDocument()
  })

  it('approves with a code entered for this approval; a wrong code stays in the dialog', async () => {
    let tries = 0
    const api = fakeApi({
      'GET /api/auth/session': SIGNED_IN,
      'POST /api/auth/qr/scan': SCANNED,
      'POST /api/auth/qr/row-1/approve': () => {
        tries += 1
        return tries === 1 ? reason(400, 'invalid_code') : { status: 'approved' }
      },
    })
    renderAt(`/qr#${NONCE}`)
    fireEvent.click(await screen.findByRole('button', { name: 'Approve…' }))
    const dialog = screen.getByRole('dialog', { name: "Confirm it's you" })
    expect(dialog).toHaveTextContent('Approving this sign-in needs a fresh code from your authenticator app, entered for this approval only.')
    enterCode('111111')
    expect(await within(dialog).findByRole('alert')).toHaveTextContent('That code is not right.')
    enterCode('123456')
    expect(await screen.findByRole('heading', { name: 'Sign-in approved' })).toBeInTheDocument()
    expect(api.to('POST', '/api/auth/qr/row-1/approve').map((call) => call.body)).toEqual([{ code: '111111' }, { code: '123456' }])
    // The approval's own code: no step-up of the phone's session.
    expect(api.to('POST', '/api/auth/mfa/stepup')).toHaveLength(0)
  })

  it('cancels the dialog without approving', async () => {
    const api = fakeApi({ 'GET /api/auth/session': SIGNED_IN, 'POST /api/auth/qr/scan': SCANNED })
    renderAt(`/qr#${NONCE}`)
    fireEvent.click(await screen.findByRole('button', { name: 'Approve…' }))
    fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Cancel' }))
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(screen.getByRole('heading', { name: 'Approve a sign-in?' })).toBeInTheDocument()
    expect(api.to('POST', '/api/auth/qr/row-1/approve')).toHaveLength(0)
  })

  it('shows the generic answer when the request ended while the dialog was open', async () => {
    fakeApi({
      'GET /api/auth/session': SIGNED_IN,
      'POST /api/auth/qr/scan': SCANNED,
      'POST /api/auth/qr/row-1/approve': reason(404, 'qr_unavailable'),
    })
    renderAt(`/qr#${NONCE}`)
    fireEvent.click(await screen.findByRole('button', { name: 'Approve…' }))
    enterCode('123456')
    expect(await screen.findByRole('heading', { name: 'This code cannot be used' })).toBeInTheDocument()
    expect(screen.queryByRole('dialog')).toBeNull()
  })

  it('denies without a code', async () => {
    const api = fakeApi({
      'GET /api/auth/session': SIGNED_IN,
      'POST /api/auth/qr/scan': SCANNED,
      'POST /api/auth/qr/row-1/deny': { status: 'denied' },
    })
    renderAt(`/qr#${NONCE}`)
    fireEvent.click(await screen.findByRole('button', { name: 'Deny' }))
    expect(await screen.findByRole('heading', { name: 'Sign-in denied' })).toBeInTheDocument()
    expect(api.to('POST', '/api/auth/qr/row-1/deny')).toHaveLength(1)
  })

  it.each([
    ['signed out', ANONYMOUS],
    ['still owing the code', SIGNED_IN_NO_CODE],
  ])('asks to sign in first and drops the nonce when %s', async (_, session) => {
    const api = fakeApi({ 'GET /api/auth/session': session })
    renderAt(`/qr#${NONCE}`)
    expect(await screen.findByRole('heading', { name: 'Sign in on this phone first' })).toBeInTheDocument()
    expect(window.location.hash).toBe('')
    expect(api.to('POST', '/api/auth/qr/scan')).toHaveLength(0)
  })

  it('gives the generic answer for a used code, a missing nonce and a session that may not approve', async () => {
    fakeApi({ 'GET /api/auth/session': SIGNED_IN, 'POST /api/auth/qr/scan': reason(404, 'qr_unavailable') })
    const first = renderAt(`/qr#${NONCE}`)
    expect(await screen.findByText(/This code has expired or was already used/)).toBeInTheDocument()
    first.unmount()

    const api = fakeApi({ 'GET /api/auth/session': SIGNED_IN })
    const second = renderAt('/qr#not-a-nonce')
    expect(await screen.findByRole('heading', { name: 'This code cannot be used' })).toBeInTheDocument()
    expect(api.to('POST', '/api/auth/qr/scan')).toHaveLength(0)
    second.unmount()

    fakeApi({ 'GET /api/auth/session': SIGNED_IN, 'POST /api/auth/qr/scan': reason(403, 'qr_approval_not_allowed') })
    renderAt(`/qr#${NONCE}`)
    await waitFor(() => {
      expect(screen.getByText('This account cannot approve a sign-in on another device.')).toBeInTheDocument()
    })
  })

  it('links the failed and denied screens to the app start, not to the public home', async () => {
    fakeApi({ 'GET /api/auth/session': SIGNED_IN, 'POST /api/auth/qr/scan': reason(404, 'qr_unavailable') })
    const failed = renderAt(`/qr#${NONCE}`)
    expect(await screen.findByText(/This code has expired or was already used/)).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Go to ownvitals' })).toHaveAttribute('href', '/overview')
    failed.unmount()

    fakeApi({
      'GET /api/auth/session': SIGNED_IN,
      'POST /api/auth/qr/scan': SCANNED,
      'POST /api/auth/qr/row-1/deny': { status: 'denied' },
    })
    renderAt(`/qr#${NONCE}`)
    fireEvent.click(await screen.findByRole('button', { name: 'Deny' }))
    expect(await screen.findByRole('heading', { name: 'Sign-in denied' })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Go to ownvitals' })).toHaveAttribute('href', '/overview')
  })
})
