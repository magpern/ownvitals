import { prefixed, type Prefixed } from '../prefix'
import { account } from './account'
import { auth } from './auth'
import { codes } from './codes'
import { common } from './common'
import { connections } from './connections'
import { detail } from './detail'
import { errors } from './errors'
import { health } from './health'
import { overview } from './overview'
import { passkeys } from './passkeys'
import { security } from './security'
import { settings } from './settings'
import { shell } from './shell'
import { ui } from './ui'

// English is the source of every message and the fallback for a key a language lacks. An area is
// one file in this folder, and one line here and in ../sv/index.ts.

export const EN_AREAS = { common, errors, codes, detail, health, auth, account, security, shell, ui, overview, settings, passkeys, connections }

export type Areas = typeof EN_AREAS
export type MessageKey = {
  [A in Extract<keyof Areas, string>]: keyof Prefixed<A, Areas[A]>
}[Extract<keyof Areas, string>]
export type Messages = Readonly<Record<MessageKey, string>>

export const EN: Messages = {
  ...prefixed('common', common),
  ...prefixed('errors', errors),
  ...prefixed('codes', codes),
  ...prefixed('detail', detail),
  ...prefixed('health', health),
  ...prefixed('auth', auth),
  ...prefixed('account', account),
  ...prefixed('security', security),
  ...prefixed('shell', shell),
  ...prefixed('ui', ui),
  ...prefixed('overview', overview),
  ...prefixed('settings', settings),
  ...prefixed('passkeys', passkeys),
  ...prefixed('connections', connections),
}
