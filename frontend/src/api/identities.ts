import { del, get, post } from './client'
import { flag, list, object, optionalText, strings, text } from './decode'

// The sign-in methods linked to the signed-in account (ADR 0003 sections 1, 5 and 6): Google is
// the only provider. Listing needs a session; linking and unlinking need recent authentication
// and a step-up, so the page runs them through `useProtected()`. The server decides everything:
// it only touches the user's own identities and refuses to remove the last sign-in method.

export interface Identity {
  id: string
  provider: string
  /** The provider's address when it was linked; absent for an old row. */
  emailAtLink: string | null
}

export interface Identities {
  identities: Identity[]
  hasPassword: boolean
  /** The providers this server has enabled. */
  available: string[]
}

export async function getIdentities(signal?: AbortSignal): Promise<Identities> {
  const body = object(await get('/api/auth/identities', signal), 'identities')
  return {
    identities: list(body, 'identities').map((item) => {
      const row = object(item, 'identity')
      return { id: text(row, 'id'), provider: text(row, 'provider'), emailAtLink: optionalText(row, 'email_at_link') }
    }),
    hasPassword: flag(body, 'has_password'),
    available: strings(body, 'available'),
  }
}

/** The authorization URL that links `provider` to this account; the caller sends the browser there. */
export async function startOAuthLink(provider: string): Promise<string> {
  const body = object(
    await post(`/api/auth/oauth/${encodeURIComponent(provider)}/start`, {
      purpose: 'link',
      invite_code: null,
      return_to: null, // the callback always comes back to /account?oauth=<outcome>
    }),
    'oauth start',
  )
  return text(body, 'authorization_url')
}

export async function unlinkIdentity(id: string): Promise<void> {
  await del(`/api/auth/identities/${encodeURIComponent(id)}`)
}
