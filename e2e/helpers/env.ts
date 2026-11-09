import { resolve } from 'node:path'

export const REPO_ROOT = resolve(import.meta.dirname, '..', '..')

const port = (name: string, fallback: number): number => Number(process.env[name] ?? fallback)

/**
 * web, the origin the browser uses (it must equal OWNVITALS_PUBLIC_BASE_URL of compose.e2e.yaml):
 * localhost, because WebAuthn has no relying party on an IP address.
 */
export const BASE_URL = `http://localhost:${String(port('WEB_PORT', 8000))}`
/** mailpit's inbox and API, published on 127.0.0.1 only. */
export const MAILPIT_URL = `http://127.0.0.1:${String(port('MAILPIT_PORT', 8025))}`
/** fake-oidc's control port (plain http, 127.0.0.1 only). */
export const FAKE_OIDC_URL = `http://127.0.0.1:${String(port('FAKE_OIDC_PORT', 9100))}`
