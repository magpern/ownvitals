import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { describe, expect, it } from 'vitest'
import { ROOT } from '../test/sourceScan'

// jsdom does no layout, so this pins the rule that keeps the notice above the phone tabs.
const css = readFileSync(join(ROOT, 'src/styles/cookie-notice.css'), 'utf8')
const tabs = readFileSync(join(ROOT, 'src/styles/shell-tabs.css'), 'utf8')

describe('cookie notice on the phone', () => {
  it('sits above the bottom tabs, by their height, on the 760 px breakpoint the tabs use', () => {
    expect(tabs).toContain('@media (max-width: 760px)')
    expect(tabs).toContain('min-height: 4rem')
    expect(css).toMatch(
      /@media \(max-width: 760px\) \{\s*body:has\(\.ov-phone-tabs\) \.cookie-notice \{\s*bottom: calc\(4rem \+ 1px \+ var\(--ov-space-4\) \+ env\(safe-area-inset-bottom\)\);/,
    )
  })
})
