// @vitest-environment node
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { describe, expect, it } from 'vitest'
import { ACCEPTED_LICENCE_IDS } from './test/licenceScan'
import { ROOT } from './test/sourceScan'

// The dependency rules of the frontend: exact versions, a committed lockfile that agrees with
// package.json, and licences the project accepts for what it depends on directly.

interface PackageJson {
  engines?: { node?: string }
  dependencies?: Record<string, string>
  devDependencies?: Record<string, string>
}
interface Lockfile {
  lockfileVersion: number
  packages: Record<string, { version?: string; license?: string; dependencies?: Record<string, string>; devDependencies?: Record<string, string> }>
}

const pkg = JSON.parse(readFileSync(join(ROOT, 'package.json'), 'utf8')) as PackageJson
const lock = JSON.parse(readFileSync(join(ROOT, 'package-lock.json'), 'utf8')) as Lockfile
const direct = { ...pkg.dependencies, ...pkg.devDependencies }

// The same list as licences.test.ts, spelled exactly (a direct dependency's own `license` field).
const ACCEPTED_LICENCES = new Set(ACCEPTED_LICENCE_IDS)

describe('dependencies', () => {
  it('has some, in both lists', () => {
    expect(Object.keys(pkg.dependencies ?? {})).toContain('react')
    expect(Object.keys(pkg.devDependencies ?? {})).toContain('vite')
  })

  it.each(Object.entries(direct))('%s is pinned to the exact version %s', (_name, version) => {
    expect(version).toMatch(/^\d+\.\d+\.\d+$/)
  })

  it.each(Object.entries(direct))('%s is locked at the version package.json names (%s)', (name, version) => {
    expect(lock.packages[`node_modules/${name}`]?.version).toBe(version)
  })

  it('has a lockfile whose root lists the same dependencies as package.json', () => {
    const root = lock.packages['']
    expect(root?.dependencies).toEqual(pkg.dependencies)
    expect(root?.devDependencies).toEqual(pkg.devDependencies)
  })

  it.each(Object.keys(direct))('%s has an accepted licence', (name) => {
    expect(ACCEPTED_LICENCES.has(lock.packages[`node_modules/${name}`]?.license ?? '')).toBe(true)
  })

  it('needs Node 24 or newer', () => {
    expect(pkg.engines?.node).toBe('>=24')
  })
})
