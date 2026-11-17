import { del, post, put } from './client'
import { count, object, text } from './decode'
import { SETTINGS_PATH } from './adminViews'

// The admin page's writes (issue 550): a flag set or cleared, an invite, "Sign out all". Each is
// an admin write, so the server wants a step-up from the last 5 minutes: callers run these through
// `useProtected()`. The invite code is returned to the caller once and kept nowhere else.

export type BannerLevel = 'info' | 'warning'
/** A mode or switch, a banner, or an AI monthly cap in whole micro-USD. */
export type FlagValue = string | number | { text: string; level: BannerLevel }

function flagPath(key: string): string {
  return `${SETTINGS_PATH}/${encodeURIComponent(key)}`
}

export async function setFlag(key: string, value: FlagValue): Promise<void> {
  await put(flagPath(key), { value })
}

/** Clears the stored value, so the server settings apply again. */
export async function clearFlag(key: string): Promise<void> {
  await del(flagPath(key))
}

export interface NewInvite {
  code: string
  expiresAt: string
}

export async function createInvite(): Promise<NewInvite> {
  const body = object(await post('/api/admin/invites'), 'invite')
  return { code: text(body, 'code'), expiresAt: text(body, 'expires_at') }
}

/** Signs the account out everywhere; answers how many sessions ended. */
export async function signOutAll(userId: string): Promise<number> {
  const body = object(await del(`/api/admin/users/${encodeURIComponent(userId)}/sessions`), 'signed out')
  return count(body, 'sessions_revoked')
}
