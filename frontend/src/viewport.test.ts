// @vitest-environment node
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { describe, expect, it } from 'vitest'
import { ROOT } from './test/sourceScan'

// index.html asks for `viewport-fit=cover`, so that the page can use the whole screen of a phone
// with a notch. That also lets it reach under the notch, the status bar and the home indicator, so
// both frames pad themselves by the safe-area insets. (`sources.test.ts` still pins index.html to
// one module script and no inline script or style: the meta tag is not one.)

const html = readFileSync(join(ROOT, 'index.html'), 'utf8')
const shell = readFileSync(join(ROOT, 'src/styles/shell.css'), 'utf8')
const app = readFileSync(join(ROOT, 'src/styles/app.css'), 'utf8')

describe('the viewport', () => {
  it('is device-width, at scale 1, and covers the whole screen', () => {
    const content = /<meta name="viewport" content="([^"]*)"/.exec(html)?.[1] ?? ''
    expect(content.split(',').map((part) => part.trim())).toEqual([
      'width=device-width',
      'initial-scale=1.0',
      'viewport-fit=cover',
    ])
  })

  it('does not stop the visitor from zooming', () => {
    expect(html).not.toMatch(/user-scalable\s*=\s*(?:no|0)/i)
    expect(html).not.toMatch(/maximum-scale/i)
  })
})

describe('the safe areas', () => {
  const sides = ['top', 'right', 'bottom', 'left']

  it.each([
    ['the app frame (shell.css)', shell],
    ['the plain frame (app.css)', app],
  ])('are kept clear on every side in %s', (_name, css) => {
    for (const side of sides) expect(css, side).toContain(`env(safe-area-inset-${side})`)
  })

  it('keep the tabs of a phone clear of the home indicator and a landscape notch', () => {
    const tabs = /\.ov-phone-tabs \{[^}]*position: fixed[^}]*\}/.exec(shell)?.[0] ?? ''
    for (const side of ['right', 'bottom', 'left']) expect(tabs, side).toContain(`padding-${side}: env(safe-area-inset-${side})`)
  })
})
