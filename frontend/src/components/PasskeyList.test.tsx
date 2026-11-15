import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'
import { SIGNED_IN, fakeApi, reason } from '../test/fakeApi'
import { renderAt } from '../test/render'
import { REGISTRATION_OPTIONS, fakeAttestation, installWebAuthn, uninstallWebAuthn } from '../test/webauthnFake'
import { attestationJson } from '../api/passkeys'

// The passkeys of Settings → Sign-in and security (ADR 0011 sections 2, 8 and 10): add and remove
// behind the shared "Confirm it's you" dialog, tried again after it; rename without a proof.
// Synthetic passkeys and a faked server and authenticator only.

afterEach(uninstallWebAuthn)

const LAPTOP = { id: 'p1', name: 'Laptop', created_at: '2030-01-01T07:30:00Z', last_used_at: '2030-01-02T07:30:00Z', synced: true, backed_up: true, suspended: false }
const KEY = { id: 'p2', name: 'Security key', created_at: '2030-01-01T07:30:00Z', last_used_at: null, synced: false, backed_up: false, suspended: true }
const NEW = { id: 'p3', name: 'Passkey', created_at: '2030-01-03T07:30:00Z', last_used_at: null, synced: false, backed_up: false, suspended: false }

function typeCode(code: string) {
  const dialog = screen.getByRole('dialog', { name: "Confirm it's you" })
  const boxes = within(within(dialog).getByRole('group', { name: '6-digit code' })).getAllByRole('textbox')
  fireEvent.change(boxes[0] as HTMLElement, { target: { value: code } })
  fireEvent.click(within(dialog).getByRole('button', { name: 'Confirm' }))
}

const list = () => screen.findByRole('list', { name: 'Your passkeys' })

