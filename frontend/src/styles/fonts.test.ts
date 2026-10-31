// @vitest-environment node
import { existsSync, readFileSync, readdirSync } from 'node:fs'
import { join } from 'node:path'
import { describe, expect, it } from 'vitest'
import { ROOT } from '../test/sourceScan'

// The fonts: IBM Plex Sans and Plex Mono, self-hosted through @fontsource (OFL-1.1), with only the
// weights the mock-ups use. What the built bundle carries is checked by src/build.test.ts, and
// that no source names a font host by src/sources.test.ts; the licence by src/licences.test.ts
// (OFL-1.1 is on the accepted list, which is untouched).

const fontsCss = readFileSync(join(ROOT, 'src/styles/fonts.css'), 'utf8')
const imports = [...fontsCss.matchAll(/@import\s+'([^']+)'\s*;/g)].map((match) => match[1] ?? '')

const MOCKUPS = join(ROOT, '..', 'docs/design/mockups/src')
const mockupSources = readdirSync(MOCKUPS)
  .filter((name) => /\.(?:dc\.html|css)$/.test(name))
  .map((name) => readFileSync(join(MOCKUPS, name), 'utf8'))

describe('fonts.css', () => {
  it('loads Sans 400, 500 and 600, Sans italic 400 and 500, and Mono 400, in the Latin subset', () => {
    expect(imports).toEqual([
      '@fontsource/ibm-plex-sans/latin-400.css',
      '@fontsource/ibm-plex-sans/latin-500.css',
      '@fontsource/ibm-plex-sans/latin-600.css',
      '@fontsource/ibm-plex-sans/latin-400-italic.css',
      '@fontsource/ibm-plex-sans/latin-500-italic.css',
      '@fontsource/ibm-plex-mono/latin-400.css',
    ])
  })

  it.each(imports)('%s exists, and is the face its name says', (specifier) => {
    const file = join(ROOT, 'node_modules', specifier)
    expect(existsSync(file)).toBe(true)
    const css = readFileSync(file, 'utf8')
    const [, family, weight, italic] = /^@fontsource\/ibm-plex-(sans|mono)\/latin-(\d+)(-italic)?\.css$/.exec(specifier) ?? []
    expect(css).toContain(`font-family: 'IBM Plex ${family === 'sans' ? 'Sans' : 'Mono'}'`)
    expect(css).toContain(`font-weight: ${weight ?? ''};`)
    expect(css).toContain(`font-style: ${italic === undefined ? 'normal' : 'italic'};`)
  })

  it('is a stylesheet of imports only: nothing that names a host, a colour or a size', () => {
    const rest = fontsCss.replace(/\/\*[\s\S]*?\*\//g, '').replace(/@import\s+'[^']+'\s*;/g, '').trim()
    expect(rest).toBe('')
  })
})

describe('the weights the mock-ups use', () => {
  const loaded = new Set([400, 500, 600])

  it('are all loaded', () => {
    const used = new Set(mockupSources.flatMap((text) => [...text.matchAll(/font-weight:\s*(\d{3})/g)].map((m) => Number(m[1]))))
    expect(used.size).toBeGreaterThan(0)
    for (const weight of used) expect(loaded.has(weight), `weight ${String(weight)}`).toBe(true)
  })

  it('include italic, which the est. value style needs, and no mono beyond the regular weight', () => {
    expect(mockupSources.some((text) => /font-style:\s*italic/.test(text))).toBe(true)
    // `.mono` sets no weight of its own, and nothing in the mock-ups makes mono text bold.
    expect(mockupSources.some((text) => /class="[^"]*\bmono\b[^"]*"[^>]*font-weight/.test(text))).toBe(false)
  })
})

describe('the packages', () => {
  const pkg = JSON.parse(readFileSync(join(ROOT, 'package.json'), 'utf8')) as { dependencies: Record<string, string> }
  const lock = JSON.parse(readFileSync(join(ROOT, 'package-lock.json'), 'utf8')) as {
    packages: Record<string, { version?: string; license?: string; dev?: boolean }>
  }

  it.each(['@fontsource/ibm-plex-sans', '@fontsource/ibm-plex-mono'])(
    '%s is a production dependency at an exact version, under OFL-1.1',
    (name) => {
      expect(pkg.dependencies[name]).toMatch(/^\d+\.\d+\.\d+$/)
      const entry = lock.packages[`node_modules/${name}`]
      expect(entry?.version).toBe(pkg.dependencies[name])
      expect(entry?.license).toBe('OFL-1.1')
      expect(entry?.dev).not.toBe(true)
    },
  )
})
