import type { SessionState } from '../../api/auth'
import type { NavId } from '../../router/appRoutes'
import type { IconName } from '../ui/Icon'

// The navigation of the app: the items and their order are those of the Rail component in the
// mock-ups (docs/design/mockups/src/Rail.dc.html), and of the phone tabs of the Onboarding board.

export interface NavItem {
  id: NavId
  label: string
  to: string
  icon: IconName
}

export interface NavGroup {
  label: string
  items: readonly NavItem[]
}

const OVERVIEW: NavItem = { id: 'overview', label: 'Overview', to: '/', icon: 'overview' }
const FINDINGS: NavItem = { id: 'findings', label: 'Findings', to: '/findings', icon: 'findings' }
const CONNECTIONS: NavItem = { id: 'connections', label: 'Connections', to: '/connections', icon: 'connections' }
const SETTINGS: NavItem = { id: 'settings', label: 'Settings', to: '/settings', icon: 'settings' }
const SECURITY: NavItem = { id: 'security', label: 'Security', to: '/settings/security', icon: 'security' }
const ADMIN: NavItem = { id: 'admin', label: 'Admin', to: '/admin', icon: 'admin' }

/** The groups of the rail. Admin is the last item of Account, and only an admin has it. */
export function railGroups(admin: boolean): readonly NavGroup[] {
  return [
    {
      label: 'Analyse',
      items: [
        OVERVIEW,
        { id: 'body', label: 'Body', to: '/body', icon: 'body' },
        { id: 'heart', label: 'Heart & ECG', to: '/heart', icon: 'heart' },
        { id: 'nerve', label: 'Nerve health', to: '/nerve', icon: 'nerve' },
        { id: 'sleep', label: 'Sleep', to: '/sleep', icon: 'sleep' },
        { id: 'activity', label: 'Activity', to: '/activity', icon: 'activity' },
        FINDINGS,
      ],
    },
    { label: 'Your data', items: [CONNECTIONS] },
    { label: 'Account', items: admin ? [SETTINGS, SECURITY, ADMIN] : [SETTINGS, SECURITY] },
  ]
}

/** What the rail shows at the end of an item, when something is known about it. */
export type NavBadge =
  /** "3 of 4", "n/a". */
  | { kind: 'text'; text: string }
  /** A status dot; `label` says what it means (it is read out). */
  | { kind: 'dot'; tone: 'ok' | 'attn'; label: string }
  /** A count that wants a look, such as the live findings. */
  | { kind: 'count'; value: number; label: string }

export interface NavStatus {
  badge?: NavBadge
  /** A stream that has no data is greyed. */
  dim?: boolean
}

/** The tabs at the bottom of a phone: the four places the Onboarding board shows. */
export const PHONE_TABS: readonly NavItem[] = [
  OVERVIEW,
  FINDINGS,
  { ...CONNECTIONS, label: 'Data' },
  SETTINGS,
]

/**
 * The phone tab a screen belongs to: the pages of an area and of a metric are reached from the
 * overview; Security is a tab of Settings. Admin has no tab (it is in the account menu).
 */
export function phoneTabOf(nav: NavId): NavId | null {
  switch (nav) {
    case 'overview':
    case 'body':
    case 'heart':
    case 'nerve':
    case 'sleep':
    case 'activity':
      return 'overview'
    case 'findings':
      return 'findings'
    case 'connections':
      return 'connections'
    case 'settings':
    case 'security':
      return 'settings'
    case 'admin':
      return null
  }
}

/** Whether this session is an administrator's (the server decides what an admin may do). */
export function isAdmin(session: SessionState | null): boolean {
  return session?.authenticated === true && session.user?.role === 'admin'
}
