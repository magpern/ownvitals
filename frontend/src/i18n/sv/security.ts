import type { security as en } from '../en/security'

// Swedish for the security area. Until the translation PR (G6) the values are English copies and the
// keys are listed in ../untranslated.ts. `satisfies` makes a missing or extra key a type error.
export const security = {
  band: 'Actions marked with a lock ask for a fresh code from your authenticator app. One code covers 10 minutes of protected actions.',
  laterLabel: 'How you sign in, the second factor and recent activity',
  later: 'Changing the password, Google, email, the authenticator and recovery codes comes in M6.',
  'sessions.title': "Where you're signed in",
  'sessions.signOutEverywhere': 'Sign out everywhere…',
  'sessions.purpose': 'Signing out everywhere',
  'sessions.device': 'Device',
  'sessions.place': 'Place and IP',
  'sessions.signedIn': 'Signed in',
  'sessions.lastActive': 'Last active',
  'sessions.action': 'Action',
  'sessions.thisDevice': 'This device',
  'sessions.unknown': 'Unknown',
  'sessions.signOutDevice': 'Sign out {device}',
  'sessions.expiry': 'A session ends after 72 hours without activity, and after 30 days at most.',
  'device.unknown': 'Unknown device',
  'device.aBrowser': 'A browser',
  'device.on': '{browser} on {system}',
  'time.today': 'Today {time}',
  'ago.minutes.one': '{count} minute ago',
  'ago.minutes.other': '{count} minutes ago',
  'ago.hours.one': '{count} hour ago',
  'ago.hours.other': '{count} hours ago',
  'ago.days.one': '{count} day ago',
  'ago.days.other': '{count} days ago',
  'stepUp.title': "Confirm it's you",
  'stepUp.password': '{purpose} needs your password, then a fresh code from your authenticator app.',
  'stepUp.code':
    '{purpose} needs a fresh code from your authenticator app. It also covers other protected actions for the next 10 minutes.',
  'stepUp.google': 'Use Google instead',
} satisfies Record<keyof typeof en, string>
