import { prefixed } from '../prefix'
import type { Messages } from '../en'
import { account } from './account'
import { auth } from './auth'
import { body } from './body'
import { nerve } from './nerve'
import { assistant } from './assistant'
import { codes } from './codes'
import { common } from './common'
import { connections } from './connections'
import { detail } from './detail'
import { ecg } from './ecg'
import { errors } from './errors'
import { findings } from './findings'
import { health } from './health'
import { overview } from './overview'
import { passkeys } from './passkeys'
import { security } from './security'
import { settings } from './settings'
import { shell } from './shell'
import { ui } from './ui'

// The Swedish catalogue: its own chunk, loaded when the language is chosen (../store.ts).

export const SV_AREAS = { common, errors, codes, detail, health, auth, account, security, shell, ui, overview, settings, passkeys, connections, ecg, findings, body, nerve, assistant }

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
  ...prefixed('connections', connections),
  ...prefixed('ecg', ecg),
  ...prefixed('findings', findings),
  ...prefixed('body', body),
  ...prefixed('nerve', nerve),
  ...prefixed('assistant', assistant),
}