describe('the passkey list', () => {
  it('shows each passkey with its dates, "synced" and a suspended one', async () => {
    fakeApi({ 'GET /api/auth/session': SIGNED_IN, 'GET /api/auth/sessions': [], 'GET /api/auth/passkeys': { passkeys: [LAPTOP, KEY], limit: 10 } })
    installWebAuthn()
    renderAt('/settings/security')
    const items = within(await list()).getAllByRole('listitem')
    expect(items).toHaveLength(2)
    expect(within(items[0] as HTMLElement).getByText('Laptop')).toBeInTheDocument()
    expect(items[0]).toHaveTextContent(/Added 1 Jan 2030 · last used 2 Jan 2030 · synced between your devices/)
    expect(items[1]).toHaveTextContent(/not used yet/)
    expect(within(items[1] as HTMLElement).getByText('Suspended')).toBeInTheDocument()
    const remove = screen.getByRole('button', { name: 'Remove Laptop' })
    expect(remove).toHaveAttribute('data-lock')
    expect(screen.getByRole('button', { name: 'Add a passkey…' })).toHaveAttribute('aria-haspopup', 'dialog')
    expect(screen.getByText('Approve sign-ins from your phone')).toBeInTheDocument()
  })

  it('says so when there is none, and when the browser has no passkeys', async () => {
    fakeApi({ 'GET /api/auth/session': SIGNED_IN, 'GET /api/auth/sessions': [] })
    renderAt('/settings/security')
    expect(await screen.findByText('No passkey yet.')).toBeInTheDocument()
    expect(screen.getByText('This browser cannot use passkeys.')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Add a passkey…' })).toBeNull()
  })

  it('shows "not available" without an add button when the server has no relying party', async () => {
    fakeApi({ 'GET /api/auth/session': SIGNED_IN, 'GET /api/auth/sessions': [], 'GET /api/auth/passkeys': reason(404, 'passkeys_unavailable') })
    installWebAuthn()
    renderAt('/settings/security')
    expect(await screen.findByText('Passkeys are not available right now.')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Add a passkey…' })).toBeNull()
    expect(screen.queryByRole('alert')).toBeNull()
  })

  it('renames without a proof', async () => {
    let rows = [LAPTOP]
    const api = fakeApi({
      'GET /api/auth/session': SIGNED_IN,
      'GET /api/auth/sessions': [],
      'GET /api/auth/passkeys': () => ({ passkeys: rows, limit: 10 }),
      'PATCH /api/auth/passkeys/p1': (body) => {
        rows = [{ ...LAPTOP, name: (body as { name: string }).name }]
        return rows[0]
      },
    })
    installWebAuthn()
    renderAt('/settings/security')
    fireEvent.click(await screen.findByRole('button', { name: 'Rename Laptop' }))
    fireEvent.change(screen.getByLabelText('Name of the passkey'), { target: { value: '  Work laptop ' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    expect(await screen.findByText('Work laptop')).toBeInTheDocument()
    expect(api.to('PATCH', '/api/auth/passkeys/p1')[0]?.body).toEqual({ name: 'Work laptop' })
    expect(screen.queryByRole('dialog')).toBeNull()
  })

  it('removes after the step-up dialog, trying the removal again', async () => {
    let stepped = false
    let rows = [LAPTOP, KEY]
    const api = fakeApi({
      'GET /api/auth/session': SIGNED_IN,
      'GET /api/auth/sessions': [],
      'GET /api/auth/passkeys': () => ({ passkeys: rows, limit: 10 }),
      'POST /api/auth/mfa/stepup': () => {
        stepped = true
        return new Response(null, { status: 204 })
      },
      'DELETE /api/auth/passkeys/p1': () => {
        if (!stepped) return reason(403, 'step_up_required')
        rows = [KEY]
        return new Response(null, { status: 204 })
      },
    })
    installWebAuthn()
    renderAt('/settings/security')
    fireEvent.click(await screen.findByRole('button', { name: 'Remove Laptop' }))
    const dialog = await screen.findByRole('dialog', { name: "Confirm it's you" })
    expect(dialog).toHaveTextContent('Removing a passkey needs a fresh code')
    typeCode('123456')
    await waitFor(() => {
      expect(screen.queryByText('Laptop')).toBeNull()
    })
    expect(api.to('DELETE', '/api/auth/passkeys/p1')).toHaveLength(2)
    expect(api.to('POST', '/api/auth/mfa/stepup')[0]?.body).toEqual({ code: '123456' })
    expect(screen.queryByRole('dialog')).toBeNull()
  })

  it('adds one: the password and the code first, then the browser, then the server', async () => {
    let password = false
    let stepped = false
    let rows: object[] = []
    const { create } = installWebAuthn()
    const api = fakeApi({
      'GET /api/auth/session': SIGNED_IN,
      'GET /api/auth/sessions': [],
      'GET /api/auth/identities': { identities: [], has_password: true, available: [] },
      'GET /api/auth/passkeys': () => ({ passkeys: rows, limit: 10 }),
      'POST /api/auth/reauth': () => {
        password = true
        return new Response(null, { status: 204 })
      },
      'POST /api/auth/mfa/stepup': () => {
        stepped = true
        return new Response(null, { status: 204 })
      },
      'POST /api/auth/passkeys/register/options': () => {
        if (!password) return reason(403, 'reauth_required')
        if (!stepped) return reason(403, 'step_up_required')
        return REGISTRATION_OPTIONS
      },
      'POST /api/auth/passkeys/register': () => {
        rows = [NEW]
        return json201(NEW)
      },
    })
    renderAt('/settings/security')
    await screen.findByText('No passkey yet.')
    fireEvent.click(screen.getByRole('button', { name: 'Add a passkey…' }))
    const dialog = await screen.findByRole('dialog', { name: "Confirm it's you" })
    expect(dialog).toHaveTextContent('Adding a passkey needs your password')
    fireEvent.change(within(dialog).getByLabelText('Password'), { target: { value: 'synthetic-password' } })
    fireEvent.click(within(dialog).getByRole('button', { name: 'Confirm' }))
    await waitFor(() => {
      expect(screen.getByRole('dialog', { name: "Confirm it's you" })).toHaveTextContent('needs a fresh code')
    })
    typeCode('123456')
    expect(await screen.findByText('Passkey')).toBeInTheDocument()
    expect(create).toHaveBeenCalledTimes(1)
    expect(api.to('POST', '/api/auth/passkeys/register')[0]?.body).toEqual({ credential: attestationJson(fakeAttestation()) })
    expect(api.to('POST', '/api/auth/passkeys/register/options')).toHaveLength(3)
  })

  it('says nothing changed when the browser window is closed', async () => {
    installWebAuthn({ create: () => Promise.reject(new DOMException('closed', 'NotAllowedError')) })
    const api = fakeApi({
      'GET /api/auth/session': SIGNED_IN,
      'GET /api/auth/sessions': [],
      'POST /api/auth/passkeys/register/options': REGISTRATION_OPTIONS,
    })
    renderAt('/settings/security')
    await screen.findByText('No passkey yet.')
    fireEvent.click(screen.getByRole('button', { name: 'Add a passkey…' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('The passkey window was closed, so nothing changed.')
    expect(api.to('POST', '/api/auth/passkeys/register')).toHaveLength(0)
  })

  it('shows nothing when the dialog is cancelled', async () => {
    const api = fakeApi({
      'GET /api/auth/session': SIGNED_IN,
      'GET /api/auth/sessions': [],
      'GET /api/auth/passkeys': { passkeys: [LAPTOP], limit: 10 },
      'DELETE /api/auth/passkeys/p1': reason(403, 'step_up_required'),
    })
    installWebAuthn()
    renderAt('/settings/security')
    fireEvent.click(await screen.findByRole('button', { name: 'Remove Laptop' }))
    const dialog = await screen.findByRole('dialog', { name: "Confirm it's you" })
    fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }))
    await waitFor(() => {
      expect(screen.queryByRole('dialog')).toBeNull()
    })
    expect(screen.queryByRole('alert')).toBeNull()
    expect(screen.getByText('Laptop')).toBeInTheDocument()
    expect(api.to('DELETE', '/api/auth/passkeys/p1')).toHaveLength(1)
  })

  it('cannot add past the limit', async () => {
    fakeApi({ 'GET /api/auth/session': SIGNED_IN, 'GET /api/auth/sessions': [], 'GET /api/auth/passkeys': { passkeys: [LAPTOP], limit: 1 } })
    installWebAuthn()
    renderAt('/settings/security')
    expect(await screen.findByText('An account holds up to 1 passkey.')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Add a passkey…' })).toBeDisabled()
  })
})

function json201(body: unknown): Response {
  return new Response(JSON.stringify(body), { status: 201, headers: { 'Content-Type': 'application/json' } })
}
