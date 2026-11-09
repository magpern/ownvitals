import { afterEach, describe, expect, it } from 'vitest'
import { fakeApi, reason } from '../test/fakeApi'
import {
  REGISTRATION_OPTIONS,
  SIGNIN_OPTIONS,
  fakeAssertion,
  fakeAttestation,
  installWebAuthn,
  uninstallWebAuthn,
} from '../test/webauthnFake'
import { ApiError } from './client'
import { DecodeError } from './decode'
import {
  PasskeyCancelled,
  assertionJson,
  attestationJson,
  createPasskey,
  creationOptions,
  fromB64url,
  listPasskeys,
  passkeysSupported,
  removePasskey,
  renamePasskey,
  requestOptions,
  signInWithPasskey,
  toB64url,
} from './passkeys'

// The WebAuthn JSON conversion and the passkey routes (ADR 0011 sections 1 to 4). Synthetic bytes
// and a faked server only.

afterEach(uninstallWebAuthn)

const bytesOf = (buffer: ArrayBuffer | BufferSource | undefined) => [...new Uint8Array(buffer as ArrayBuffer)]

describe('base64url', () => {
  it('round-trips every byte value without padding', () => {
    const all = new Uint8Array(256).map((_, i) => i)
    const text = toB64url(all)
    expect(text).not.toMatch(/[+/=]/)
    expect(bytesOf(fromB64url(text))).toEqual([...all])
  })

  it.each([['AQID', [1, 2, 3]], ['AQI', [1, 2]], ['AQ', [1]], ['', []], ['-_8', [251, 255]]])('decodes %s', (text, expected) => {
    expect(bytesOf(fromB64url(text))).toEqual(expected)
  })

  it('refuses text that is not base64url', () => {
    expect(() => fromB64url('AQ==')).toThrow(DecodeError)
    expect(() => fromB64url('a+b/')).toThrow(DecodeError)
    expect(() => fromB64url('A')).toThrow(DecodeError)
  })
})

describe('options for the browser', () => {
  it('turns the registration options into buffers and keeps the rest', () => {
    const options = creationOptions(REGISTRATION_OPTIONS)
    expect(new TextDecoder().decode(options.challenge as ArrayBuffer)).toBe('challenge')
    expect(new TextDecoder().decode(options.user.id as ArrayBuffer)).toBe('user')
    expect(options.user.name).toBe('ann@example.test')
    expect(bytesOf(options.excludeCredentials?.[0]?.id)).toEqual([1, 2, 3])
    expect(options.excludeCredentials?.[0]?.transports).toEqual(['internal'])
    expect(options.authenticatorSelection?.userVerification).toBe('required')
    expect(options.attestation).toBe('none')
    expect(options.rp.id).toBe('localhost')
  })

  it('turns sign-in options into buffers, with no credential list for a discoverable sign-in', () => {
    const options = requestOptions(SIGNIN_OPTIONS)
    expect(new TextDecoder().decode(options.challenge as ArrayBuffer)).toBe('signin')
    expect(options.allowCredentials).toBeUndefined()
    expect(options.userVerification).toBe('required')
    const reauth = requestOptions({ ...SIGNIN_OPTIONS, allowCredentials: [{ id: 'AQID', type: 'public-key' }] })
    expect(bytesOf(reauth.allowCredentials?.[0]?.id)).toEqual([1, 2, 3])
  })

  it('refuses options without a challenge', () => {
    expect(() => creationOptions({ ...REGISTRATION_OPTIONS, challenge: undefined })).toThrow(DecodeError)
    expect(() => requestOptions(null)).toThrow(DecodeError)
  })
})

describe("the browser's answer as JSON", () => {
  it('writes a new passkey with its transports', () => {
    expect(attestationJson(fakeAttestation())).toEqual({
      id: 'AQID',
      rawId: 'AQID',
      type: 'public-key',
      response: { clientDataJSON: 'BAUG', attestationObject: 'BwgJ', transports: ['internal', 'hybrid'] },
      authenticatorAttachment: 'platform',
    })
  })

  it('writes a signature with its user handle', () => {
    expect(assertionJson(fakeAssertion())).toEqual({
      id: 'AQID',
      rawId: 'AQID',
      type: 'public-key',
      response: { clientDataJSON: 'BAUG', authenticatorData: 'Cgs', signature: 'DA3_', userHandle: 'Dg' },
      authenticatorAttachment: 'cross-platform',
    })
  })
})

