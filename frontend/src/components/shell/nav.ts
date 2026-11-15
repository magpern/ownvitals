import type { MessageKey } from '../../i18n/en'
import type { NavId } from '../../router/appRoutes'
import type { IconName } from '../ui/Icon'

// The navigation of the app: the items and their order are those of the Rail component in the
// mock-ups (docs/design/mockups/src/Rail.dc.html), and of the phone tabs of the Onboarding board.

export interface NavItem {
  id: NavId
  /** The catalogue key of the name. */
  label: MessageKey
  to: string
  icon: IconName
}

export interface NavGroup {
  label: MessageKey
  items: readonly NavItem[]
}

const OVERVIEW: NavItem = { id: 'overview', label: 'shell.nav.overview', to: '/overview', icon: 'overview' }
const FINDINGS: NavItem = { id: 'findings', label: 'shell.nav.findings', to: '/findings', icon: 'findings' }
const CONNECTIONS: NavItem = { id: 'connections', label: 'shell.nav.connections', to: '/connections', icon: 'connections' }
const SETTINGS: NavItem = { id: 'settings', label: 'shell.nav.settings', to: '/settings', icon: 'settings' }
const SECURITY: NavItem = { id: 'security', label: 'shell.nav.security', to: '/settings/security', icon: 'security' }
const ADMIN: NavItem = { id: 'admin', label: 'shell.nav.admin', to: '/admin', icon: 'admin' }

/** The groups of the rail. Admin is the last item of Account, and only an admin has it. */
export function railGroups(admin: boolean): readonly NavGroup[] {
  return [
    {
      label: 'shell.nav.analyse',
      items: [
        OVERVIEW,
        { id: 'body', label: 'shell.nav.body', to: '/body', icon: 'body' },
        { id: 'heart', label: 'shell.nav.heart', to: '/heart', icon: 'heart' },
        { id: 'nerve', label: 'shell.nav.nerve', to: '/nerve', icon: 'nerve' },
        { id: 'sleep', label: 'shell.nav.sleep', to: '/sleep', icon: 'sleep' },
        { id: 'activity', label: 'shell.nav.activity', to: '/activity', icon: 'activity' },
        FINDINGS,
      ],
    },
    { label: 'shell.nav.yourData', items: [CONNECTIONS] },
    { label: 'shell.nav.account', items: admin ? [SETTINGS, SECURITY, ADMIN] : [SETTINGS, SECURITY] },
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
  { ...CONNECTIONS, label: 'shell.nav.data' },
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
