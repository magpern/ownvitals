// @vitest-environment node
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { describe, expect, it } from 'vitest'
import { cspViolations, ROOT } from '../../test/sourceScan'

const read = (path: string) => readFileSync(join(ROOT, path), 'utf8')
const tokens = read('src/styles/tokens.css')
const motion = read('src/styles/motion.css')

describe('motion tokens and styles', () => {
  it.each([
    ['--ov-dur-instant', '90ms'],
    ['--ov-dur-fast', '160ms'],
    ['--ov-dur-base', '240ms'],
    ['--ov-dur-slow', '480ms'],
    ['--ov-dur-count', '700ms'],
    ['--ov-stagger', '40ms'],
    ['--ov-lift-y', '-2px'],
  ])('defines %s as %s', (name, value) => {
    expect(tokens).toContain(`${name}: ${value};`)
  })

  it('defines the easings, glow (both themes) and sheen', () => {
    for (const name of ['--ov-ease-out', '--ov-ease-in-out', '--ov-ease-in', '--ov-sheen', '--ov-motion']) {
      expect(tokens).toContain(`${name}:`)
    }
    expect(tokens.match(/--ov-glow:/g)).toHaveLength(3)
  })

  it('zeroes motion for the media query and for data-motion="off"', () => {
    expect(motion).toContain('@media (prefers-reduced-motion: reduce)')
    expect(motion).toContain(":root[data-motion='off']")
    expect(motion.match(/transition-duration: 0\.01ms !important/g)).toHaveLength(2)
    expect(motion.match(/animation-duration: 0\.01ms !important/g)).toHaveLength(2)
    expect(motion).toMatch(/\.ov-skeleton::after \{\s*animation: none/)
  })

  it('animates only opacity and transform (no layout property, no literal duration)', () => {
    const noComments = motion.replace(/\/\*[\s\S]*?\*\//g, '')
    for (const prop of ['width', 'height', 'margin', 'padding', 'top', 'left', 'font-size']) {
      expect(noComments).not.toMatch(new RegExp(`transition:[^;]*\\b${prop}\\b`))
    }
    expect(noComments.replace(/0\.01ms|\b0s\b/g, '')).not.toMatch(/\b\d+(\.\d+)?m?s\b/)
  })
})

describe('CSP and animated values (motion.md rule 8)', () => {
  it('allows values set through the element.style property API', () => {
    const code = "ref.current.style.setProperty('--ov-reveal-i', '3'); el.style.opacity = '1'"
    expect(cspViolations('src/components/motion/x.tsx', code)).toEqual([])
  })

  it('refuses a style attribute', () => {
    expect(cspViolations('src/components/motion/x.tsx', 'return <p style={{ opacity: 0 }} />').join()).toContain(
      'inline style attribute',
    )
  })
})
