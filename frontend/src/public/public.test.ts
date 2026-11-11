import { existsSync, readFileSync } from 'node:fs'
import { join } from 'node:path'
import { describe, expect, it } from 'vitest'
import { LOCALES } from '../i18n/locale'
import { PUBLIC_IMAGES, imageFiles } from './images'
import { normaliseOrigin, safeJson } from './meta'
import { renderPublicPage } from './renderPublic'
import { PUBLIC_PAGES, alternates, allPublicPaths, publicPath } from './routes'
import { textFor } from './renderPublic'

// Synthetic only: the pages hold fixed marketing text and the owner-approved synthetic mock-ups.

const ORIGIN = 'https://example.test'
const STYLESHEET = '/assets/public.css'
const PNG_DIR = join(import.meta.dirname, '..', '..', '..', 'docs', 'design', 'mockups', 'png')

const render = (page: (typeof PUBLIC_PAGES)[number], locale: (typeof LOCALES)[number]) =>
  renderPublicPage({ page, locale, origin: ORIGIN, stylesheet: STYLESHEET })

const combos = PUBLIC_PAGES.flatMap((page) => LOCALES.map((locale) => [page, locale] as const))

function attr(html: string, pattern: RegExp): string[] {
  return [...html.matchAll(pattern)].map((m) => m[1] ?? '')
}

describe('routes', () => {
  it('maps each page and language to one address', () => {
    expect(allPublicPaths()).toEqual(['/', '/features', '/sv', '/sv/features'])
    expect(publicPath('home', 'en')).toBe('/')
    expect(publicPath('features', 'sv')).toBe('/sv/features')
  })

  it('lists every language and x-default (English) as alternates', () => {
    expect(alternates('features')).toEqual([
      { hreflang: 'en', path: '/features' },
      { hreflang: 'sv', path: '/sv/features' },
      { hreflang: 'x-default', path: '/features' },
    ])
  })
})

describe.each(combos)('the %s page in %s', (page, locale) => {
  const html = render(page, locale)

  it('is a complete document in its language with one h1 and a main landmark', () => {
    expect(html.startsWith('<!doctype html><html lang="' + locale + '">')).toBe(true)
    expect(html.match(/<h1[ >]/g)).toHaveLength(1)
    expect(html).toContain('<main id="content"')
    expect(html).toContain('href="#content"')
  })

  it('has a title, description, canonical, hreflang, Open Graph and Twitter tags', () => {
    const t = textFor(locale)
    const title = t(page === 'home' ? 'site.meta.home.title' : 'site.meta.features.title')
    expect(html).toContain(`<title>${title}</title>`)
    expect(html).toContain(`<link rel="canonical" href="${ORIGIN}${publicPath(page, locale)}"/>`)
    for (const alt of alternates(page)) {
      expect(html).toContain(`<link rel="alternate" hrefLang="${alt.hreflang}" href="${ORIGIN}${alt.path}"/>`)
    }
    for (const name of ['og:title', 'og:description', 'og:url', 'og:image', 'og:image:alt', 'og:locale', 'og:type']) {
      expect(html, name).toContain(`property="${name}"`)
    }
    for (const name of ['twitter:card', 'twitter:title', 'twitter:description', 'twitter:image']) {
      expect(html, name).toContain(`name="${name}"`)
    }
    expect(html).toContain('<meta name="viewport" content="width=device-width, initial-scale=1"/>')
  })

  it('carries parseable SoftwareApplication JSON-LD', () => {
    const [block] = attr(html, /<script type="application\/ld\+json">(.*?)<\/script>/gs)
    const data = JSON.parse(block ?? '') as Record<string, unknown>
    expect(data['@context']).toBe('https://schema.org')
    expect(data['@type']).toBe('SoftwareApplication')
    expect(data.inLanguage).toBe(locale)
    expect(String(data.url).startsWith(ORIGIN)).toBe(true)
  })

  it('has no script but the data block, no inline style and no event handler', () => {
    expect(html.match(/<script/g)).toHaveLength(1)
    expect(html).not.toMatch(/<style|\sstyle=|\son[a-z]+=|javascript:|<iframe|<form|<link rel="modulepreload"/i)
    expect(html.match(/<link rel="stylesheet"/g)).toHaveLength(1)
  })

  it('names no host but its own origin outside plain links', () => {
    const urls = attr(html, /(?:href|src|srcSet|content)="(https?:\/\/[^"]+)"/g)
    for (const url of urls) expect(url.startsWith(ORIGIN), url).toBe(true)
    const [block] = attr(html, /<script type="application\/ld\+json">(.*?)<\/script>/gs)
    const strings: string[] = []
    const walk = (value: unknown, key: string) => {
      if (typeof value === 'string') {
        if (key !== '@context') strings.push(value)
      } else if (value !== null && typeof value === 'object') {
        for (const [k, v] of Object.entries(value)) walk(v, k)
      }
    }
    walk(JSON.parse(block ?? ''), '')
    for (const value of strings.filter((v) => /^https?:|^\/\//i.test(v))) expect(value.startsWith(ORIGIN + '/'), value).toBe(true)
    expect(strings.some((v) => v.startsWith(ORIGIN))).toBe(true)
    const external = attr(html, /<a [^>]*href="(https?:\/\/[^"]+)"/g)
    expect(external.every((url) => url.startsWith(ORIGIN))).toBe(true)
  })

  it('links to sign-in and sign-up as ordinary links and states what it is not', () => {
    expect(html).toContain('href="/signin"')
    expect(html).toContain('href="/signup"')
    expect(html).toContain(textFor(locale)('site.footer.disclaimer'))
  })
})

