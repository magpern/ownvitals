// @vitest-environment node
import { spawnSync } from 'node:child_process'
import { mkdtempSync, readFileSync, readdirSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { afterAll, beforeAll, describe, expect, it } from 'vitest'
import { ROOT } from '../test/sourceScan'
import { imageFiles } from './images'
import { allPublicPaths } from './routes'
import { builtPages, builtPaths, legalApproved } from './site'

// The public build (scripts/build-public.mjs, ADR 0016) run into a scratch folder with synthetic
// settings: what it emits, that the files hold the privacy rules, and that the legal gate fails
// closed. Nothing here touches the network.

const ORIGIN = 'https://ownvitals.example'
const REPO = join(ROOT, '..')
const outs: string[] = []

function listing(dir: string, prefix = ''): string[] {
  return readdirSync(dir, { withFileTypes: true }).flatMap((entry) =>
    entry.isDirectory() ? listing(join(dir, entry.name), `${prefix}${entry.name}/`) : [`${prefix}${entry.name}`],
  )
}

function run(env: Record<string, string>) {
  const out = mkdtempSync(join(tmpdir(), 'ownvitals-public-'))
  outs.push(out)
  const base: NodeJS.ProcessEnv = { ...process.env, ...env }
  delete base.OWNVITALS_PUBLIC_LEGAL_APPROVED
  const result = spawnSync(process.execPath, [join(ROOT, 'scripts', 'build-public.mjs'), out], {
    cwd: ROOT,
    env: { ...base, ...env },
    encoding: 'utf8',
  })
  return { out, result }
}

let built = ''
beforeAll(() => {
  const { out, result } = run({ OWNVITALS_PUBLIC_ORIGIN: ORIGIN })
  if (result.status !== 0) throw new Error(`build-public failed:\n${result.stdout}\n${result.stderr}`)
  built = out
}, 120_000)

afterAll(() => {
  for (const out of outs) rmSync(out, { recursive: true, force: true })
})

const read = (path: string) => readFileSync(join(built, path), 'utf8')
const htmlFiles = () => listing(built).filter((path) => path.endsWith('.html'))

describe('what the build emits', () => {
  it('is exactly the pages, the sitemap, robots.txt, public.css and the six images', () => {
    expect(listing(built).sort()).toEqual(builtPaths(ORIGIN, false).sort())
    expect(htmlFiles().sort()).toEqual(['features/index.html', 'index.html', 'sv/features/index.html', 'sv/index.html'])
  })

  it('names the same files as the server\'s PUBLIC_FILES, one by one', () => {
    const source = readFileSync(join(REPO, 'backend', 'src', 'ownvitals', 'public_site.py'), 'utf8')
    const tuple = /PUBLIC_FILES: Final = \(([^)]*)\)/s.exec(source)?.[1] ?? ''
    const named = [...tuple.matchAll(/"([^"]+)"/g)].map((match) => match[1])
    expect(named.sort()).toEqual(builtPaths(ORIGIN, false).filter((path) => !path.endsWith('index.html')).sort())
  })

  it('copies the images byte for byte from docs/design', () => {
    for (const name of imageFiles()) {
      const source = readFileSync(join(REPO, 'docs', 'design', 'mockups', 'png', name))
      expect(readFileSync(join(built, 'img', name)).equals(source), name).toBe(true)
    }
  })

  it('puts the stylesheet after the tokens and the fonts, and links no font host', () => {
    const css = read('public.css')
    const tokens = css.indexOf('--ov-bg:')
    const fonts = css.indexOf('@font-face')
    const page = css.indexOf('.pub{')
    expect(tokens).toBeGreaterThanOrEqual(0)
    expect(fonts).toBeGreaterThan(tokens)
    expect(page).toBeGreaterThan(fonts)
    expect(css).not.toMatch(/https?:\/\/|\/\/[a-z]|data:/i)
    for (const url of css.matchAll(/url\(([^)]+)\)/g)) expect(url[1]).toMatch(/^\/assets\/ibm-plex-[\w-]+\.woff2?$/)
  })

  it('has robots.txt that disallows only /api/, and a sitemap of exactly the built pages', () => {
    const robots = read('robots.txt')
    expect(robots.split('\n').filter((line) => /^disallow/i.test(line))).toEqual(['Disallow: /api/'])
    expect(robots).toContain(`Sitemap: ${ORIGIN}/sitemap.xml`)
    const locs = [...read('sitemap.xml').matchAll(/<loc>([^<]+)<\/loc>/g)].map((match) => match[1])
    expect(locs?.sort()).toEqual(allPublicPaths().map((path) => `${ORIGIN}${path === '/' ? '/' : path}`).sort())
  })
})

