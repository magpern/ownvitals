import { describe, expect, it } from 'vitest'
import { APP_ROUTES, matchAppRoute, type RouteId } from './appRoutes'

// The screens of the signed-in app, by address: the information architecture of
// docs/design/README.md.

const IDS = (): RouteId[] => APP_ROUTES.map((route) => route.id)

describe('the routes of the app', () => {
  it('are the information architecture of the design, one entry each', () => {
    expect(APP_ROUTES.map((route) => route.pattern)).toEqual([
      '/',
      '/body',
      '/heart',
      '/nerve',
      '/sleep',
      '/activity',
      '/m/:metric',
      '/m/:metric/:day',
      '/heart/ecg',
      '/heart/ecg/:id',
      '/findings',
      '/connections',
      '/settings',
      '/settings/security',
      '/admin',
    ])
    expect(new Set(IDS()).size).toBe(APP_ROUTES.length)
    expect(new Set(APP_ROUTES.map((route) => route.pattern)).size).toBe(APP_ROUTES.length)
  })

  it('give every screen a page header, a "Coming later" line and a navigation item', () => {
    for (const route of APP_ROUTES) {
      expect(route.title, route.id).not.toBe('')
      expect(route.later, route.id).toMatch(/\.$/)
      expect(route.nav, route.id).toBeTruthy()
    }
  })

  it('keep the sentence the first placeholder had, for the dashboard', () => {
    expect(APP_ROUTES.find((route) => route.id === 'overview')?.later).toBe('The dashboard comes in M6.')
  })

  it('mark only the admin area as admin-only', () => {
    expect(APP_ROUTES.filter((route) => route.adminOnly === true).map((route) => route.id)).toEqual(['admin'])
  })
})

describe('matchAppRoute', () => {
  it.each([
    ['/', 'overview'],
    ['/body', 'body'],
    ['/heart', 'heart'],
    ['/nerve', 'nerve'],
    ['/sleep', 'sleep'],
    ['/activity', 'activity'],
    ['/m/weight', 'metric'],
    ['/m/weight/2026-10-14', 'day'],
    ['/heart/ecg', 'ecg-list'],
    ['/heart/ecg/ecg-1', 'ecg'],
    ['/findings', 'findings'],
    ['/connections', 'connections'],
    ['/settings', 'settings'],
    ['/settings/security', 'security'],
    ['/admin', 'admin'],
  ])('%s is the %s screen', (path, id) => {
    expect(matchAppRoute(path)?.route.id).toBe(id)
  })

  it('reads the parameters, decoded', () => {
    expect(matchAppRoute('/m/weight')?.params).toEqual({ metric: 'weight' })
    expect(matchAppRoute('/m/weight/2026-10-14')?.params).toEqual({ metric: 'weight', day: '2026-10-14' })
    expect(matchAppRoute('/heart/ecg/ecg-1')?.params).toEqual({ id: 'ecg-1' })
    expect(matchAppRoute('/m/resting%20heart%20rate')?.params).toEqual({ metric: 'resting heart rate' })
    expect(matchAppRoute('/body')?.params).toEqual({})
  })

  it('does not mind a trailing slash', () => {
    expect(matchAppRoute('/body/')?.route.id).toBe('body')
    expect(matchAppRoute('/m/weight/')?.route.id).toBe('metric')
  })

  it.each([
    '/signin',
    '/signup',
    '/reset',
    '/two-factor',
    '/two-factor/setup',
    '/signup/confirm',
    '/nowhere',
    '/body/extra',
    '/m',
    '/m/weight/2026-10-14/extra',
    '/heart/ecg/a/b',
    '/settings/profile',
    '/Admin',
    '/m/%E0%A4%A',
  ])('%s is no screen of the app', (path) => {
    expect(matchAppRoute(path)).toBeNull()
  })

  it('puts the second-factor and sign-in pages outside the app', () => {
    for (const path of ['/signin', '/signin/email', '/signup', '/signup/confirm', '/reset', '/two-factor', '/two-factor/setup']) {
      expect(matchAppRoute(path), path).toBeNull()
    }
  })
})
