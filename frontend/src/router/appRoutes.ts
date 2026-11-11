// The screens of the signed-in app, by address: the information architecture of
// docs/design/README.md ("Information architecture"). Every level is a URL. A screen that is not
// built yet has an entry here and shows its page header and the "Coming later" pattern; the
// issue that builds the screen replaces its page in src/pages/appPages.tsx.
//
// The signed-out pages, the second-factor pages and the mail-link pages are not here: they sit
// outside the app frame (src/App.tsx).

import type { MessageKey } from '../i18n/en'

/** The item of the navigation that a screen belongs to. */
export type NavId =
  | 'overview'
  | 'body'
  | 'heart'
  | 'nerve'
  | 'sleep'
  | 'activity'
  | 'findings'
  | 'connections'
  | 'settings'
  | 'security'
  | 'admin'

export type RouteId =
  | 'overview'
  | 'body'
  | 'heart'
  | 'nerve'
  | 'sleep'
  | 'activity'
  | 'metric'
  | 'day'
  | 'ecg-list'
  | 'ecg'
  | 'findings'
  | 'assistant'
  | 'connections'
  | 'settings'
  | 'security'
  | 'notifications'
  | 'data'
  | 'admin'

export interface AppRoute {
  id: RouteId
  /** The path; `:name` matches one segment. */
  pattern: string
  /** Other paths of the same screen (`/settings` shows the first tab, `/settings/profile`). */
  also?: readonly string[]
  /** The catalogue key of the page header (the h1 of the top bar, and the start of the document title). */
  title: MessageKey
  /** What the placeholder card calls the screen, where that differs from the title. */
  label?: MessageKey
  /** What the "Coming later" placeholder says. */
  later: MessageKey
  /** The navigation item that is current on this screen. */
  nav: NavId
  /** The Admin area is shown to admins only (the server enforces it as well). */
  adminOnly?: boolean
}

export const APP_ROUTES: readonly AppRoute[] = [
  { id: 'overview', pattern: '/overview', title: 'shell.route.overview.title', later: 'shell.route.overview.later', nav: 'overview' },
  { id: 'body', pattern: '/body', title: 'shell.route.body.title', later: 'shell.route.body.later', nav: 'body' },
  { id: 'heart', pattern: '/heart', title: 'shell.route.heart.title', later: 'shell.route.heart.later', nav: 'heart' },
  { id: 'nerve', pattern: '/nerve', title: 'shell.route.nerve.title', later: 'shell.route.nerve.later', nav: 'nerve' },
  { id: 'sleep', pattern: '/sleep', title: 'shell.route.sleep.title', later: 'shell.route.sleep.later', nav: 'sleep' },
  { id: 'activity', pattern: '/activity', title: 'shell.route.activity.title', later: 'shell.route.activity.later', nav: 'activity' },
  { id: 'metric', pattern: '/m/:metric', title: 'shell.route.metric.title', later: 'shell.route.metric.later', nav: 'body' },
  { id: 'day', pattern: '/m/:metric/:day', title: 'shell.route.day.title', later: 'shell.route.day.later', nav: 'body' },
  { id: 'ecg-list', pattern: '/heart/ecg', title: 'shell.route.ecg-list.title', later: 'shell.route.ecg-list.later', nav: 'heart' },
  { id: 'ecg', pattern: '/heart/ecg/:id', title: 'shell.route.ecg.title', later: 'shell.route.ecg.later', nav: 'heart' },
  { id: 'findings', pattern: '/findings', title: 'shell.route.findings.title', later: 'shell.route.findings.later', nav: 'findings' },
  // Issue 208: the chat assistant, in the Settings group (no rail item; docs/design/README.md).
  { id: 'assistant', pattern: '/assistant', title: 'shell.route.assistant.title', later: 'shell.route.assistant.later', nav: 'settings' },
  {
    id: 'connections',
    pattern: '/connections',
    title: 'shell.route.connections.title',
    later: 'shell.route.connections.later',
    nav: 'connections',
  },
  // The tabs of Settings: /settings/profile, /settings/security, /settings/notifications and
  // /settings/data (docs/design/README.md); /settings shows the first.
  {
    id: 'settings',
    pattern: '/settings',
    also: ['/settings/profile'],
    title: 'shell.route.settings.title',
    label: 'shell.route.settings.label',
    later: 'shell.route.settings.later',
    nav: 'settings',
  },
  {
    id: 'security',
    pattern: '/settings/security',
    title: 'shell.route.security.title',
    label: 'shell.route.security.label',
    later: 'shell.route.security.later',
    nav: 'security',
  },
  {
    id: 'notifications',
    pattern: '/settings/notifications',
    title: 'shell.route.notifications.title',
    label: 'shell.route.notifications.label',
    later: 'shell.route.notifications.later',
    nav: 'settings',
  },
  {
    id: 'data',
    pattern: '/settings/data',
    title: 'shell.route.data.title',
    label: 'shell.route.data.label',
    later: 'shell.route.data.later',
    nav: 'settings',
  },
  {
    id: 'admin',
    pattern: '/admin',
    title: 'shell.route.admin.title',
    later: 'shell.route.admin.later',
    nav: 'admin',
    adminOnly: true,
  },
]

export interface RouteMatch {
  route: AppRoute
  /** The values of the `:name` segments, decoded. */
  params: Record<string, string>
}

function segments(path: string): string[] {
  return path.split('/').filter((segment) => segment !== '')
}

function decode(segment: string): string | null {
  try {
    return decodeURIComponent(segment)
  } catch {
    return null
  }
}

function matchPattern(pattern: string[], parts: string[]): Record<string, string> | null {
  if (pattern.length !== parts.length) return null
  const params: Record<string, string> = {}
  const matches = pattern.every((expected, i) => {
    const actual = parts[i] ?? ''
    if (!expected.startsWith(':')) return expected === actual
    const value = decode(actual)
    if (value === null) return false
    params[expected.slice(1)] = value
    return true
  })
  return matches ? params : null
}

/** The screen at `pathname` (a trailing slash does not matter), or null when it is none of them. */
export function matchAppRoute(pathname: string): RouteMatch | null {
  const parts = segments(pathname)
  for (const route of APP_ROUTES) {
    for (const path of [route.pattern, ...(route.also ?? [])]) {
      const params = matchPattern(segments(path), parts)
      if (params !== null) return { route, params }
    }
  }
  return null
}