describe('the ceremonies', () => {
  it('knows when the browser has no WebAuthn', () => {
    expect(passkeysSupported()).toBe(false)
    installWebAuthn()
    expect(passkeysSupported()).toBe(true)
  })

  it.each([
    ['NotAllowedError', 'cancelled'],
    ['AbortError', 'cancelled'],
    ['InvalidStateError', 'exists'],
  ])('turns a %s into "%s"', async (name, why) => {
    installWebAuthn({ create: () => Promise.reject(new DOMException('no', name)) })
    const options = creationOptions(REGISTRATION_OPTIONS)
    await expect(createPasskey(options)).rejects.toEqual(new PasskeyCancelled(why as 'cancelled'))
    await createPasskey(options).catch((error: unknown) => {
      expect((error as PasskeyCancelled).why).toBe(why)
    })
  })

  it('treats a missing credential as cancelled and passes other errors on', async () => {
    installWebAuthn({ create: () => Promise.resolve(null) })
    await expect(createPasskey(creationOptions(REGISTRATION_OPTIONS))).rejects.toBeInstanceOf(PasskeyCancelled)
    installWebAuthn({ create: () => Promise.reject(new TypeError('broken')) })
    await expect(createPasskey(creationOptions(REGISTRATION_OPTIONS))).rejects.toBeInstanceOf(TypeError)
  })

  it('signs in: options, the browser, then the signature', async () => {
    const { get } = installWebAuthn()
    const api = fakeApi({
      'POST /api/auth/passkeys/signin/options': SIGNIN_OPTIONS,
      'POST /api/auth/passkeys/signin': { id: 'u1', email: 'ann@example.test', role: 'user' },
    })
    await expect(signInWithPasskey()).resolves.toEqual({ email: 'ann@example.test' })
    expect(get).toHaveBeenCalledTimes(1)
    const [sent] = api.to('POST', '/api/auth/passkeys/signin')
    expect(sent?.body).toEqual({ credential: assertionJson(fakeAssertion()) })
    expect(sent?.headers.get('X-CSRF-Token')).toBe('csrf-anon')
  })

  it('reports a refused sign-in as the server worded it', async () => {
    installWebAuthn()
    fakeApi({
      'POST /api/auth/passkeys/signin/options': SIGNIN_OPTIONS,
      'POST /api/auth/passkeys/signin': reason(401, 'passkey_failed'),
    })
    await expect(signInWithPasskey()).rejects.toMatchObject({ status: 401, reason: 'passkey_failed' })
  })
})

describe('the management routes', () => {
  it('lists, renames and removes', async () => {
    const row = { id: 'p1', name: 'Laptop', created_at: '2030-01-01T07:30:00Z', last_used_at: null, synced: true, backed_up: true, suspended: false }
    const api = fakeApi({
      'GET /api/auth/session': { authenticated: false, csrf_token: 'csrf-anon' },
      'GET /api/auth/passkeys': { passkeys: [row], limit: 10 },
      'PATCH /api/auth/passkeys/p1': { ...row, name: 'Work laptop' },
      'DELETE /api/auth/passkeys/p1': () => new Response(null, { status: 204 }),
    })
    await expect(listPasskeys()).resolves.toEqual({
      passkeys: [{ id: 'p1', name: 'Laptop', createdAt: row.created_at, lastUsedAt: null, synced: true, backedUp: true, suspended: false }],
      limit: 10,
    })
    await expect(renamePasskey('p1', 'Work laptop')).resolves.toMatchObject({ name: 'Work laptop' })
    expect(api.to('PATCH', '/api/auth/passkeys/p1')[0]?.body).toEqual({ name: 'Work laptop' })
    await removePasskey('p1')
    expect(api.to('DELETE', '/api/auth/passkeys/p1')).toHaveLength(1)
  })

  it('refuses a list it cannot read', async () => {
    fakeApi({ 'GET /api/auth/passkeys': { passkeys: [{ id: 1 }], limit: 10 } })
    await expect(listPasskeys()).rejects.toBeInstanceOf(DecodeError)
    fakeApi({ 'GET /api/auth/passkeys': reason(404, 'passkeys_unavailable') })
    await expect(listPasskeys()).rejects.toBeInstanceOf(ApiError)
  })
})
