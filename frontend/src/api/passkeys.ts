import { del, get, post, request } from './client'
import { DecodeError, count, flag, isObject, list, object, optionalText, text, type JsonObject } from './decode'

// Passkeys (ADR 0011 sections 1 to 4; docs/dev/auth-passkeys.md). The server speaks the JSON form
// of WebAuthn (base64url without padding for every binary field); the browser's API wants
// ArrayBuffers. The conversion is here, so no page touches a byte. Nothing is stored in the
// browser: a challenge lives only in the server's row and in the one ceremony that uses it.

// --- base64url --------------------------------------------------------------------------------

const B64URL = /^[A-Za-z0-9_-]*$/

/** The bytes of a base64url text (no padding, as the server sends it). */
export function fromB64url(value: string): ArrayBuffer {
  if (!B64URL.test(value) || value.length % 4 === 1) throw new DecodeError('base64url')
  const padded = value.replace(/-/g, '+').replace(/_/g, '/') + '='.repeat((4 - (value.length % 4)) % 4)
  const binary = atob(padded)
  const bytes = new Uint8Array(binary.length)
  for (let i = 0; i < binary.length; i += 1) bytes[i] = binary.charCodeAt(i)
  return bytes.buffer
}

/** Bytes as base64url without padding, as the server reads them. */
export function toB64url(value: ArrayBuffer | ArrayBufferView): string {
  const bytes =
    value instanceof ArrayBuffer ? new Uint8Array(value) : new Uint8Array(value.buffer, value.byteOffset, value.byteLength)
  let binary = ''
  for (const byte of bytes) binary += String.fromCharCode(byte)
  return btoa(binary).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '')
}

// --- options: JSON to the browser's form ------------------------------------------------------

function descriptors(source: JsonObject, key: string): PublicKeyCredentialDescriptor[] | undefined {
  if (source[key] === undefined) return undefined
  return list(source, key).map((item) => {
    const row = object(item, key)
    const transports = row.transports
    return {
      type: 'public-key',
      id: fromB64url(text(row, 'id')),
      ...(Array.isArray(transports) ? { transports: transports.filter((t) => typeof t === 'string') as AuthenticatorTransport[] } : {}),
    }
  })
}

/** Registration options as `navigator.credentials.create` takes them. */
export function creationOptions(json: unknown): PublicKeyCredentialCreationOptions {
  const source = object(json, 'registration options')
  const user = object(source.user, 'user')
  return {
    ...(source as unknown as PublicKeyCredentialCreationOptions),
    challenge: fromB64url(text(source, 'challenge')),
    user: { id: fromB64url(text(user, 'id')), name: text(user, 'name'), displayName: text(user, 'displayName') },
    excludeCredentials: descriptors(source, 'excludeCredentials') ?? [],
  }
}

/** Sign-in or re-authentication options as `navigator.credentials.get` takes them. */
export function requestOptions(json: unknown): PublicKeyCredentialRequestOptions {
  const source = object(json, 'sign-in options')
  const allow = descriptors(source, 'allowCredentials')
  return {
    ...(source as unknown as PublicKeyCredentialRequestOptions),
    challenge: fromB64url(text(source, 'challenge')),
    ...(allow === undefined ? {} : { allowCredentials: allow }),
  }
}

// --- the browser's answer: to JSON ------------------------------------------------------------

function attachment(credential: PublicKeyCredential): string | undefined {
  const value = credential.authenticatorAttachment
  return value === 'platform' || value === 'cross-platform' ? value : undefined
}

/** A new passkey, as `POST /register` reads it. */
export function attestationJson(credential: PublicKeyCredential): JsonObject {
  const response = credential.response as AuthenticatorAttestationResponse
  const transports = typeof response.getTransports === 'function' ? response.getTransports() : []
  return {
    id: credential.id,
    rawId: toB64url(credential.rawId),
    type: credential.type,
    response: {
      clientDataJSON: toB64url(response.clientDataJSON),
      attestationObject: toB64url(response.attestationObject),
      transports,
    },
    authenticatorAttachment: attachment(credential),
  }
}

/** A signature with a passkey, as `POST /signin` and `POST /reauth` read it. */
export function assertionJson(credential: PublicKeyCredential): JsonObject {
  const response = credential.response as AuthenticatorAssertionResponse
  return {
    id: credential.id,
    rawId: toB64url(credential.rawId),
    type: credential.type,
    response: {
      clientDataJSON: toB64url(response.clientDataJSON),
      authenticatorData: toB64url(response.authenticatorData),
      signature: toB64url(response.signature),
      userHandle: response.userHandle === null ? null : toB64url(response.userHandle),
    },
    authenticatorAttachment: attachment(credential),
  }
}

