import { render } from '@testing-library/react'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { describe, expect, it } from 'vitest'
import type { SessionState } from '../../api/auth'
import { ROOT } from '../../test/sourceScan'
import { Icon } from '../ui/Icon'
import { PHONE_TABS, isAdmin, phoneTabOf, railGroups } from './nav'

// The items of the navigation, held to the mock-ups they come from: docs/design/mockups/src/
// Rail.dc.html (the rail) and Onboarding.dc.html (the tabs of the phone frame).

const MOCKUPS = join(ROOT, '..', 'docs/design/mockups/src')
const railSource = readFileSync(join(MOCKUPS, 'Rail.dc.html'), 'utf8')
const onboardingSource = readFileSync(join(MOCKUPS, 'Onboarding.dc.html'), 'utf8')

/** The path an icon draws, read back from what it renders. */
function pathOf(icon: Parameters<typeof Icon>[0]['name']): string {
  return render(<Icon name={icon} />).container.querySelector('path')?.getAttribute('d') ?? ''
}

// `it('overview', 'Overview', 'Dashboard.dc.html', I.overview ...` in the mock-up, in order.
const mockItems = [...railSource.matchAll(/\bit\('(\w+)', '([^']+)', '[^']+', I\.(\w+)/g)].map((m) => ({
  id: m[1] ?? '',
  label: m[2] ?? '',
  icon: m[3] ?? '',
}))
const mockIcons = Object.fromEntries(
  [...railSource.slice(railSource.indexOf('var I = {')).matchAll(/^\s+(\w+): '([^']+)'/gm)].map((m) => [m[1] ?? '', m[2] ?? '']),
)

describe('the rail', () => {
  const groups = railGroups(false)

  it('has the groups of the mock-up, in its order', () => {
    const mockGroups = [...railSource.matchAll(/<span class="eyebrow">([^<]+)<\/span>/g)].map((m) => m[1])
    expect(groups.map((group) => group.label)).toEqual(mockGroups)
    expect(mockGroups).toEqual(['Analyse', 'Your data', 'Account'])
  })

  it('has the items of the mock-up, with its labels, in its order', () => {
    expect(mockItems).toHaveLength(10)
    const items = groups.flatMap((group) => group.items)
    expect(items.map((item) => [item.id, item.label])).toEqual(mockItems.map((item) => [item.id, item.label]))
  })

  it('has the icons of the mock-up', () => {
    for (const item of groups.flatMap((group) => group.items)) {
      const mock = mockItems.find((candidate) => candidate.id === item.id)
      expect(pathOf(item.icon), item.id).toBe(mockIcons[mock?.icon ?? ''])
    }
  })

  it('puts Admin last in Account, for an admin only, with the mock-up\'s icon', () => {
    expect(groups.flatMap((group) => group.items).some((item) => item.id === 'admin')).toBe(false)
    const account = railGroups(true).find((group) => group.label === 'Account')
    expect(account?.items.map((item) => item.label)).toEqual(['Settings', 'Security', 'Admin'])
    const adminIcon = /<path d="(M12 3l7 3v5[^"]*)"/.exec(railSource)?.[1]
    expect(pathOf('admin')).toBe(adminIcon)
  })

  it('leads each item to its page', () => {
    expect(railGroups(true).flatMap((group) => group.items.map((item) => [item.id, item.to]))).toEqual([
      ['overview', '/'],
      ['body', '/body'],
      ['heart', '/heart'],
      ['nerve', '/nerve'],
      ['sleep', '/sleep'],
      ['activity', '/activity'],
      ['findings', '/findings'],
      ['connections', '/connections'],
      ['settings', '/settings'],
      ['security', '/settings/security'],
      ['admin', '/admin'],
    ])
  })
})

describe('the phone tabs', () => {
  const mockTabs = [
    ...onboardingSource.slice(onboardingSource.indexOf('<nav class="phone-tabs"')).split('</nav>')[0]!.matchAll(/<path d="([^"]+)" class="ic"><\/path><\/svg>([^<]+)<\/a>/g),
  ].map((m) => ({ icon: m[1] ?? '', label: m[2] ?? '' }))

  it('are the four of the Onboarding board: Overview, Findings, Data and Settings', () => {
    expect(mockTabs.map((tab) => tab.label)).toEqual(['Overview', 'Findings', 'Data', 'Settings'])
    expect(PHONE_TABS.map((tab) => tab.label)).toEqual(mockTabs.map((tab) => tab.label))
    expect(PHONE_TABS.map((tab) => pathOf(tab.icon))).toEqual(mockTabs.map((tab) => tab.icon))
  })

  it('lead to the overview, the findings, the connections ("Data") and the settings', () => {
    expect(PHONE_TABS.map((tab) => tab.to)).toEqual(['/', '/findings', '/connections', '/settings'])
  })

  it('belong to the tab the page is reached from, and Admin has none', () => {
    const tabs = new Set(PHONE_TABS.map((tab) => tab.id))
    expect(phoneTabOf('overview')).toBe('overview')
    for (const area of ['body', 'heart', 'nerve', 'sleep', 'activity'] as const) expect(phoneTabOf(area)).toBe('overview')
    expect(phoneTabOf('findings')).toBe('findings')
    expect(phoneTabOf('connections')).toBe('connections')
    expect(phoneTabOf('settings')).toBe('settings')
    expect(phoneTabOf('security')).toBe('settings')
    expect(phoneTabOf('admin')).toBeNull()
    for (const nav of ['overview', 'body', 'heart', 'nerve', 'sleep', 'activity', 'findings', 'connections', 'settings', 'security'] as const) {
      expect(tabs.has(phoneTabOf(nav) ?? 'admin'), nav).toBe(true)
    }
  })
})

describe('isAdmin', () => {
  const session = (role: string): SessionState => ({
    authenticated: true,
    user: { id: 'u', email: 'a@example.test', role },
    session: { expiresAt: '', recentAuth: true, mfa: true, stepup: false },
    totpEnabled: true,
  })

  it('is true for the admin role only', () => {
    expect(isAdmin(session('admin'))).toBe(true)
    expect(isAdmin(session('user'))).toBe(false)
    expect(isAdmin(session('Admin'))).toBe(false)
    expect(isAdmin(session(''))).toBe(false)
  })

  it('is false for nobody and for a signed-out visitor', () => {
    expect(isAdmin(null)).toBe(false)
    expect(isAdmin({ authenticated: false, user: null, session: null, totpEnabled: false })).toBe(false)
  })
})
