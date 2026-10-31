import { useId } from 'react'
import { Link } from '../../router/Router'
import type { NavId } from '../../router/appRoutes'
import { Icon, Logo } from '../ui/Icon'
import { PHONE_TABS, railGroups, type NavBadge, type NavItem, type NavStatus } from './nav'

// The navigation rail of every app page (docs/design/mockups/src/Rail.dc.html), and, on a phone,
// the tabs at the bottom (the Onboarding board's phone frame). Which of the two shows is the
// stylesheet's choice (src/styles/shell.css): the rail above 760 px, the tabs below.

export type NavStatuses = Partial<Record<NavId, NavStatus>>

function current(item: NavItem, active: NavId | null, path: string): 'page' | 'true' | undefined {
  if (item.id !== active) return undefined
  // The page itself, or a page inside this item's area (a metric under Body).
  return item.to === path ? 'page' : 'true'
}

function Badge({ badge }: { badge: NavBadge }) {
  switch (badge.kind) {
    case 'text':
      return <span className="ov-navlist__end ov-small ov-muted">{badge.text}</span>
    case 'dot':
      return <span className={`ov-navlist__end ov-dot ov-dot--${badge.tone}`} role="img" aria-label={badge.label} />
    case 'count':
      return (
        <span className="ov-navlist__end ov-count ov-count--attn" role="img" aria-label={badge.label}>
          {badge.value}
        </span>
      )
  }
}

interface RailProps {
  /** The item that is current. */
  active: NavId | null
  /** The address of the page, to tell the item's own page from a page inside it. */
  path: string
  admin: boolean
  /**
   * What is known about the items: "3 of 4" on Nerve health, a dot on Activity, a count on
   * Findings, "n/a" on a stream with no data. Nothing is shown for an item that has none; these
   * come from the screens that own the data.
   */
  status?: NavStatuses
}

export function Rail({ active, path, admin, status = {} }: RailProps) {
  const id = useId()
  return (
    <nav className="ov-rail" aria-label="Main">
      <Link className="ov-brand" to="/">
        <Logo />
        ownvitals
      </Link>
      {railGroups(admin).map((group, i) => {
        const labelId = `${id}-${String(i)}`
        return (
          <div className="ov-navgroup" role="group" aria-labelledby={labelId} key={group.label}>
            <span className="ov-eyebrow" id={labelId}>
              {group.label}
            </span>
            <ul className="ov-navlist">
              {group.items.map((item) => {
                const itemStatus = status[item.id]
                return (
                  <li key={item.id}>
                    <Link
                      to={item.to}
                      className={itemStatus?.dim ? 'ov-navlist__dim' : undefined}
                      aria-current={current(item, active, path)}
                    >
                      <Icon name={item.icon} />
                      <span>{item.label}</span>
                      {itemStatus?.badge ? <Badge badge={itemStatus.badge} /> : null}
                    </Link>
                  </li>
                )
              })}
            </ul>
          </div>
        )
      })}
    </nav>
  )
}

/** The four tabs at the bottom of a phone. `active` is the tab of the current page. */
export function PhoneTabs({ active, path }: { active: NavId | null; path: string }) {
  return (
    <nav className="ov-phone-tabs" aria-label="Main tabs">
      {PHONE_TABS.map((item) => (
        <Link key={item.id} to={item.to} aria-current={current(item, active, path)}>
          <Icon name={item.icon} />
          {item.label}
        </Link>
      ))}
    </nav>
  )
}
