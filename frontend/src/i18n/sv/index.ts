import { prefixed } from '../prefix'
import type { Messages } from '../en'
import { account } from './account'
import { auth } from './auth'
import { common } from './common'
import { errors } from './errors'
import { security } from './security'
import { shell } from './shell'
import { ui } from './ui'

// The Swedish catalogue: its own chunk, loaded when the language is chosen (../store.ts).

export const SV_AREAS = { common, errors, auth, account, security, shell, ui }

export const SV: Messages = {
  ...prefixed('common', common),
  ...prefixed('errors', errors),
  ...prefixed('auth', auth),
  ...prefixed('account', account),
  ...prefixed('security', security),
  ...prefixed('shell', shell),
  ...prefixed('ui', ui),
}
