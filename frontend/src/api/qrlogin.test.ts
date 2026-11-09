import { describe, expect, it, vi } from 'vitest'
import { installEventSource } from '../test/eventSourceFake'
import { fakeApi, reason } from '../test/fakeApi'
import { approveQr, completeQrLogin, denyQr, nonceFromHash, qrErrorMessage, scanQr, startQrLogin, watchQrLogin } from './qrlogin'
import { ApiError } from './client'

// QR approve-login's calls and stream (ADR 0011 section 5). A made-up nonce and ids; the server
// and the stream are fakes.

const NONCE = 'A'.repeat(42) + 'Q'

describe('the nonce in the address', () => {
  it('takes only a 43-character base64url fragment', () => {
    expect(nonceFromHash(`#${NONCE}`)).toBe(NONCE)
    expect(nonceFromHash(NONCE)).toBe(NONCE)
    expect(nonceFromHash('')).toBeNull()
    expect(nonceFromHash('#')).toBeNull()
    expect(nonceFromHash(`#${NONCE}A`)).toBeNull()
    expect(nonceFromHash(`#${NONCE.slice(1)}=`)).toBeNull()
    expect(nonceFromHash('#<script>')).toBeNull()
  })
})

describe('the stream', () => {
  it('reports each state and stops at a final one', () => {
    const Source = installEventSource()
    const seen: string[] = []
    watchQrLogin('row-1', (state) => seen.push(state))
    const source = Source.latest()
    expect(source.url).toBe('/api/auth/qr/row-1/events')
    source.emit('scanned')
    source.emit('approved')
    source.emit('denied')
    expect(seen).toEqual(['scanned', 'approved'])
    expect(source.readyState).toBe(Source.CLOSED)
  })

  it('reports a stream that closed for good without an end as lost, once', () => {
    const Source = installEventSource()
    const onState = vi.fn()
    watchQrLogin('row-1', onState)
    Source.latest().fail()
    Source.latest().onerror?.(new Event('error'))
    expect(onState.mock.calls).toEqual([['lost']])
  })

  it('ignores an error while the browser reconnects, and anything after stop', () => {
    const Source = installEventSource()
    const onState = vi.fn()
    const stop = watchQrLogin('row-1', onState)
    const source = Source.latest()
    source.readyState = Source.CONNECTING
    source.onerror?.(new Event('error'))
    expect(onState).not.toHaveBeenCalled()
    stop()
    source.emit('scanned')
    expect(onState).not.toHaveBeenCalled()
  })
})

describe('the calls', () => {
  it('starts, completes, scans, approves and denies', async () => {
    const api = fakeApi({
      'POST /api/auth/qr/start': { id: 'row-1', qr_svg: '<svg/>', qr_url: 'http://localhost/qr#x', expires_in: 60 },
      'POST /api/auth/qr/row-1/complete': { id: 'u1', email: 'ann@example.test', role: 'user' },
      'POST /api/auth/qr/scan': {
        id: 'row-1',
        desktop: { browser: 'Firefox', os: 'Windows', ip: '203.0.113.24', place: 'Place unknown' },
        requested_at: '2030-01-01T07:30:00Z',
        different_network: true,
        expires_in: 118,
      },
      'POST /api/auth/qr/row-1/approve': { status: 'approved' },
      'POST /api/auth/qr/row-1/deny': { status: 'denied' },
    })
    await expect(startQrLogin()).resolves.toEqual({ id: 'row-1', qrSvg: '<svg/>', expiresIn: 60 })
    await expect(completeQrLogin('row-1')).resolves.toEqual({ email: 'ann@example.test' })
    await expect(scanQr(NONCE)).resolves.toEqual({
      id: 'row-1',
      desktop: { browser: 'Firefox', os: 'Windows', ip: '203.0.113.24', place: 'Place unknown' },
      requestedAt: '2030-01-01T07:30:00Z',
      differentNetwork: true,
      expiresIn: 118,
    })
    expect(api.to('POST', '/api/auth/qr/scan')[0]?.body).toEqual({ nonce: NONCE })
    await approveQr('row-1', '123456')
    expect(api.to('POST', '/api/auth/qr/row-1/approve')[0]?.body).toEqual({ code: '123456' })
    await denyQr('row-1')
    expect(api.to('POST', '/api/auth/qr/row-1/deny')).toHaveLength(1)
  })

  it('words the generic refusal, and any other as usual', async () => {
    fakeApi({ 'POST /api/auth/qr/scan': reason(404, 'qr_unavailable') })
    const caught = await scanQr(NONCE).catch((error: unknown) => error)
    expect(caught).toBeInstanceOf(ApiError)
    expect(qrErrorMessage(caught)).toBe(
      'This code has expired or was already used. Show a new code on the computer and scan it again.',
    )
    expect(qrErrorMessage(new ApiError(403, 'qr_approval_not_allowed', null, undefined))).toBe(
      'This account cannot approve a sign-in on another device.',
    )
  })
})
