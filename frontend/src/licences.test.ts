// @vitest-environment node
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { describe, expect, it } from 'vitest'
import {
  type LockPackage,
  type Lockfile,
  licenceAccepted,
  nameOf,
  notFlaggedDev,
  productionClosure,
  shippedPackages,
  unacceptedLicences,
} from './test/licenceScan'
import { ROOT } from './test/sourceScan'

// The licence rule for what ships (PLAN §2): every package in the production dependency tree of
// package-lock.json, direct or not, has an accepted licence (MIT, ISC, Apache-2.0 or OFL-1.1, or
// an SPDX expression that gives a choice of them). Packages only the build and the tests use
// (the lockfile's `dev` packages: Vite, ESLint, Vitest, the CSS tooling ...) are not distributed
// and are exempt, as the decisions log records: some of them are MPL-2.0, BSD-3-Clause, CC-BY-4.0
// or BlueOak-1.0.0. src/packageJson.test.ts checks the licences of the direct dependencies of
// both kinds.

const lock = JSON.parse(readFileSync(join(ROOT, 'package-lock.json'), 'utf8')) as Lockfile
const pkg = JSON.parse(readFileSync(join(ROOT, 'package.json'), 'utf8')) as {
  dependencies?: Record<string, string>
}

describe('the production dependency tree', () => {
  it('is read from a lockfile that lists every package', () => {
    expect([2, 3]).toContain(lock.lockfileVersion)
    expect(Object.keys(lock.packages).length).toBeGreaterThan(10)
  })

  it('holds the direct dependencies and what they need, and not the build tooling', () => {
    const names = shippedPackages(lock).map(nameOf)
    for (const name of Object.keys(pkg.dependencies ?? {})) expect(names).toContain(name)
    expect(names).toEqual(expect.arrayContaining(['react', 'react-dom', 'scheduler']))
    for (const tooling of ['vite', 'vitest', 'eslint', 'typescript', 'jsdom', 'lightningcss']) {
      expect(names).not.toContain(tooling)
    }
  })

  it('is the same set as the packages npm does not flag as dev-only', () => {
    // The `dev` flags are what exempts a package. They must agree with what package.json
    // really depends on, or a flag could hide a package that ships.
    expect(productionClosure(lock)).toEqual(notFlaggedDev(lock))
  })

  it.each(shippedPackages(lock))('%s has an accepted licence', (key) => {
    const entry = lock.packages[key]
    expect(licenceAccepted(entry?.license), `${nameOf(key)}@${entry?.version ?? '?'}: ${JSON.stringify(entry?.license)}`).toBe(true)
  })

  it('has no package without an accepted licence', () => {
    expect(unacceptedLicences(lock)).toEqual([])
  })
})

