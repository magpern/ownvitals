// What src/licences.test.ts checks: the licences of the packages that ship in the bundle, read
// from package-lock.json (lockfile version 2 or 3, which list every installed package).

export interface LockPackage {
  version?: string
  license?: unknown
  /** npm: only reachable through `devDependencies`. */
  dev?: boolean
  dependencies?: Record<string, string>
  devDependencies?: Record<string, string>
  optionalDependencies?: Record<string, string>
  peerDependencies?: Record<string, string>
}

export interface Lockfile {
  lockfileVersion: number
  /** Keyed by install path (`node_modules/a/node_modules/b`); the key `""` is the project itself. */
  packages: Record<string, LockPackage>
}

/** The licences the project accepts (SPDX identifiers, compared without regard to case). */
export const ACCEPTED_LICENCES = new Set(['MIT', 'ISC', 'Apache-2.0', 'OFL-1.1'].map((id) => id.toLowerCase()))

/**
 * Whether an SPDX licence expression is acceptable: `A OR B` when either side is, `A AND B` when
 * both are (`AND` binds tighter), parentheses for grouping. A licence with an exception
 * (`WITH`), a `+` suffix, an unknown identifier, `UNLICENSED`, `SEE LICENSE IN ...`, something
 * that is not a string or an expression that does not parse is not acceptable.
 */
export function licenceAccepted(expression: unknown): boolean {
  if (typeof expression !== 'string') return false
  const tokens = expression.match(/[()]|[^\s()]+/g) ?? []
  let at = 0
  const orExpression = (): boolean => {
    let ok = andExpression()
    while (tokens[at] === 'OR') {
      at += 1
      const next = andExpression()
      ok = ok || next
    }
    return ok
  }
  const andExpression = (): boolean => {
    let ok = atom()
    while (tokens[at] === 'AND') {
      at += 1
      const next = atom()
      ok = ok && next
    }
    return ok
  }
  const atom = (): boolean => {
    const token = tokens[at]
    at += 1
    if (token === '(') {
      const ok = orExpression()
      if (tokens[at] !== ')') throw new SyntaxError('missing )')
      at += 1
      return ok
    }
    if (token === undefined || token === ')' || token === 'AND' || token === 'OR' || token === 'WITH') {
      throw new SyntaxError(`unexpected ${token ?? 'end of the expression'}`)
    }
    if (tokens[at] === 'WITH') {
      at += 2 // `MIT WITH Some-exception` is a different licence from MIT
      return false
    }
    return ACCEPTED_LICENCES.has(token.toLowerCase())
  }
  try {
    const ok = orExpression()
    return ok && at === tokens.length
  } catch {
    return false
  }
}

/** The package name in an install path: `node_modules/a/node_modules/@s/b` is `@s/b`. */
export function nameOf(key: string): string {
  return key.slice(key.lastIndexOf('node_modules/') + 'node_modules/'.length)
}

/** Where npm finds `name` for the package at `from`: next to it, then in each folder above. */
function resolve(lock: Lockfile, from: string, name: string): string | undefined {
  let base = from
  for (;;) {
    const key = base === '' ? `node_modules/${name}` : `${base}/node_modules/${name}`
    if (key in lock.packages) return key
    if (base === '') return undefined
    const outer = base.lastIndexOf('/node_modules/')
    base = outer === -1 ? '' : base.slice(0, outer)
  }
}

/**
 * The install paths reachable from the project's `dependencies` (and optional dependencies)
 * through each package's dependencies, optional dependencies and peer dependencies: the
 * production tree, worked out here rather than taken from npm's `dev` flags.
 */
export function productionClosure(lock: Lockfile): string[] {
  const seen = new Set<string>()
  const visit = (from: string, names: string[]) => {
    for (const name of names) {
      const key = resolve(lock, from, name)
      if (key === undefined || seen.has(key)) continue
      seen.add(key)
      const pkg = lock.packages[key]
      visit(key, Object.keys({ ...pkg?.dependencies, ...pkg?.optionalDependencies, ...pkg?.peerDependencies }))
    }
  }
  const root = lock.packages['']
  visit('', Object.keys({ ...root?.dependencies, ...root?.optionalDependencies }))
  return [...seen].sort()
}

/** The install paths npm does not flag `dev`: it would install them with `npm ci --omit=dev`. */
export function notFlaggedDev(lock: Lockfile): string[] {
  return Object.keys(lock.packages)
    .filter((key) => key !== '' && lock.packages[key]?.dev !== true)
    .sort()
}

/**
 * Every package that ships: the production tree as worked out here, plus whatever npm does not
 * flag `dev`. Packages only the build and the tests use are left out (see licences.test.ts).
 */
export function shippedPackages(lock: Lockfile): string[] {
  return [...new Set([...productionClosure(lock), ...notFlaggedDev(lock)])].sort()
}

function show(licence: unknown): string {
  return JSON.stringify(licence) ?? 'none'
}

/** The shipped packages without an accepted licence, as `name@version: licence`. */
export function unacceptedLicences(lock: Lockfile): string[] {
  return shippedPackages(lock).flatMap((key) => {
    const pkg = lock.packages[key]
    return licenceAccepted(pkg?.license) ? [] : [`${nameOf(key)}@${pkg?.version ?? '?'}: ${show(pkg?.license)}`]
  })
}
