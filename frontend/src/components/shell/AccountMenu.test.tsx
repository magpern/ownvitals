import { fireEvent, render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { RouterProvider } from '../../router/Router'
import { AccountMenu } from './AccountMenu'

// The account menu (docs/design/mockups/src/Auth.dc.html, "Account menu and sign out"): a menu
// button with Settings, Security and devices, Admin for an admin, and Sign out.

beforeEach(() => {
  window.history.replaceState(null, '', '/')
})

function setup(props: { admin?: boolean; onSignOut?: () => void } = {}) {
  const user = userEvent.setup()
  const onSignOut = props.onSignOut ?? vi.fn()
  render(
    <RouterProvider>
      <p>Outside</p>
      <AccountMenu email="ann@example.test" admin={props.admin ?? false} onSignOut={onSignOut} />
    </RouterProvider>,
  )
  return { user, onSignOut, button: screen.getByRole('button', { name: /ann@example\.test/ }) }
}

const itemNames = () => within(screen.getByRole('menu', { name: 'Account' })).getAllByRole('menuitem').map((item) => item.textContent)

describe('AccountMenu', () => {
  it('shows who is signed in on a button, and no menu until it is opened', () => {
    const { button } = setup()
    expect(button).toHaveAttribute('aria-haspopup', 'menu')
    expect(button).toHaveAttribute('aria-expanded', 'false')
    expect(button).toHaveTextContent('ann@example.test')
    expect(button).toHaveTextContent('A')
    expect(screen.queryByRole('menu')).toBeNull()
  })

  it('opens on a click, with the items of the mock-up and the address on top', async () => {
    const { user, button } = setup()
    await user.click(button)
    expect(button).toHaveAttribute('aria-expanded', 'true')
    expect(button).toHaveAttribute('aria-controls')
    expect(itemNames()).toEqual(['Settings', 'Security and devices', 'Sign out'])
    expect(screen.getAllByText('ann@example.test')).toHaveLength(2)
    expect(screen.getByRole('menuitem', { name: 'Settings' })).toHaveAttribute('href', '/settings')
    expect(screen.getByRole('menuitem', { name: 'Security and devices' })).toHaveAttribute('href', '/settings/security')
  })

  it('has Admin for an admin only, before Sign out', async () => {
    const { user, button } = setup({ admin: true })
    await user.click(button)
    expect(itemNames()).toEqual(['Settings', 'Security and devices', 'Admin', 'Sign out'])
    expect(screen.getByRole('menuitem', { name: 'Admin' })).toHaveAttribute('href', '/admin')
  })

  it('closes on a second click of the button', async () => {
    const { user, button } = setup()
    await user.click(button)
    await user.click(button)
    expect(screen.queryByRole('menu')).toBeNull()
    expect(button).toHaveAttribute('aria-expanded', 'false')
  })

  it('moves the focus to the first item when it opens', async () => {
    const { user, button } = setup()
    await user.click(button)
    expect(screen.getByRole('menuitem', { name: 'Settings' })).toHaveFocus()
  })

  it('opens by keyboard: Enter and Space on the button, and the arrow keys', async () => {
    const { user, button } = setup()
    button.focus()
    await user.keyboard('{Enter}')
    expect(screen.getByRole('menuitem', { name: 'Settings' })).toHaveFocus()
    await user.keyboard('{Escape}')
    expect(screen.queryByRole('menu')).toBeNull()
    await user.keyboard(' ')
    expect(screen.getByRole('menu')).toBeInTheDocument()
    await user.keyboard('{Escape}')
    await user.keyboard('{ArrowDown}')
    expect(screen.getByRole('menuitem', { name: 'Settings' })).toHaveFocus()
    await user.keyboard('{Escape}')
    await user.keyboard('{ArrowUp}')
    expect(screen.getByRole('menuitem', { name: 'Sign out' })).toHaveFocus()
  })

  it('moves between the items with the arrow keys, Home and End, and wraps', async () => {
    const { user, button } = setup()
    await user.click(button)
    await user.keyboard('{ArrowDown}')
    expect(screen.getByRole('menuitem', { name: 'Security and devices' })).toHaveFocus()
    await user.keyboard('{ArrowDown}')
    expect(screen.getByRole('menuitem', { name: 'Sign out' })).toHaveFocus()
    await user.keyboard('{ArrowDown}')
    expect(screen.getByRole('menuitem', { name: 'Settings' })).toHaveFocus()
    await user.keyboard('{ArrowUp}')
    expect(screen.getByRole('menuitem', { name: 'Sign out' })).toHaveFocus()
    await user.keyboard('{Home}')
    expect(screen.getByRole('menuitem', { name: 'Settings' })).toHaveFocus()
    await user.keyboard('{End}')
    expect(screen.getByRole('menuitem', { name: 'Sign out' })).toHaveFocus()
  })

  it('closes on Escape and gives the focus back to the button', async () => {
    const { user, button } = setup()
    await user.click(button)
    await user.keyboard('{Escape}')
    expect(screen.queryByRole('menu')).toBeNull()
    expect(button).toHaveFocus()
  })

  it('closes on Tab, and on a click anywhere outside', async () => {
    const { user, button } = setup()
    await user.click(button)
    await user.keyboard('{Tab}')
    expect(screen.queryByRole('menu')).toBeNull()
    await user.click(button)
    expect(screen.getByRole('menu')).toBeInTheDocument()
    await user.click(screen.getByText('Outside'))
    expect(screen.queryByRole('menu')).toBeNull()
  })

  it('stays open for a click inside it that is not on an item', async () => {
    const { user, button } = setup()
    await user.click(button)
    fireEvent.pointerDown(screen.getAllByText('ann@example.test')[1] as HTMLElement)
    expect(screen.getByRole('menu')).toBeInTheDocument()
  })

  it('goes to the page of a link, and closes', async () => {
    const { user, button } = setup()
    await user.click(button)
    await user.click(screen.getByRole('menuitem', { name: 'Security and devices' }))
    expect(window.location.pathname).toBe('/settings/security')
    expect(screen.queryByRole('menu')).toBeNull()
  })

  it('signs out from the menu, once, and closes', async () => {
    const onSignOut = vi.fn()
    const { user, button } = setup({ onSignOut })
    await user.click(button)
    await user.click(screen.getByRole('menuitem', { name: 'Sign out' }))
    expect(onSignOut).toHaveBeenCalledTimes(1)
    expect(screen.queryByRole('menu')).toBeNull()
  })

  it('shows an initial for any address, and a mark for none', () => {
    const { unmount } = render(
      <RouterProvider>
        <AccountMenu email="zed@example.test" admin={false} onSignOut={() => undefined} />
      </RouterProvider>,
    )
    expect(screen.getByRole('button')).toHaveTextContent('Z')
    unmount()
    render(
      <RouterProvider>
        <AccountMenu email="" admin={false} onSignOut={() => undefined} />
      </RouterProvider>,
    )
    expect(screen.getByRole('button')).toHaveTextContent('?')
  })
})
