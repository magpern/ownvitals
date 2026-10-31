import { fireEvent, screen, within } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { APP_ROUTES } from '../../router/appRoutes'
import { MFA_OFF, SIGNED_IN, SIGNED_IN_NO_CODE, fakeApi } from '../../test/fakeApi'
import { renderAt } from '../../test/render'

// The app frame, end to end: what a signed-in user gets around every screen (the rail, the top
// bar with the page header and the account menu, the phone tabs), the screens that are not built
// yet, and the pages that stay outside it.

const ADMIN = { ...SIGNED_IN, user: { ...SIGNED_IN.user, role: 'admin' } }
const here = () => window.location.pathname

/** A path that matches the route, for the ones with parameters. */
const concrete = (pattern: string) => pattern.replace(':metric', 'weight').replace(':day', '2026-10-14').replace(':id', 'ecg-1')

describe('the app frame', () => {
  it('puts the rail, the top bar with the page header, the page and the phone tabs around a screen', async () => {
    fakeApi({ 'GET /api/auth/session': SIGNED_IN })
    renderAt('/')
    const heading = await screen.findByRole('heading', { level: 1, name: 'Overview' })
    expect(within(screen.getByRole('banner')).getByRole('heading', { level: 1 })).toBe(heading)
    const rail = screen.getByRole('navigation', { name: 'Main' })
    expect(within(rail).getByRole('link', { name: 'Overview' })).toHaveAttribute('aria-current', 'page')
    expect(screen.getByRole('navigation', { name: 'Main tabs' })).toBeInTheDocument()
    expect(within(screen.getByRole('main')).getByText('Coming later')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /ann@example\.test/ })).toBeInTheDocument()
    // The top bar's logo (the phone's brand) goes home, too.
    expect(within(screen.getByRole('banner')).getByRole('link', { name: 'ownvitals' })).toHaveAttribute('href', '/')
  })

  it.each(APP_ROUTES.map((route) => [route.id, route] as const))(
    'shows the page header and "Coming later" on the %s screen',
    async (_id, route) => {
      fakeApi({ 'GET /api/auth/session': ADMIN })
      renderAt(concrete(route.pattern))
      expect(await screen.findByRole('heading', { level: 1, name: route.title })).toBeInTheDocument()
      const main = screen.getByRole('main')
      expect(within(main).getByText(route.later)).toBeInTheDocument()
      expect(within(main).getByText(route.label ?? route.title)).toBeInTheDocument()
      expect(within(main).getByText('Coming later')).toBeInTheDocument()
      expect(screen.getByRole('navigation', { name: 'Main' })).toBeInTheDocument()
    },
  )

  it('marks the rail item of the screen, also for a screen inside an area', async () => {
    fakeApi({ 'GET /api/auth/session': SIGNED_IN })
    renderAt('/m/weight')
    await screen.findByRole('heading', { level: 1, name: 'Metric' })
    const rail = screen.getByRole('navigation', { name: 'Main' })
    expect(within(rail).getByRole('link', { name: 'Body' })).toHaveAttribute('aria-current', 'true')
    expect(within(screen.getByRole('navigation', { name: 'Main tabs' })).getByRole('link', { name: 'Overview' })).toHaveAttribute(
      'aria-current',
      'true',
    )
  })

  it('shows Security as the current item of its own page, and the Settings tab on the phone', async () => {
    fakeApi({ 'GET /api/auth/session': SIGNED_IN })
    renderAt('/settings/security')
    expect(await screen.findByRole('heading', { level: 1, name: 'Settings' })).toBeInTheDocument()
    const rail = screen.getByRole('navigation', { name: 'Main' })
    expect(within(rail).getByRole('link', { name: 'Security' })).toHaveAttribute('aria-current', 'page')
    expect(within(rail).getByRole('link', { name: 'Settings' })).not.toHaveAttribute('aria-current')
    expect(within(screen.getByRole('navigation', { name: 'Main tabs' })).getByRole('link', { name: 'Settings' })).toHaveAttribute(
      'aria-current',
      'true',
    )
    expect(within(screen.getByRole('main')).getByText('Sign-in and security')).toBeInTheDocument()
  })

  it('moves to another screen from the rail, without a reload', async () => {
    fakeApi({ 'GET /api/auth/session': SIGNED_IN })
    renderAt('/')
    await screen.findByRole('heading', { level: 1, name: 'Overview' })
    fireEvent.click(within(screen.getByRole('navigation', { name: 'Main' })).getByRole('link', { name: 'Findings' }))
    expect(await screen.findByRole('heading', { level: 1, name: 'Findings' })).toBeInTheDocument()
    expect(here()).toBe('/findings')
    expect(within(screen.getByRole('navigation', { name: 'Main' })).getByRole('link', { name: 'Findings' })).toHaveAttribute(
      'aria-current',
      'page',
    )
  })

  it('moves to another screen from the phone tabs', async () => {
    fakeApi({ 'GET /api/auth/session': SIGNED_IN })
    renderAt('/')
    await screen.findByRole('heading', { level: 1, name: 'Overview' })
    fireEvent.click(within(screen.getByRole('navigation', { name: 'Main tabs' })).getByRole('link', { name: 'Data' }))
    expect(await screen.findByRole('heading', { level: 1, name: 'Connections' })).toBeInTheDocument()
    expect(here()).toBe('/connections')
  })

  it('names the document after the page, and puts the product name back when it leaves', async () => {
    fakeApi({ 'GET /api/auth/session': SIGNED_IN })
    renderAt('/connections')
    await screen.findByRole('heading', { level: 1, name: 'Connections' })
    expect(document.title).toBe('Connections · ownvitals')
    fireEvent.click(within(screen.getByRole('navigation', { name: 'Main' })).getByRole('link', { name: 'Sleep' }))
    await screen.findByRole('heading', { level: 1, name: 'Sleep' })
    expect(document.title).toBe('Sleep · ownvitals')
  })

  it('does not take a trailing slash for another page', async () => {
    fakeApi({ 'GET /api/auth/session': SIGNED_IN })
    renderAt('/body/')
    expect(await screen.findByRole('heading', { level: 1, name: 'Body' })).toBeInTheDocument()
    expect(within(screen.getByRole('navigation', { name: 'Main' })).getByRole('link', { name: 'Body' })).toHaveAttribute(
      'aria-current',
      'page',
    )
  })
})

