import { t } from '../i18n/store'
import { ApiError, post } from './client'
import { count, flag, object, optionalText, text } from './decode'
import { errorMessage } from './messages'

// QR approve-login (ADR 0011 sections 5 to 7; docs/dev/auth-qrlogin.md). The desktop starts a
// request and waits on its SSE stream; the phone scans the QR, sees where the request comes from
// and approves with a code entered for this approval, or denies. The nonce is only ever in the
// QR's fragment, the phone's memory for one request, and the body of the scan: never stored, never
// in a URL the server sees.

// --- the desktop ------------------------------------------------------------------------------

export interface QrStart {
  id: string
  /** The QR as the server draws it (an SVG document, drawn by `QrCode`). */
  qrSvg: string
  /** Seconds until an unscanned code expires (60). */
  expiresIn: number
}

/** A new request; the server sets a new pre-session cookie and expires this browser's last one. */
export async function startQrLogin(): Promise<QrStart> {
  const body = object(await post('/api/auth/qr/start'), 'qr start')
  return { id: text(body, 'id'), qrSvg: text(body, 'qr_svg'), expiresIn: count(body, 'expires_in') }
}

/** The states the stream reports, then `lost` when it ends without one of the final states. */
export type QrState = 'scanned' | 'approved' | 'denied' | 'expired' | 'completed' | 'lost'

const STREAM_STATES = ['scanned', 'approved', 'denied', 'expired', 'completed'] as const
const FINAL: ReadonlySet<QrState> = new Set(['approved', 'denied', 'expired', 'completed'])

/**
 * Follows the request's stream (`GET /api/auth/qr/{id}/events`, authorised by the pre-session
 * cookie). Each event name is the new state; the events carry nothing else. Returns the function
 * that stops it. The browser reconnects by itself while the stream is open; a stream that closes
 * for good without a final state is reported as `lost`.
 */
export function watchQrLogin(id: string, onState: (state: QrState) => void): () => void {
  const source = new EventSource(`/api/auth/qr/${encodeURIComponent(id)}/events`)
  let ended = false
  const stop = () => {
    ended = true
    source.close()
  }
  for (const state of STREAM_STATES) {
    source.addEventListener(state, () => {
      if (ended) return
      if (FINAL.has(state)) stop()
      onState(state)
    })
  }
  source.onerror = () => {
    if (ended || source.readyState !== EventSource.CLOSED) return
    ended = true
    onState('lost')
  }
  return stop
}

/** After `approved`: the new session's cookie comes in this answer (never in an event). */
export async function completeQrLogin(id: string): Promise<{ email: string }> {
  const body = object(await post(`/api/auth/qr/${encodeURIComponent(id)}/complete`), 'qr complete')
  return { email: text(body, 'email') }
}

// --- the phone --------------------------------------------------------------------------------

/** A nonce is 32 bytes as base64url without padding: 43 characters. */
const NONCE = /^[A-Za-z0-9_-]{43}$/

/** The nonce of a `/qr#<nonce>` address, or null for anything else. */
export function nonceFromHash(hash: string): string | null {
  const value = hash.startsWith('#') ? hash.slice(1) : hash
  return NONCE.test(value) ? value : null
}

export interface ScannedRequest {
  id: string
  desktop: {
    /** As the desktop's browser reports itself: an attacker can choose it. */
    browser: string
    os: string
    ip: string | null
    /** "City, CC", "CC" or the server's "Place unknown": display text only. */
    place: string
  }
  requestedAt: string
  /** The desktop's IP is on another network than this phone's (IPv6 by /64). */
  differentNetwork: boolean
  /** Seconds left to approve or deny. */
  expiresIn: number
}

/** The first scan wins; any other gets the generic 404 `qr_unavailable`. */
export async function scanQr(nonce: string): Promise<ScannedRequest> {
  const body = object(await post('/api/auth/qr/scan', { nonce }), 'qr scan')
  const desktop = object(body.desktop, 'desktop')
  return {
    id: text(body, 'id'),
    desktop: {
      browser: text(desktop, 'browser'),
      os: text(desktop, 'os'),
      ip: optionalText(desktop, 'ip'),
      place: text(desktop, 'place'),
    },
    requestedAt: text(body, 'requested_at'),
    differentNetwork: flag(body, 'different_network'),
    expiresIn: count(body, 'expires_in'),
  }
}

/** Approves with a TOTP or recovery code entered for this approval (an earlier step-up does not count). */
export async function approveQr(id: string, code: string): Promise<void> {
  await post(`/api/auth/qr/${encodeURIComponent(id)}/approve`, { code })
}

export async function denyQr(id: string): Promise<void> {
  await post(`/api/auth/qr/${encodeURIComponent(id)}/deny`)
}

/** The server's one answer for a code that is unknown, expired, used or another session's. */
export const QR_UNAVAILABLE = 'qr_unavailable'

/** The text of a failed QR call: the generic "expired or already used" for `qr_unavailable`. */
export function qrErrorMessage(error: unknown): string {
  if (error instanceof ApiError && error.reason === QR_UNAVAILABLE) return t('passkeys.approve.unavailable')
  return errorMessage(error)
}
