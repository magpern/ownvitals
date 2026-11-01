import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { ROOT } from '../test/sourceScan'
import { GoogleLogo } from './GoogleLogo'

// The Google button's mark (owner decision, 2026-11-01): Google's standard four-colour "G",
// drawn from this origin with token fills; it names no URL, so nothing loads from Google until
// the click. The label is in src/pages/authPages.test.tsx ("Continue with Google").

const tokens = readFileSync(join(ROOT, 'src/styles/tokens.css'), 'utf8')

describe('GoogleLogo', () => {
  it('is the four-colour G, each colour a token, decorative and without a URL', () => {
    const { container } = render(<GoogleLogo />)
    const svg = screen.getByTestId('google-logo')
    expect(svg.getAttribute('aria-hidden')).toBe('true')
    expect(svg.getAttribute('viewBox')).toBe('0 0 48 48')
    const fills = [...svg.querySelectorAll('path')].map((path) => path.getAttribute('fill'))
    expect(fills).toEqual([
      'var(--ov-google-red)',
      'var(--ov-google-blue)',
      'var(--ov-google-yellow)',
      'var(--ov-google-green)',
    ])
    expect(container.innerHTML).not.toMatch(/https?:|\/\/|href|<image|<use|data:/i)
  })

  it("takes Google's brand colours from the tokens, the same in both themes", () => {
    const light = tokens.slice(0, tokens.indexOf('@media'))
    for (const [name, value] of [
      ['blue', '#4285f4'],
      ['red', '#ea4335'],
      ['yellow', '#fbbc05'],
      ['green', '#34a853'],
    ]) {
      expect(light).toContain(`--ov-google-${name}: ${value};`)
      expect(tokens.split(`--ov-google-${name}:`).length).toBe(2) // never overridden for dark
    }
  })

  it("has the light and dark button variants of Google's guidelines", () => {
    const light = tokens.slice(0, tokens.indexOf('@media'))
    expect(light).toContain('--ov-google-surface: #ffffff;')
    expect(light).toContain('--ov-google-border: #747775;')
    expect(light).toContain('--ov-google-text: #1f1f1f;')
    const dark = tokens.slice(tokens.indexOf('@media'))
    expect(dark.split('--ov-google-surface: #131314;').length).toBe(3) // media query and attribute
    expect(dark.split('--ov-google-border: #8e918f;').length).toBe(3)
    expect(dark.split('--ov-google-text: #e3e3e3;').length).toBe(3)
  })
})