describe('the languages', () => {
  it('differ in title and description, and every page differs from the others', () => {
    const titles = combos.map(([page, locale]) => /<title>(.*?)<\/title>/.exec(render(page, locale))?.[1])
    expect(new Set(titles).size).toBe(combos.length)
    const descriptions = combos.map(([page, locale]) => /name="description" content="(.*?)"/.exec(render(page, locale))?.[1])
    expect(new Set(descriptions).size).toBe(combos.length)
  })
})

describe('the images', () => {
  const html = render('home', 'en')

  it('are the fixed list of owner-approved mock-ups and no others', () => {
    expect(imageFiles().sort()).toEqual([
      'Connections.dark.png',
      'Connections.png',
      'Dashboard.dark.png',
      'Dashboard.png',
      'Findings.dark.png',
      'Findings.png',
    ])
    const shown = [...attr(html, /<img [^>]*src="([^"]+)"/g), ...attr(html, /<source [^>]*srcSet="([^"]+)"/g)]
    expect(shown.sort()).toEqual(imageFiles().map((file) => `/img/${file}`).sort())
  })

  it('exist in docs/design with the stated dimensions', () => {
    for (const image of PUBLIC_IMAGES) {
      for (const file of [image.light, image.dark]) {
        const path = join(PNG_DIR, file)
        expect(existsSync(path), file).toBe(true)
        const header = readFileSync(path).subarray(0, 24)
        expect(header.readUInt32BE(16), `${file} width`).toBe(image.width)
        expect(header.readUInt32BE(20), `${file} height`).toBe(image.height)
      }
    }
  })

  it('have alt text and explicit dimensions', () => {
    const imgs = html.match(/<img [^>]*>/g) ?? []
    expect(imgs).toHaveLength(PUBLIC_IMAGES.length)
    for (const img of imgs) {
      expect(img).toMatch(/alt="[^"]{10,}"/)
      expect(img).toMatch(/width="\d+"/)
      expect(img).toMatch(/height="\d+"/)
    }
  })
})

describe('the head helpers', () => {
  it('accepts a bare https origin only', () => {
    expect(normaliseOrigin('https://example.test/')).toBe(ORIGIN)
    for (const bad of ['http://localhost', 'http://localhost:8000', 'http://example.test', 'https://example.test/x', 'https://example.test/?q=1', 'ftp://example.test', 'nope']) {
      expect(() => normaliseOrigin(bad), bad).toThrow()
    }
  })

  it('cannot let JSON close its script element', () => {
    expect(safeJson({ a: '</script><script>x' })).not.toContain('<')
    expect(JSON.parse(safeJson({ a: '</script>' }))).toEqual({ a: '</script>' })
  })
})
