// @vitest-environment node
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { describe, expect, it } from 'vitest'
import { ROOT } from '../test/sourceScan'

// The tokens of src/styles/tokens.css against the design they come from, the `.ov` and
// `.ov[data-theme="dark"]` blocks of docs/design/mockups/src/ov.css, in both themes, and the way
// a theme is chosen: Light is the default, the device's `prefers-color-scheme` gives Dark, and
// `data-theme` on <html> overrides the device either way. There is no browser here: the stylesheet
// is read as text, and the cascade of its few rules is worked out by `resolve` below.

const tokensCss = readFileSync(join(ROOT, 'src/styles/tokens.css'), 'utf8')
const ovCss = readFileSync(join(ROOT, '..', 'docs/design/mockups/src/ov.css'), 'utf8')

interface Rule {
  selector: string
  media: string | null
  declarations: Map<string, string>
}

function withoutComments(css: string): string {
  return css.replace(/\/\*[\s\S]*?\*\//g, '')
}

function declarationsOf(body: string): Map<string, string> {
  const found = new Map<string, string>()
  for (const part of body.split(';')) {
    const colon = part.indexOf(':')
    if (colon < 0) continue
    found.set(part.slice(0, colon).trim(), part.slice(colon + 1).replace(/\s+/g, ' ').trim())
  }
  return found
}

/** The rules of a stylesheet, one level of `@media` deep, in source order. */
function parseRules(css: string, media: string | null = null): Rule[] {
  const rules: Rule[] = []
  const text = withoutComments(css)
  let depth = 0
  let start = 0
  let prelude = ''
  for (let i = 0; i < text.length; i += 1) {
    const char = text[i]
    if (char === '{') {
      if (depth === 0) {
        prelude = text.slice(start, i).trim()
        start = i + 1
      }
      depth += 1
    } else if (char === '}') {
      depth -= 1
      if (depth === 0) {
        const body = text.slice(start, i)
        if (prelude.startsWith('@media')) rules.push(...parseRules(body, prelude.slice('@media'.length).trim()))
        else rules.push({ selector: prelude.replace(/\s+/g, ' '), media, declarations: declarationsOf(body) })
        start = i + 1
      }
    }
  }
  return rules
}

const tokenRules = parseRules(tokensCss)

type Choice = 'system' | 'light' | 'dark'

/** Whether a rule of tokens.css applies to a root with this `data-theme` choice, on this device. */
function applies(rule: Rule, choice: Choice, deviceIsDark: boolean): boolean {
  const key = `${rule.media ?? ''} | ${rule.selector}`
  switch (key) {
    case ' | :root':
      return true
    case "(prefers-color-scheme: dark) | :root:not([data-theme='light'])":
      return deviceIsDark && choice !== 'light'
    case " | :root[data-theme='light']":
      return choice === 'light'
    case " | :root[data-theme='dark']":
      return choice === 'dark'
    default:
      // A new kind of rule has to be taught to this test, or it would go unchecked.
      throw new Error(`tokens.css has a rule this test does not know: ${key}`)
  }
}

/**
 * The value of every custom property, `color-scheme` included, for a root with this choice on
 * this device. The rules never differ in specificity in a way source order does not give, so the
 * last one that applies wins.
 */
function resolve(choice: Choice, deviceIsDark: boolean): Map<string, string> {
  const values = new Map<string, string>()
  for (const rule of tokenRules) {
    if (!applies(rule, choice, deviceIsDark)) continue
    for (const [name, value] of rule.declarations) values.set(name, value)
  }
  return values
}

// --- the design's colours --------------------------------------------------------------------

function ovVariables(selector: string): Map<string, string> {
  const rule = parseRules(ovCss).find((candidate) => candidate.selector === selector)
  if (rule === undefined) throw new Error(`ov.css has no ${selector} block`)
  return new Map([...rule.declarations].filter(([name]) => name.startsWith('--')))
}

/** ov.css names the ink `--ink` and the text on the brand colour `--brand-ink`; the app says text and on-brand. */
function tokenName(variable: string): string {
  const name = variable.slice(2)
  return `--ov-${name === 'ink' ? 'text' : name === 'brand-ink' ? 'on-brand' : name}`
}

const lightDesign = ovVariables('.ov')
const darkDesign = new Map([...lightDesign, ...ovVariables('.ov[data-theme="dark"]')])

function expected(design: Map<string, string>): Map<string, string> {
  return new Map([...design].map(([name, value]) => [tokenName(name), value.toLowerCase()]))
}

function colours(values: Map<string, string>, names: Iterable<string>): Map<string, string> {
  return new Map([...names].map((name) => [name, (values.get(name) ?? '(missing)').toLowerCase()]))
}

describe('the colours of the design', () => {
  it('reads the design: some thirty colours, with a dark override of most', () => {
    expect(lightDesign.size).toBeGreaterThan(30)
    expect(ovVariables('.ov[data-theme="dark"]').size).toBeGreaterThan(25)
  })

  it('has every colour of ov.css as an --ov-* token, with its light value', () => {
    const want = expected(lightDesign)
    expect(colours(resolve('light', false), want.keys())).toEqual(want)
    expect(colours(resolve('system', false), want.keys())).toEqual(want)
  })

  it('has every colour of ov.css as an --ov-* token, with its dark value', () => {
    const want = expected(darkDesign)
    expect(colours(resolve('dark', true), want.keys())).toEqual(want)
    expect(colours(resolve('system', true), want.keys())).toEqual(want)
  })

  it('keeps the QR code dark on light in both themes', () => {
    for (const [choice, device] of [['light', false], ['dark', true], ['system', true]] as const) {
      const values = resolve(choice, device)
      expect(values.get('--ov-qr-ink')).toBe('#14181d')
      expect(values.get('--ov-qr-paper')).toBe('#ffffff')
    }
  })
})

describe('choosing the theme', () => {
  const colourNames = [...expected(lightDesign).keys()]

  it.each<[Choice, boolean, 'light' | 'dark', string]>([
    ['system', false, 'light', 'System on a light device is light'],
    ['system', true, 'dark', 'System on a dark device is dark (the media query)'],
    ['light', false, 'light', 'Light on a light device is light'],
    ['light', true, 'light', 'Light overrides a dark device'],
    ['dark', false, 'dark', 'Dark overrides a light device'],
    ['dark', true, 'dark', 'Dark on a dark device is dark'],
  ])('%s, device dark: %s -> %s (%s)', (choice, deviceIsDark, theme) => {
    const want = expected(theme === 'dark' ? darkDesign : lightDesign)
    expect(colours(resolve(choice, deviceIsDark), colourNames)).toEqual(colours(want, colourNames))
  })

  it('tells the browser which scheme is in force, for scrollbars and form controls', () => {
    expect(resolve('system', false).get('color-scheme')).toBe('light dark')
    expect(resolve('system', true).get('color-scheme')).toBe('light dark')
    for (const device of [false, true]) {
      expect(resolve('light', device).get('color-scheme')).toBe('light')
      expect(resolve('dark', device).get('color-scheme')).toBe('dark')
    }
  })

  it('writes the dark colours twice, for the media query and for the attribute, and keeps them equal', () => {
    const inMedia = tokenRules.find((rule) => rule.media !== null)
    const inAttribute = tokenRules.find((rule) => rule.selector === ":root[data-theme='dark']")
    const onlyColours = (rule: Rule | undefined) =>
      [...(rule?.declarations ?? [])].filter(([name]) => name !== 'color-scheme')
    expect(onlyColours(inMedia).length).toBeGreaterThan(25)
    expect(onlyColours(inMedia)).toEqual(onlyColours(inAttribute))
  })

  it('applies the media query to a root with no Light override only', () => {
    const media = tokenRules.filter((rule) => rule.media !== null)
    expect(media.map((rule) => [rule.media, rule.selector])).toEqual([
      ['(prefers-color-scheme: dark)', ":root:not([data-theme='light'])"],
    ])
  })
})

describe('type', () => {
  const light = resolve('light', false)

  it('puts IBM Plex first, with the system fonts as the fallback', () => {
    expect(light.get('--ov-font-sans')).toMatch(/^'IBM Plex Sans', system-ui, /)
    expect(light.get('--ov-font-mono')).toMatch(/^'IBM Plex Mono', ui-monospace, /)
  })

  it('has the weights the design uses, and no bold', () => {
    expect(light.get('--ov-weight-regular')).toBe('400')
    expect(light.get('--ov-weight-medium')).toBe('500')
    expect(light.get('--ov-weight-semibold')).toBe('600')
    expect([...light.keys()].filter((name) => name.startsWith('--ov-weight-'))).toHaveLength(3)
  })
})

// --- contrast --------------------------------------------------------------------------------

function luminance(hex: string): number {
  const [r = 0, g = 0, b = 0] = [1, 3, 5].map((at) => {
    const channel = parseInt(hex.slice(at, at + 2), 16) / 255
    return channel <= 0.03928 ? channel / 12.92 : ((channel + 0.055) / 1.055) ** 2.4
  })
  return 0.2126 * r + 0.7152 * g + 0.0722 * b
}

function contrast(a: string, b: string): number {
  const [lighter = 0, darker = 0] = [luminance(a), luminance(b)].sort((x, y) => y - x)
  return (lighter + 0.05) / (darker + 0.05)
}

describe('contrast', () => {
  // The design says every text colour passes 4.5:1 on its ground in both themes (Main, "Colour").
  const pairs: [string, string][] = [
    ['text', 'bg'],
    ['text', 'surface'],
    ['text', 'raised'],
    ['muted', 'bg'],
    ['muted', 'surface'],
    ['muted', 'raised'],
    ['muted', 'brand-soft'],
    ['brand', 'bg'],
    ['brand', 'surface'],
    ['brand', 'brand-soft'],
    ['on-brand', 'brand'],
    ['device', 'surface'],
    ['device', 'device-soft'],
    ['trend', 'trend-soft'],
    ['exp', 'surface'],
    ['exp', 'exp-soft'],
    ['attn', 'surface'],
    ['attn', 'attn-soft'],
    ['urgent', 'surface'],
    ['urgent', 'urgent-soft'],
    ['ok', 'surface'],
    ['ok', 'ok-soft'],
    ['admin-ink', 'admin'],
  ]
  it.each([
    ['light', resolve('light', false)],
    ['dark', resolve('dark', false)],
  ])('has text colours at 4.5:1 or better on their grounds in %s', (_theme, values) => {
    for (const [text, ground] of pairs) {
      const ratio = contrast(values.get(`--ov-${text}`) ?? '', values.get(`--ov-${ground}`) ?? '')
      expect(ratio, `${text} on ${ground}`).toBeGreaterThanOrEqual(4.5)
    }
  })
})

// --- touch targets and the phone breakpoint ----------------------------------------------------

describe('the frame on a phone', () => {
  const shell = parseRules(readFileSync(join(ROOT, 'src/styles/shell.css'), 'utf8'))
  const primitives = parseRules(readFileSync(join(ROOT, 'src/styles/primitives.css'), 'utf8'))
  const find = (rules: Rule[], selector: string, media: string | null = null) =>
    rules.find((rule) => rule.selector === selector && rule.media === media)?.declarations

  it('has a target of 44 px, and every control of the frame is at least that high', () => {
    expect(resolve('light', false).get('--ov-target')).toBe('2.75rem')
    for (const [rules, selector] of [
      [primitives, '.ov-btn'],
      [shell, '.ov-brand'],
      [shell, '.ov-navlist a'],
      [shell, '.ov-acct'],
      [shell, '.ov-menu a, .ov-menu button'],
      [shell, '.ov-topbar__logo'],
    ] as const) {
      expect(find(rules, selector)?.get('min-height'), selector).toBe('var(--ov-target)')
    }
    // The tabs of a phone are 4 rem (64 px) high.
    expect(find(shell, '.ov-phone-tabs a')?.get('min-height')).toBe('4rem')
  })

  it('shows the rail on a desktop and the tabs on a phone, switching at 760 px, as the mock-up does', () => {
    expect(find(shell, '.ov-phone-tabs')?.get('display')).toBe('none')
    expect(find(shell, '.ov-rail-wrap')?.get('display')).toBeUndefined()
    const media = '(max-width: 760px)'
    expect(find(shell, '.ov-rail-wrap', media)?.get('display')).toBe('none')
    expect(find(shell, '.ov-phone-tabs', media)?.get('display')).toBe('grid')
    expect(find(shell, '.ov-phone-tabs', media)?.get('grid-template-columns')).toBe('repeat(4, minmax(0, 1fr))')
    expect(find(shell, '.ov-phone-tabs', media)?.get('position')).toBe('fixed')
    expect(find(shell, '.ov-topbar__logo', media)?.get('display')).toBe('inline-flex')
    expect(find(shell, '.ov-topbar__logo')?.get('display')).toBe('none')
    expect(find(shell, '.ov-app', media)?.get('grid-template-columns')).toBe('minmax(0, 1fr)')
  })

  it('leaves room above the tabs, so they never cover the end of a page', () => {
    expect(find(shell, '.ov-content', '(max-width: 760px)')?.get('padding')).toContain('calc(5rem')
  })
})
