// @vitest-environment node
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { describe, expect, it } from 'vitest'
import { renderPublicPage } from './renderPublic'
import { ROOT } from '../test/sourceScan'

const render = (locale: 'en' | 'sv') =>
  renderPublicPage({ page: 'home', locale, origin: 'https://ownvitals.example', stylesheet: '/public.css' })
const css = readFileSync(join(ROOT, 'src/styles/public-lively.css'), 'utf8').replace(/\/\*[\s\S]*?\*\//g, '')

describe('the lively home page', () => {
  it.each(['en', 'sv'] as const)('has the mock-up labelled as example data, a stat strip and three steps (%s)', (locale) => {
    const html = render(locale)
    expect(html).toContain('pub-mock')
    expect(html).toContain(locale === 'en' ? 'Example data' : 'Exempeldata')
    expect(html).toContain('pub-stats')
    expect(html.match(/<li><h3>/g)?.length).toBeGreaterThanOrEqual(3)
    expect(html).toContain('<ol class="pub-steps')
  })

  it('stays script-free and inline-style-free', () => {
    const html = render('en')
    expect([...html.matchAll(/<script\b[^>]*>/g)].map((m) => m[0])).toEqual(['<script type="application/ld+json">'])
    expect(html).not.toMatch(/\sstyle=|<style/)
  })
})

describe('the lively stylesheet', () => {
  it('animates only inside the no-preference media query, so reduced motion shows the final state', () => {
    const outside = css.slice(0, css.indexOf('@media (prefers-reduced-motion: no-preference)'))
    expect(outside).not.toMatch(/animation|transition|@keyframes/)
    expect(css).toContain('@media (prefers-reduced-motion: no-preference)')
  })

  it('uses duration tokens only', () => {
    expect(css.replace(/\b0s\b/g, '')).not.toMatch(/\b\d+(\.\d+)?m?s\b/)
  })
})
