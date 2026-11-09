import { prefixed } from '../prefix'
import type { Messages } from '../en'
import { account } from './account'
import { auth } from './auth'
import { codes } from './codes'
import { common } from './common'
import { detail } from './detail'
import { errors } from './errors'
import { health } from './health'
import { overview } from './overview'
import { passkeys } from './passkeys'
import { security } from './security'
import { settings } from './settings'
import { shell } from './shell'
import { ui } from './ui'

// The Swedish catalogue: its own chunk, loaded when the language is chosen (../store.ts).

export const SV_AREAS = { common, errors, codes, detail, health, auth, account, security, shell, ui, overview, settings, passkeys }

export const SV: Messages = {
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
}
