import { prefixed } from '../prefix'
import type { Messages } from '../en'
import { account } from './account'
import { auth } from './auth'
import { codes } from './codes'
import { common } from './common'
import { detail } from './detail'
import { errors } from './errors'
import { health } from './health'
import { security } from './security'
import { shell } from './shell'
import { ui } from './ui'

// The Swedish catalogue: its own chunk, loaded when the language is chosen (../store.ts).

export const SV_AREAS = { common, errors, codes, detail, health, auth, account, security, shell, ui }

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
}
