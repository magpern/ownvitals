import { request } from './client'
import { DecodeError, flag, list, object, text } from './decode'

// The email preference per mail kind (issue 459, ADR 0018): which of the "something is waiting"
// emails the user gets. Account and security emails and a requested export are not listed: they
// always arrive. Every such email also carries a one-click link that switches its kind off.

export const EMAIL_KINDS = ['findings_waiting', 'connection_attention', 'ai_digest_ready'] as const
export type EmailKind = (typeof EMAIL_KINDS)[number]
export type EmailPrefs = Record<EmailKind, boolean>

export const EMAIL_PREFS_PATH = '/api/notify/email'

export function decodeEmailPrefs(raw: unknown): EmailPrefs {
  const prefs: Partial<EmailPrefs> = {}
  for (const item of list(object(raw, 'email preferences'), 'kinds')) {
    const row = object(item, 'email preference')
    const kind = text(row, 'kind')
    if (!(EMAIL_KINDS as readonly string[]).includes(kind)) throw new DecodeError('kind')
    prefs[kind as EmailKind] = flag(row, 'enabled')
  }
  for (const kind of EMAIL_KINDS) if (prefs[kind] === undefined) throw new DecodeError('kinds')
  return prefs as EmailPrefs
}

export async function setEmailPref(kind: EmailKind, enabled: boolean): Promise<EmailPrefs> {
  return decodeEmailPrefs(await request('PUT', EMAIL_PREFS_PATH, { body: { kind, enabled } }))
}
