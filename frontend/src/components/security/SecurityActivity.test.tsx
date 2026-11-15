import { screen, within } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'
import { setLocale } from '../../i18n'
import { SIGNED_IN, fakeApi, reason } from '../../test/fakeApi'
import { renderAt } from '../../test/render'
import { eventDevice, eventLabel, withinWindow } from './SecurityActivity'

// "Recent security activity" (issue 458). Synthetic events and a faked server only.

const ago = (hours: number) => new Date(Date.now() - hours * 3_600_000).toISOString()
const route = (events: object[]) => ({
  'GET /api/auth/session': SIGNED_IN,
  'GET /api/auth/sessions': [],
  'GET /api/auth/activity': { events, next_before: null },
})

describe('the activity panel', () => {
  it('lists events with wording, device, place and the failure tone', async () => {
    fakeApi(
      route([
        { kind: 'sign_in', at: ago(1), method: 'password', browser: 'Firefox', os: 'Windows', place: 'Stockholm, SE' },
        { kind: 'mfa_failed', at: ago(30), method: null, browser: 'Safari', os: 'iPhone', place: 'Place unknown' },
        { kind: 'export_ready', at: ago(24 * 40), method: null, browser: null, os: null, place: 'Place unknown' },
      ]),
    )
    renderAt('/settings/security')
    const panel = await screen.findByRole('region', { name: 'Recent security activity' })
    const items = await within(panel).findAllByRole('listitem')
    expect(items).toHaveLength(2) // the 40-day-old one is outside "Last 30 days"
    expect(items[0]).toHaveTextContent('Signed in with your password')
    expect(items[0]).toHaveTextContent('Firefox on Windows · Stockholm, SE')
    expect(items[1]).toHaveTextContent('Wrong authenticator code')
    expect(items[1]).toHaveTextContent('Safari on iPhone · Place unknown')
    expect(items[1]).toHaveAttribute('data-tone', 'attn')
    expect(items[0]).not.toHaveAttribute('data-tone')
    expect(within(panel).getByText('Last 30 days')).toBeInTheDocument()
  })

  it('says so when there is nothing to show', async () => {
    fakeApi(route([]))
    renderAt('/settings/security')
    expect(await screen.findByText('Nothing to show from the last 30 days.')).toBeInTheDocument()
  })

  it('shows the reason and a way to try again when the read fails', async () => {
    fakeApi({ ...route([]), 'GET /api/auth/activity': () => reason(500, 'server_error') })
    renderAt('/settings/security')
    const panel = await screen.findByRole('region', { name: 'Recent security activity' })
    expect(await within(panel).findByRole('alert')).toBeInTheDocument()
    expect(within(panel).getByRole('button', { name: 'Try again' })).toBeInTheDocument()
  })

  it('words an unknown code generically', async () => {
    fakeApi(route([{ kind: 'something_new', at: ago(2), method: null, browser: null, os: null, place: 'SE' }]))
    renderAt('/settings/security')
    const item = await screen.findByText('Security event')
    expect(item.closest('li')).toHaveTextContent('Unknown device · SE')
  })
})

describe('the helpers', () => {
  it('words a sign-in by method and falls back without one', () => {
    expect(eventLabel({ kind: 'sign_in', method: 'passkey' })).toBe('Signed in with a passkey')
    expect(eventLabel({ kind: 'sign_in', method: null })).toBe('Signed in')
    expect(eventLabel({ kind: 'sign_in', method: 'carrier-pigeon' })).toBe('Signed in')
    expect(eventLabel({ kind: 'nope', method: null })).toBe('Security event')
  })
  it('names the device from what is recorded', () => {
    expect(eventDevice({ browser: 'Chrome', os: null })).toBe('Chrome')
    expect(eventDevice({ browser: null, os: 'Linux' })).toBe('Linux')
  })
  it('words the "other" family as an unknown device, keeping a family that is known', () => {
    expect(eventDevice({ browser: 'other', os: 'other' })).toBe('Unknown device')
    expect(eventDevice({ browser: 'other', os: 'Linux' })).toBe('Linux')
    expect(eventDevice({ browser: 'Chrome', os: 'other' })).toBe('Chrome')
  })
  it('keeps only the last 30 days', () => {
    const now = new Date('2030-02-01T00:00:00Z')
    const mk = (at: string) => ({ kind: 'sign_in', at, method: null, browser: null, os: null, place: 'SE' })
    expect(withinWindow([mk('2030-01-31T00:00:00Z'), mk('2029-12-01T00:00:00Z')], now)).toHaveLength(1)
  })
})

describe('in Swedish', () => {
  afterEach(async () => {
    await setLocale('en')
  })

  it('words an unknown place and an unknown device in Swedish', async () => {
    await setLocale('sv')
    fakeApi(
      route([{ kind: 'sign_in', at: ago(1), method: 'password', browser: 'other', os: 'other', place: 'Place unknown' }]),
    )
    renderAt('/settings/security')
    const item = (await screen.findByText('Okänd enhet', { exact: false })).closest('li')
    expect(item).toHaveTextContent('Okänd enhet · Okänd plats')
    expect(item).not.toHaveTextContent('Place unknown')
    expect(item).not.toHaveTextContent('other')
  })
})
