// The screens of the signed-in app, by address: the information architecture of
// docs/design/README.md ("Information architecture"). Every level is a URL. A screen that is not
// built yet has an entry here and shows its page header and the "Coming later" pattern; the
// issue that builds the screen replaces its page in src/pages/appPages.tsx.
//
// The signed-out pages, the second-factor pages and the mail-link pages are not here: they sit
// outside the app frame (src/App.tsx).

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
  | 'connections'
  | 'settings'
  | 'security'
  | 'admin'

export interface AppRoute {
  id: RouteId
  /** The path; `:name` matches one segment. */
  pattern: string
  /** The page header (the h1 of the top bar, and the start of the document title). */
  title: string
  /** What the placeholder card calls the screen, where that differs from the title. */
  label?: string
  /** What the "Coming later" placeholder says. */
  later: string
  /** The navigation item that is current on this screen. */
  nav: NavId
  /** The Admin area is shown to admins only (the server enforces it as well). */
  adminOnly?: boolean
}

export const APP_ROUTES: readonly AppRoute[] = [
  { id: 'overview', pattern: '/', title: 'Overview', later: 'The dashboard comes in M6.', nav: 'overview' },
  { id: 'body', pattern: '/body', title: 'Body', later: 'The body page comes in M6.', nav: 'body' },
  { id: 'heart', pattern: '/heart', title: 'Heart', later: 'The heart page comes in M6.', nav: 'heart' },
  { id: 'nerve', pattern: '/nerve', title: 'Nerve health', later: 'The nerve health page comes in M6.', nav: 'nerve' },
  { id: 'sleep', pattern: '/sleep', title: 'Sleep', later: 'The sleep page comes in M6.', nav: 'sleep' },
  { id: 'activity', pattern: '/activity', title: 'Activity', later: 'The activity page comes in M6.', nav: 'activity' },
  { id: 'metric', pattern: '/m/:metric', title: 'Metric', later: 'The metric page comes in M6.', nav: 'body' },
  { id: 'day', pattern: '/m/:metric/:day', title: 'Day', later: 'The day page comes in M6.', nav: 'body' },
  { id: 'ecg-list', pattern: '/heart/ecg', title: 'ECG recordings', later: 'The ECG list comes in M6.', nav: 'heart' },
  { id: 'ecg', pattern: '/heart/ecg/:id', title: 'ECG recording', later: 'The ECG viewer comes in M6.', nav: 'heart' },
  { id: 'findings', pattern: '/findings', title: 'Findings', later: 'The findings page comes in M6.', nav: 'findings' },
  {
    id: 'connections',
    pattern: '/connections',
    title: 'Connections',
    later: 'The connections page comes in M6.',
    nav: 'connections',
  },
  { id: 'settings', pattern: '/settings', title: 'Settings', later: 'The settings page comes in M6.', nav: 'settings' },
  {
    id: 'security',
    pattern: '/settings/security',
    title: 'Settings',
    label: 'Sign-in and security',
    later: 'The sign-in and security page comes in M6.',
    nav: 'security',
  },
  {
    id: 'admin',
    pattern: '/admin',
    title: 'Admin',
    later: 'The admin pages come later.',
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

/** The screen at `pathname` (a trailing slash does not matter), or null when it is none of them. */
export function matchAppRoute(pathname: string): RouteMatch | null {
  const parts = segments(pathname)
  for (const route of APP_ROUTES) {
    const pattern = segments(route.pattern)
    if (pattern.length !== parts.length) continue
    const params: Record<string, string> = {}
    const matches = pattern.every((expected, i) => {
      const actual = parts[i] ?? ''
      if (!expected.startsWith(':')) return expected === actual
      const value = decode(actual)
      if (value === null) return false
      params[expected.slice(1)] = value
      return true
    })
    if (matches) return { route, params }
  }
  return null
}