describe('the licence rule', () => {
  /** A made-up lockfile: the project needs `a`, which needs `b`; `tool` is only for the build. */
  function lockWith(overrides: Record<string, LockPackage | null> = {}): Lockfile {
    const lock: Lockfile = {
      lockfileVersion: 3,
      packages: {
        '': { dependencies: { a: '1.0.0' }, devDependencies: { tool: '1.0.0' } },
        'node_modules/a': { version: '1.0.0', license: 'MIT', dependencies: { b: '^1.0.0' } },
        'node_modules/b': { version: '1.2.0', license: 'ISC' },
        'node_modules/tool': { version: '1.0.0', license: 'MPL-2.0', dev: true, dependencies: { c: '^1.0.0' } },
        'node_modules/c': { version: '1.0.0', license: 'CC-BY-4.0', dev: true },
      },
    }
    for (const [key, entry] of Object.entries(overrides)) {
      if (entry === null) delete lock.packages[key]
      else lock.packages[key] = entry
    }
    return lock
  }

  it('passes when every package that ships has an accepted licence, whatever the build tooling has', () => {
    expect(unacceptedLicences(lockWith())).toEqual([])
    expect(shippedPackages(lockWith()).map(nameOf)).toEqual(['a', 'b'])
  })

  it('flags a direct dependency under a licence that is not accepted', () => {
    const planted = lockWith({ 'node_modules/a': { version: '1.0.0', license: 'GPL-3.0', dependencies: { b: '^1.0.0' } } })
    expect(unacceptedLicences(planted)).toEqual(['a@1.0.0: "GPL-3.0"'])
  })

  it('flags a dependency of a dependency, nested or not', () => {
    const planted = lockWith({ 'node_modules/b': { version: '1.2.0', license: 'AGPL-3.0-only' } })
    expect(unacceptedLicences(planted)).toEqual(['b@1.2.0: "AGPL-3.0-only"'])
    const nested = lockWith({
      'node_modules/a/node_modules/b': { version: '2.0.0', license: 'SSPL-1.0' },
    })
    expect(unacceptedLicences(nested)).toEqual(['b@2.0.0: "SSPL-1.0"'])
  })

  it('flags a package with no licence, an empty one, or one that is not a plain expression', () => {
    for (const license of [undefined, '', 'UNLICENSED', 'SEE LICENSE IN LICENSE.md', { type: 'MIT' }, ['MIT']]) {
      const planted = lockWith({ 'node_modules/b': { version: '1.2.0', license } })
      expect(unacceptedLicences(planted), JSON.stringify(license)).toHaveLength(1)
    }
  })

  it('exempts a build tool, but not the same package once it ships', () => {
    // MPL-2.0 under `tool` is fine while only the build needs it ...
    expect(unacceptedLicences(lockWith())).toEqual([])
    // ... and not when the project starts to depend on it.
    const shipped = lockWith({
      '': { dependencies: { a: '1.0.0', tool: '1.0.0' } },
      'node_modules/tool': { version: '1.0.0', license: 'MPL-2.0', dependencies: { c: '^1.0.0' } },
    })
    expect(unacceptedLicences(shipped)).toEqual(['c@1.0.0: "CC-BY-4.0"', 'tool@1.0.0: "MPL-2.0"'])
  })

  it('is not fooled by a dev flag on a package that ships', () => {
    const flagged = lockWith({ 'node_modules/b': { version: '1.2.0', license: 'GPL-3.0', dev: true } })
    expect(notFlaggedDev(flagged).map(nameOf)).toEqual(['a'])
    expect(productionClosure(flagged).map(nameOf)).toEqual(['a', 'b'])
    expect(unacceptedLicences(flagged)).toEqual(['b@1.2.0: "GPL-3.0"'])
  })

  it('follows optional and peer dependencies too', () => {
    const planted = lockWith({
      'node_modules/a': {
        version: '1.0.0',
        license: 'MIT',
        optionalDependencies: { o: '1.0.0' },
        peerDependencies: { p: '1.0.0', missing: '1.0.0' },
      },
      'node_modules/b': null,
      'node_modules/o': { version: '1.0.0', license: 'LGPL-3.0' },
      'node_modules/p': { version: '1.0.0', license: 'Unlicense' },
    })
    expect(unacceptedLicences(planted)).toEqual(['o@1.0.0: "LGPL-3.0"', 'p@1.0.0: "Unlicense"'])
  })

  it.each([
    'MIT',
    'ISC',
    'Apache-2.0',
    'OFL-1.1',
    'mit',
    '(MIT)',
    '((MIT))',
    '(MIT OR Apache-2.0)',
    'MIT OR GPL-3.0',
    '(GPL-3.0 OR MIT)',
    'MIT AND ISC',
    '(MIT AND (ISC OR GPL-3.0))',
    'MIT AND GPL-3.0 OR ISC',
  ])('accepts the licence %s', (expression) => {
    expect(licenceAccepted(expression)).toBe(true)
  })

  it.each([
    'GPL-3.0',
    'GPL-3.0-only',
    'LGPL-2.1',
    'AGPL-3.0-or-later',
    'MPL-2.0',
    'BSD-2-Clause',
    'BSD-3-Clause',
    'MIT-0',
    'CC-BY-4.0',
    'CC0-1.0',
    'BlueOak-1.0.0',
    'Unlicense',
    'UNLICENSED',
    'SEE LICENSE IN LICENSE.md',
    'MIT+',
    'Apache-2.0+',
    'MIT WITH Classpath-exception-2.0',
    'MIT AND GPL-3.0',
    '(MIT AND GPL-3.0)',
    'GPL-3.0 OR MIT AND GPL-3.0',
    'GPL-3.0 OR LGPL-2.1',
    '',
    ' ',
    'MIT OR',
    'OR MIT',
    'AND',
    '(MIT',
    'MIT)',
    '()',
    'MIT ISC',
    'MIT OR ISC)',
  ])('rejects the licence %j', (expression) => {
    expect(licenceAccepted(expression)).toBe(false)
  })

  it.each([undefined, null, 42, true, { type: 'MIT' }, ['MIT']])('rejects %j, which is not an expression', (value) => {
    expect(licenceAccepted(value)).toBe(false)
  })

  it('names a package by its last install path segment', () => {
    expect(nameOf('node_modules/react')).toBe('react')
    expect(nameOf('node_modules/@types/node')).toBe('@types/node')
    expect(nameOf('node_modules/a/node_modules/@s/b')).toBe('@s/b')
  })
})
