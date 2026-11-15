import { vi } from 'vitest'

// A browser's WebAuthn API for the tests: `navigator.credentials` and `PublicKeyCredential`
// (jsdom has neither). The credentials are made-up bytes, not a real authenticator's: the server
// side is faked by `fakeApi`, so nothing here is ever verified. Synthetic throughout.

const bytes = (...values: number[]) => new Uint8Array(values).buffer

/** A new passkey as `navigator.credentials.create` returns it. */
export function fakeAttestation(): PublicKeyCredential {
  return {
    id: 'AQID',
    rawId: bytes(1, 2, 3),
    type: 'public-key',
    authenticatorAttachment: 'platform',
    response: {
      clientDataJSON: bytes(4, 5, 6),
      attestationObject: bytes(7, 8, 9),
      getTransports: () => ['internal', 'hybrid'],
    },
  } as unknown as PublicKeyCredential
}

/** A signature as `navigator.credentials.get` returns it. */
export function fakeAssertion(): PublicKeyCredential {
  return {
    id: 'AQID',
    rawId: bytes(1, 2, 3),
    type: 'public-key',
    authenticatorAttachment: 'cross-platform',
    response: {
      clientDataJSON: bytes(4, 5, 6),
      authenticatorData: bytes(10, 11),
      signature: bytes(12, 13, 255),
      userHandle: bytes(14),
    },
  } as unknown as PublicKeyCredential
}

/** Registration options as py_webauthn's `options_to_json` writes them (made-up values). */
export const REGISTRATION_OPTIONS = {
  rp: { name: 'ownvitals', id: 'localhost' },
  user: { id: 'dXNlcg', name: 'ann@example.test', displayName: 'ann@example.test' },
  challenge: 'Y2hhbGxlbmdl',
  pubKeyCredParams: [{ type: 'public-key', alg: -7 }],
  timeout: 60000,
  excludeCredentials: [{ id: 'AQID', type: 'public-key', transports: ['internal'] }],
  authenticatorSelection: { residentKey: 'required', requireResidentKey: true, userVerification: 'required' },
  attestation: 'none',
}

/** Sign-in options: discoverable, so no `allowCredentials`. */
export const SIGNIN_OPTIONS = { challenge: 'c2lnbmlu', timeout: 60000, rpId: 'localhost', userVerification: 'required' }

/**
 * Installs the API with `create` and `get` mocks; returns them. Undone by `uninstallWebAuthn`
 * (call it in `afterEach`), so a test without it sees a browser that has no passkeys.
 */
export function installWebAuthn(answers: { create?: () => Promise<unknown>; get?: () => Promise<unknown> } = {}) {
  const create = vi.fn(answers.create ?? (() => Promise.resolve(fakeAttestation())))
  const get = vi.fn(answers.get ?? (() => Promise.resolve(fakeAssertion())))
  vi.stubGlobal('PublicKeyCredential', function PublicKeyCredential() {})
  Object.defineProperty(navigator, 'credentials', { value: { create, get }, configurable: true })
  return { create, get }
}

export function uninstallWebAuthn(): void {
  Reflect.deleteProperty(navigator, 'credentials')
}
