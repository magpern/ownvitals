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
let fonts: string[] = []

// The IBM Plex font files (OFL-1.1, from @fontsource) are the one binary kind in the build: self-
// hosted under assets/ as .woff2 and its .woff fallback. They are listed and checked by name, not
// read as text.
const FONT_FILE = /\.woff2?$/

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
  const paths = listing(out)
  fonts = paths.filter((path) => FONT_FILE.test(path))
  built = new Map(paths.filter((path) => !FONT_FILE.test(path)).map((path) => [path, readFileSync(join(out, path), 'utf8')]))
}, 120_000)

afterAll(() => {
  if (out !== '') rmSync(out, { recursive: true, force: true })
})

describe('the production build under the content security policy', () => {
  it('is an index.html plus scripts, styles and font files under assets/, and nothing else', () => {
    const paths = [...built.keys()]
    expect(paths).toContain('index.html')
    const rest = [...paths.filter((path) => path !== 'index.html'), ...fonts]
    expect(rest.some((path) => path.endsWith('.js'))).toBe(true)
    expect(rest.some((path) => path.endsWith('.css'))).toBe(true)
    // Fonts are the only addition, and only these two kinds: any other file still fails here.
    expect(rest.every((path) => /^assets\/[^/]+\.(?:js|css|woff2?)$/.test(path))).toBe(true)
  })

  it('carries the Plex font files, from its own origin', () => {
    // The faces of src/styles/fonts.css: Sans 400, 500, 600 and italic 400, 500; Mono 400.
    const faces = [
      'ibm-plex-sans-latin-400-normal',
      'ibm-plex-sans-latin-500-normal',
      'ibm-plex-sans-latin-600-normal',
      'ibm-plex-sans-latin-400-italic',
      'ibm-plex-sans-latin-500-italic',
      'ibm-plex-mono-latin-400-normal',
    ]
    for (const face of faces) {
      for (const extension of ['woff2', 'woff']) {
        expect(fonts.filter((path) => new RegExp(`^assets/${face}-[\\w-]+\\.${extension}$`).test(path)), `${face}.${extension}`).toHaveLength(1)
      }
    }
    expect(fonts).toHaveLength(faces.length * 2)

    // Every @font-face of the stylesheet names a file of the build by a path on this origin.
    const css = [...built].filter(([path]) => path.endsWith('.css')).map(([, text]) => text).join('\n')
    const urls = [...css.matchAll(/@font-face\{[^}]*\}/g)].flatMap((rule) =>
      [...rule[0].matchAll(/url\(([^)]+)\)/g)].map((url) => url[1] ?? ''),
    )
    expect(urls).toHaveLength(faces.length * 2)
    for (const url of urls) {
      expect(url, url).toMatch(/^\/assets\/[^/]+\.woff2?$/)
      expect(fonts, url).toContain(url.slice(1))
    }
    expect(css).toMatch(/font-family:\s*["']?IBM Plex Sans/)
    expect(css).toMatch(/font-family:\s*["']?IBM Plex Mono/)
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
