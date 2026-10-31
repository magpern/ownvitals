// @vitest-environment node
import { spawnSync } from 'node:child_process'
import { mkdtempSync, readFileSync, readdirSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { afterAll, beforeAll, describe, expect, it } from 'vitest'
import { ROOT, cspViolations, dataUrls, remoteUrls } from './test/sourceScan'

// The production build, made the way `npm run build` makes it (into a scratch folder), against
// the content security policy the server sends (`default-src 'self'`, no inline script or style,
// no data: URL). Bundled JavaScript is checked for data: URLs only: React's own code names
// `innerHTML` and `javascript:` as strings, and its error decoder names react.dev, none of which
// is a request or a script this page loads.

let out = ''
let built = new Map<string, string>()

function listing(dir: string, prefix = ''): string[] {
  return readdirSync(dir, { withFileTypes: true }).flatMap((entry) =>
    entry.isDirectory()
      ? listing(join(dir, entry.name), `${prefix}${entry.name}/`)
      : [`${prefix}${entry.name}`],
  )
}

beforeAll(() => {
  out = mkdtempSync(join(tmpdir(), 'ownvitals-dist-'))
  const result = spawnSync(
    process.execPath,
    [join(ROOT, 'node_modules', 'vite', 'bin', 'vite.js'), 'build', '--outDir', out, '--emptyOutDir', '--logLevel', 'error'],
    { cwd: ROOT, env: { ...process.env, NODE_ENV: 'production' }, encoding: 'utf8' },
  )
  if (result.status !== 0) throw new Error(`vite build failed:\n${result.stdout}\n${result.stderr}`)
  built = new Map(listing(out).map((path) => [path, readFileSync(join(out, path), 'utf8')]))
}, 120_000)

afterAll(() => {
  if (out !== '') rmSync(out, { recursive: true, force: true })
})

describe('the production build under the content security policy', () => {
  it('is an index.html plus scripts and styles under assets/, and nothing else', () => {
    const paths = [...built.keys()]
    expect(paths).toContain('index.html')
    const rest = paths.filter((path) => path !== 'index.html')
    expect(rest.some((path) => path.endsWith('.js'))).toBe(true)
    expect(rest.some((path) => path.endsWith('.css'))).toBe(true)
    expect(rest.every((path) => /^assets\/[^/]+\.(?:js|css)$/.test(path))).toBe(true)
  })

  it('has an index.html with no inline script or style and nothing from another origin', () => {
    const html = built.get('index.html') ?? ''
    expect(cspViolations('dist/index.html', html)).toEqual([])
    expect(remoteUrls(html)).toEqual([])
    const scripts = html.match(/<script\b[^>]*>/g) ?? []
    expect(scripts).toHaveLength(1)
    expect(scripts[0]).toMatch(/type="module"[^>]*src="\/assets\/[^"]+\.js"/)
  })

  it('has no data: URL in any file: no asset is inlined into the stylesheet or the bundle', () => {
    for (const [path, text] of built) expect(dataUrls(text), path).toEqual([])
  })

  it('has a stylesheet that loads nothing from another host', () => {
    for (const [path, text] of built) {
      if (path.endsWith('.css')) expect(remoteUrls(text), path).toEqual([])
    }
  })
})
