import { render, screen, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { RouterProvider } from '../../router/Router'
import { PhoneTabs, Rail } from './Rail'

// The navigation rail and the phone tabs, as the Rail component and the Onboarding board show
// them (docs/design/mockups/src/Rail.dc.html). The items are held to the mock-up in nav.test.tsx.

function inRouter(node: React.ReactNode) {
  return render(<RouterProvider>{node}</RouterProvider>)
}

describe('Rail', () => {
  it('is the main navigation, with the brand, three groups and their items in order', () => {
    inRouter(<Rail active="overview" path="/" admin={false} />)
    const nav = screen.getByRole('navigation', { name: 'Main' })
    expect(within(nav).getByRole('link', { name: 'ownvitals' })).toHaveAttribute('href', '/')
    const groups = within(nav).getAllByRole('group')
    expect(groups.map((group) => group.getAttribute('aria-labelledby')).every(Boolean)).toBe(true)
    expect(within(nav).getByRole('group', { name: 'Analyse' })).toBeInTheDocument()
    expect(within(nav).getByRole('group', { name: 'Your data' })).toBeInTheDocument()
    expect(within(nav).getByRole('group', { name: 'Account' })).toBeInTheDocument()
    expect(
      within(nav)
        .getAllByRole('link')
        .map((link) => link.textContent),
    ).toEqual([
      'ownvitals',
      'Overview',
      'Body',
      'Heart & ECG',
      'Nerve health',
      'Sleep',
      'Activity',
      'Findings',
      'Connections',
      'Settings',
      'Security',
    ])
  })

  it('links each item to its page', () => {
    inRouter(<Rail active={null} path="/" admin />)
    const hrefs = Object.fromEntries(screen.getAllByRole('link').map((link) => [link.textContent, link.getAttribute('href')]))
    expect(hrefs).toMatchObject({
      Overview: '/',
      Body: '/body',
      'Heart & ECG': '/heart',
      'Nerve health': '/nerve',
      Sleep: '/sleep',
      Activity: '/activity',
      Findings: '/findings',
      Connections: '/connections',
      Settings: '/settings',
      Security: '/settings/security',
      Admin: '/admin',
    })
  })

  it('shows Admin only to an admin', () => {
    const { unmount } = inRouter(<Rail active={null} path="/" admin={false} />)
    expect(screen.queryByRole('link', { name: 'Admin' })).toBeNull()
    unmount()
    inRouter(<Rail active={null} path="/" admin />)
    const account = screen.getByRole('group', { name: 'Account' })
    expect(within(account).getAllByRole('link').map((link) => link.textContent)).toEqual(['Settings', 'Security', 'Admin'])
  })

  it('marks the current item: the page itself as the page, a page inside its area as current', () => {
    const { rerender } = inRouter(<Rail active="body" path="/body" admin={false} />)
    expect(screen.getByRole('link', { name: 'Body' })).toHaveAttribute('aria-current', 'page')
    expect(screen.getAllByRole('link').filter((link) => link.hasAttribute('aria-current'))).toHaveLength(1)
    rerender(
      <RouterProvider>
        <Rail active="body" path="/m/weight" admin={false} />
      </RouterProvider>,
    )
    expect(screen.getByRole('link', { name: 'Body' })).toHaveAttribute('aria-current', 'true')
    rerender(
      <RouterProvider>
        <Rail active="security" path="/settings/security" admin={false} />
      </RouterProvider>,
    )
    expect(screen.getByRole('link', { name: 'Security' })).toHaveAttribute('aria-current', 'page')
    expect(screen.getByRole('link', { name: 'Settings' })).not.toHaveAttribute('aria-current')
  })

  it('marks nothing when no item is current', () => {
    inRouter(<Rail active={null} path="/nowhere" admin={false} />)
    expect(screen.getAllByRole('link').some((link) => link.hasAttribute('aria-current'))).toBe(false)
  })

  it('shows nothing at the end of an item until it is told something', () => {
    const { container } = inRouter(<Rail active="overview" path="/" admin={false} />)
    expect(container.querySelector('.ov-navlist__end')).toBeNull()
    expect(container.querySelector('.ov-navlist__dim')).toBeNull()
  })

  it('shows the status of an item: text, a dot, a count, and greys a stream with no data', () => {
    const { container } = inRouter(
      <Rail
        active="overview"
        path="/"
        admin={false}
        status={{
          nerve: { badge: { kind: 'text', text: '3 of 4' } },
          sleep: { badge: { kind: 'text', text: 'n/a' }, dim: true },
          activity: { badge: { kind: 'dot', tone: 'attn', label: 'History is still importing' } },
          findings: { badge: { kind: 'count', value: 2, label: '2 live findings' } },
          connections: { badge: { kind: 'dot', tone: 'ok', label: 'Connected' } },
        }}
      />,
    )
    expect(screen.getByText('3 of 4')).toHaveClass('ov-navlist__end', 'ov-muted')
    expect(screen.getByText('n/a')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: /Sleep/ })).toHaveClass('ov-navlist__dim')
    expect(screen.getByRole('img', { name: 'History is still importing' })).toHaveClass('ov-dot', 'ov-dot--attn')
    expect(screen.getByRole('img', { name: 'Connected' })).toHaveClass('ov-dot', 'ov-dot--ok')
    expect(screen.getByRole('img', { name: '2 live findings' })).toHaveClass('ov-count', 'ov-count--attn')
    expect(screen.getByRole('link', { name: /Findings/ }).textContent).toBe('Findings2')
    expect(container.querySelectorAll('.ov-navlist__end')).toHaveLength(5)
    expect(container.querySelector('[style]')).toBeNull()
  })

  it('has icons that are not read out', () => {
    const { container } = inRouter(<Rail active="overview" path="/" admin />)
    const icons = container.querySelectorAll('.ov-navlist svg')
    expect(icons).toHaveLength(11)
    for (const icon of icons) expect(icon).toHaveAttribute('aria-hidden', 'true')
  })
})

describe('PhoneTabs', () => {
  it('has the four tabs of the phone, in order', () => {
    inRouter(<PhoneTabs active="overview" path="/" />)
    const nav = screen.getByRole('navigation', { name: 'Main tabs' })
    expect(within(nav).getAllByRole('link').map((link) => [link.textContent, link.getAttribute('href')])).toEqual([
      ['Overview', '/'],
      ['Findings', '/findings'],
      ['Data', '/connections'],
      ['Settings', '/settings'],
    ])
  })

  it('marks the current tab', () => {
    const { rerender } = inRouter(<PhoneTabs active="connections" path="/connections" />)
    expect(screen.getByRole('link', { name: 'Data' })).toHaveAttribute('aria-current', 'page')
    expect(screen.getAllByRole('link').filter((link) => link.hasAttribute('aria-current'))).toHaveLength(1)
    rerender(
      <RouterProvider>
        <PhoneTabs active="overview" path="/body" />
      </RouterProvider>,
    )
    expect(screen.getByRole('link', { name: 'Overview' })).toHaveAttribute('aria-current', 'true')
  })

  it('marks none for a page that has no tab', () => {
    inRouter(<PhoneTabs active={null} path="/admin" />)
    expect(screen.getAllByRole('link').some((link) => link.hasAttribute('aria-current'))).toBe(false)
  })
})