describe('the built HTML', () => {
  it('has no script but the JSON-LD data block, no cookie or analytics, and no URL but its own origin', () => {
    for (const path of htmlFiles()) {
      const html = read(path)
      const scripts = [...html.matchAll(/<script\b[^>]*>/g)].map((match) => match[0])
      expect(scripts, path).toEqual(['<script type="application/ld+json">'])
      expect(html, path).not.toMatch(/document\.cookie|gtag|googletagmanager|fbq\(|<iframe|<form|<style|\sstyle=|\son[a-z]+=/i)
      for (const url of html.matchAll(/(?:href|src|srcSet|content)="(https?:\/\/[^"]+)"/g)) {
        expect(url[1]?.startsWith(ORIGIN), `${path} ${url[1]}`).toBe(true)
      }
    }
  })

  it('shows only the six listed images', () => {
    const shown = new Set<string>()
    for (const path of htmlFiles()) {
      for (const match of read(path).matchAll(/(?:src|srcSet)="(\/img\/[^"]+)"/g)) shown.add(match[1] ?? '')
    }
    expect([...shown].sort()).toEqual(imageFiles().map((name) => `/img/${name}`).sort())
  })
})

describe('the build settings', () => {
  it('refuses a missing, non-https or non-bare origin', () => {
    for (const origin of ['', 'http://ownvitals.example', 'https://ownvitals.example/path', 'not a url']) {
      const { out, result } = run({ OWNVITALS_PUBLIC_ORIGIN: origin })
      rmSync(out, { recursive: true, force: true })
      expect(result.status, origin).not.toBe(0)
    }
  }, 60_000)
})

describe('the legal gate fails closed', () => {
  it('is open only for the exact value true', () => {
    expect(legalApproved('true')).toBe(true)
    for (const value of [undefined, '', 'TRUE', 'True', '1', 'yes', 'false', ' true', 'true ']) {
      expect(legalApproved(value), String(value)).toBe(false)
    }
    // Today no legal page exists yet, so either way the set is the two pages; the gate is the
    // only way the legal issue's pages can join it.
    expect(builtPages(false)).toEqual(['home', 'features'])
  })

  it('leaves the output unchanged for any other value of the flag', () => {
    const { out, result } = run({ OWNVITALS_PUBLIC_ORIGIN: ORIGIN, OWNVITALS_PUBLIC_LEGAL_APPROVED: 'yes' })
    expect(result.status).toBe(0)
    expect(listing(out).sort()).toEqual(builtPaths(ORIGIN, false).sort())
  }, 120_000)

  it('is set in no committed file (a workflow may pass the owner\'s repository variable)', () => {
    const tracked = spawnSync('git', ['ls-files', '-z'], { cwd: REPO, encoding: 'utf8' })
    expect(tracked.status).toBe(0)
    const files = tracked.stdout.split('\0').filter((path) => /^(Dockerfile|compose.*\.ya?ml|\.env.*|\.github\/.*|scripts\/.*|docker\/.*|frontend\/package\.json|frontend\/scripts\/.*)$/.test(path))
    expect(files).toContain('Dockerfile')
    const setting = /OWNVITALS_PUBLIC_LEGAL_APPROVED\s*(?:=|:\s)\s*(?!\$)\S/
    for (const path of files) {
      const text = readFileSync(join(REPO, path), 'utf8')
      expect(setting.test(text), path).toBe(false)
      expect(/^\s*(?:ENV|ARG)\s+OWNVITALS_PUBLIC_LEGAL_APPROVED[ \t]+\S/m.test(text), path).toBe(false) // the space form
    }
  })
})