/** Whether this browser has WebAuthn at all (an old one, or an insecure origin, has not). */
export function passkeysSupported(): boolean {
  return (
    typeof window !== 'undefined' &&
    typeof window.PublicKeyCredential === 'function' &&
    typeof navigator.credentials?.create === 'function'
  )
}

/** What a ceremony the user cancelled, or the browser refused, throws: shown as one sentence. */
export class PasskeyCancelled extends Error {
  /** `exists`: this authenticator already holds a passkey of the account. */
  readonly why: 'cancelled' | 'exists'
  constructor(why: 'cancelled' | 'exists') {
    super(why)
    this.name = 'PasskeyCancelled'
    this.why = why
  }
}

function asCeremonyError(error: unknown): unknown {
  if (error instanceof DOMException) {
    if (error.name === 'InvalidStateError') return new PasskeyCancelled('exists')
    if (error.name === 'NotAllowedError' || error.name === 'AbortError') return new PasskeyCancelled('cancelled')
  }
  return error
}

async function ceremony(run: () => Promise<Credential | null>): Promise<PublicKeyCredential> {
  let credential: Credential | null
  try {
    credential = await run()
  } catch (error) {
    throw asCeremonyError(error)
  }
  if (credential === null || credential.type !== 'public-key') throw new PasskeyCancelled('cancelled')
  return credential as PublicKeyCredential
}

// --- the routes -------------------------------------------------------------------------------

export interface Passkey {
  id: string
  name: string
  createdAt: string
  lastUsedAt: string | null
  /** Backup-eligible: synced between the user's devices (shown, never enforced). */
  synced: boolean
  backedUp: boolean
  /** The sign count went backwards: the passkey is refused until it is removed. */
  suspended: boolean
}

export interface Passkeys {
  passkeys: Passkey[]
  limit: number
}

function decodePasskey(value: unknown): Passkey {
  const row = object(value, 'passkey')
  return {
    id: text(row, 'id'),
    name: text(row, 'name'),
    createdAt: text(row, 'created_at'),
    lastUsedAt: optionalText(row, 'last_used_at'),
    synced: flag(row, 'synced'),
    backedUp: flag(row, 'backed_up'),
    suspended: flag(row, 'suspended'),
  }
}

export async function listPasskeys(signal?: AbortSignal): Promise<Passkeys> {
  const body = object(await get('/api/auth/passkeys', signal), 'passkeys')
  return { passkeys: list(body, 'passkeys').map(decodePasskey), limit: count(body, 'limit') }
}

export async function renamePasskey(id: string, name: string): Promise<Passkey> {
  return decodePasskey(await request('PATCH', `/api/auth/passkeys/${encodeURIComponent(id)}`, { body: { name } }))
}

/** Needs recent authentication and a step-up: run it through `useProtected()`. */
export async function removePasskey(id: string): Promise<void> {
  await del(`/api/auth/passkeys/${encodeURIComponent(id)}`)
}

/** Registration options; needs recent authentication and a step-up (`useProtected()`). */
export async function registrationOptions(): Promise<PublicKeyCredentialCreationOptions> {
  return creationOptions(await post('/api/auth/passkeys/register/options'))
}

/** Asks the browser for a new passkey with the options the server gave. */
export function createPasskey(options: PublicKeyCredentialCreationOptions): Promise<PublicKeyCredential> {
  return ceremony(() => navigator.credentials.create({ publicKey: options }))
}

/** Saves the new passkey; the server mails the user. Needs the same proofs as the options. */
export async function registerPasskey(credential: PublicKeyCredential): Promise<Passkey> {
  return decodePasskey(await post('/api/auth/passkeys/register', { credential: attestationJson(credential) }))
}

export interface PasskeySignedIn {
  email: string
}

/**
 * Sign-in with a discoverable passkey: options (a new pre-session cookie), the browser's
 * ceremony (it offers this device's passkeys, a security key and "Use a phone"), then the
 * signature. The session still owes its TOTP, as after a password.
 */
export async function signInWithPasskey(): Promise<PasskeySignedIn> {
  const options = requestOptions(await post('/api/auth/passkeys/signin/options'))
  const credential = await ceremony(() => navigator.credentials.get({ publicKey: options }))
  const body = await post('/api/auth/passkeys/signin', { credential: assertionJson(credential) })
  if (!isObject(body)) throw new DecodeError('signed in')
  return { email: text(body, 'email') }
}

/**
 * Re-authentication with one of the account's passkeys (ADR 0011 section 3): options bound to
 * this session, the browser's ceremony, then the signature. It sets the recent authentication;
 * for an account without an authenticator app it is the step-up too (issue 475).
 */
export async function reauthWithPasskey(): Promise<void> {
  const options = requestOptions(await post('/api/auth/passkeys/reauth/options'))
  const credential = await ceremony(() => navigator.credentials.get({ publicKey: options }))
  await post('/api/auth/passkeys/reauth', { credential: assertionJson(credential) })
}
