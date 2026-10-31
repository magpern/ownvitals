import { useEffect, useId, useRef, useState, type KeyboardEvent } from 'react'
import { Link } from '../../router/Router'
import { Icon } from '../ui/Icon'

// The account menu, top right of every app page (docs/design/mockups/src/Auth.dc.html, "Account
// menu and sign out"): who is signed in, Settings, Security and devices, Admin (admins only) and
// Sign out. It is a menu button: Enter, Space or the arrow keys open it; the arrow keys, Home and
// End move between its items; Escape closes it and returns to the button; Tab and a click outside
// close it.

interface AccountMenuProps {
  email: string
  admin: boolean
  onSignOut: () => void
}

export function AccountMenu({ email, admin, onSignOut }: AccountMenuProps) {
  const [open, setOpen] = useState(false)
  const root = useRef<HTMLDivElement>(null)
  const button = useRef<HTMLButtonElement>(null)
  const menu = useRef<HTMLDivElement>(null)
  const menuId = useId()

  const items = (): HTMLElement[] => [...(menu.current?.querySelectorAll<HTMLElement>('[role="menuitem"]') ?? [])]
  const focusItem = (index: number) => {
    const all = items()
    all[(index + all.length) % all.length]?.focus()
  }

  // Opening puts the focus on the first item (on the last, for ArrowUp).
  const startAt = useRef<'first' | 'last'>('first')
  useEffect(() => {
    if (!open) return
    const all = [...(menu.current?.querySelectorAll<HTMLElement>('[role="menuitem"]') ?? [])]
    ;(startAt.current === 'first' ? all[0] : all[all.length - 1])?.focus()
  }, [open])

  useEffect(() => {
    if (!open) return
    const outside = (event: Event) => {
      if (event.target instanceof Node && !root.current?.contains(event.target)) setOpen(false)
    }
    document.addEventListener('pointerdown', outside)
    return () => {
      document.removeEventListener('pointerdown', outside)
    }
  }, [open])

  const close = (returnFocus: boolean) => {
    setOpen(false)
    if (returnFocus) button.current?.focus()
  }
  // A link: the page changes, so the focus is not sent back to the button.
  const closeQuietly = () => {
    close(false)
  }

  const onButtonKey = (event: KeyboardEvent) => {
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault()
      startAt.current = event.key === 'ArrowDown' ? 'first' : 'last'
      setOpen(true)
    }
  }

  const onMenuKey = (event: KeyboardEvent) => {
    const all = items()
    const at = all.indexOf(document.activeElement as HTMLElement)
    switch (event.key) {
      case 'ArrowDown':
        event.preventDefault()
        focusItem(at + 1)
        break
      case 'ArrowUp':
        event.preventDefault()
        focusItem(at - 1)
        break
      case 'Home':
        event.preventDefault()
        focusItem(0)
        break
      case 'End':
        event.preventDefault()
        focusItem(-1)
        break
      case 'Escape':
        event.preventDefault()
        close(true)
        break
      case 'Tab':
        close(false)
        break
    }
  }

  return (
    <div className="ov-acct-wrap" ref={root}>
      <button
        ref={button}
        className="ov-acct"
        type="button"
        aria-haspopup="menu"
        aria-expanded={open}
        aria-controls={open ? menuId : undefined}
        onClick={() => {
          startAt.current = 'first'
          setOpen(!open)
        }}
        onKeyDown={onButtonKey}
      >
        <span className="ov-avatar" aria-hidden="true">
          {(email.charAt(0) || '?').toUpperCase()}
        </span>
        <span className="ov-acct__email">{email}</span>
        <Icon name={open ? 'chevron-up' : 'chevron-down'} size={16} />
      </button>
      {open ? (
        <div className="ov-menu" id={menuId}>
          <p className="ov-menu__who">{email}</p>
          <div role="menu" aria-label="Account" ref={menu} onKeyDown={onMenuKey}>
            <Link role="menuitem" to="/settings" onClick={closeQuietly}>
              <Icon name="settings" />
              Settings
            </Link>
            <Link role="menuitem" to="/settings/security" onClick={closeQuietly}>
              <Icon name="security" />
              Security and devices
            </Link>
            {admin ? (
              <Link role="menuitem" to="/admin" onClick={closeQuietly}>
                <Icon name="admin" />
                Admin
              </Link>
            ) : null}
            <hr className="ov-menu__rule" />
            <button
              role="menuitem"
              type="button"
              onClick={() => {
                close(false)
                onSignOut()
              }}
            >
              <Icon name="signout" />
              Sign out
            </button>
          </div>
        </div>
      ) : null}
    </div>
  )
}