describe('the Admin area', () => {
  it('is in the rail and the account menu for an admin, and has its screen', async () => {
    fakeApi({ 'GET /api/auth/session': ADMIN })
    renderAt('/')
    await screen.findByRole('heading', { level: 1, name: 'Overview' })
    expect(within(screen.getByRole('navigation', { name: 'Main' })).getByRole('link', { name: 'Admin' })).toHaveAttribute('href', '/admin')
    fireEvent.click(screen.getByRole('button', { name: /ann@example\.test/ }))
    expect(within(await screen.findByRole('menu', { name: 'Account' })).getByRole('menuitem', { name: 'Admin' })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('menuitem', { name: 'Admin' }))
    expect(await screen.findByRole('heading', { level: 1, name: 'Admin' })).toBeInTheDocument()
    expect(within(screen.getByRole('navigation', { name: 'Main' })).getByRole('link', { name: 'Admin' })).toHaveAttribute('aria-current', 'page')
  })

  it('is nowhere for anyone else: not in the rail, not in the menu, and no such page', async () => {
    fakeApi({ 'GET /api/auth/session': SIGNED_IN })
    renderAt('/')
    await screen.findByRole('heading', { level: 1, name: 'Overview' })
    expect(screen.queryByRole('link', { name: 'Admin' })).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: /ann@example\.test/ }))
    expect(within(await screen.findByRole('menu', { name: 'Account' })).queryByRole('menuitem', { name: 'Admin' })).toBeNull()
  })

  it('answers "Page not found" to a user who is not an admin, outside the app frame', async () => {
    fakeApi({ 'GET /api/auth/session': SIGNED_IN })
    renderAt('/admin')
    expect(await screen.findByRole('heading', { name: 'Page not found' })).toBeInTheDocument()
    expect(screen.queryByRole('navigation', { name: 'Main' })).toBeNull()
    expect(screen.queryByText('Coming later')).toBeNull()
  })
})

describe('outside the app frame', () => {
  it('sends a signed-out visitor to sign-in, with no rail', async () => {
    fakeApi({})
    renderAt('/body')
    expect(await screen.findByRole('heading', { name: 'Sign in' })).toBeInTheDocument()
    expect(here()).toBe('/signin')
    expect(screen.queryByRole('navigation', { name: 'Main' })).toBeNull()
    expect(screen.queryByRole('navigation', { name: 'Main tabs' })).toBeNull()
    expect(document.title).toBe('ownvitals')
  })

  it('keeps the sign-in and sign-up pages in the plain frame', async () => {
    fakeApi({})
    renderAt('/signup')
    expect(await screen.findByRole('navigation', { name: 'Account' })).toBeInTheDocument()
    expect(screen.queryByRole('navigation', { name: 'Main' })).toBeNull()
  })

  it('keeps the second-factor pages in the plain frame, with Sign out, and sends a user with no app there', async () => {
    fakeApi({ 'GET /api/auth/session': SIGNED_IN_NO_CODE, 'GET /api/auth/mfa': MFA_OFF })
    renderAt('/connections')
    expect(await screen.findByRole('heading', { name: 'Set up two-factor' })).toBeInTheDocument()
    expect(here()).toBe('/two-factor/setup')
    expect(screen.queryByRole('navigation', { name: 'Main' })).toBeNull()
    expect(screen.getByRole('button', { name: 'Sign out' })).toBeInTheDocument()
  })

  it('shows the product name and nothing of the app while the session loads', () => {
    vi.stubGlobal('fetch', vi.fn(() => new Promise<Response>(() => undefined)))
    renderAt('/')
    expect(screen.getByRole('link', { name: 'ownvitals' })).toBeInTheDocument()
    expect(screen.queryByRole('navigation', { name: 'Main' })).toBeNull()
    expect(screen.queryByRole('heading', { level: 1, name: 'Overview' })).toBeNull()
  })
})
