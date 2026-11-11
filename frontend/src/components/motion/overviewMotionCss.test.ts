// @vitest-environment node
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { describe, expect, it } from 'vitest'
import { ROOT } from '../../test/sourceScan'

const read = (path: string) => readFileSync(join(ROOT, path), 'utf8')
const motion = read('src/styles/motion.css')

describe('overview motion styles (#438)', () => {
  it('draws the chart in with a clip-path, once, from tokens', () => {
    expect(motion).toMatch(/\.ov-chart--draw \{\s*animation: ov-draw var\(--ov-dur-slow\) var\(--ov-ease-out\);/)
    expect(motion).toContain('clip-path: inset(0 100% 0 0)')
  })

  it('pulses the last point for one iteration and cross-fades the page content', () => {
    expect(motion).toMatch(/\.ov-c-pulse \{[^}]*ov-pulse[^}]* 1 both;/)
    expect(motion).toMatch(/\.ov-page \{\s*animation: ov-fade-in/)
  })

  it('wires the classes in the components', () => {
    expect(read('src/components/charts/TimeSeriesChart.tsx')).toContain('ov-chart--draw')
    expect(read('src/components/shell/AppFrame.tsx')).toContain('ov-page')
  })
})
