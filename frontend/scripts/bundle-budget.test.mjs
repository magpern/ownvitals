import { describe, expect, it } from 'vitest'
import { BUDGET, violations } from './bundle-budget.mjs'

const ok = [
  { name: 'index-a.js', gzip: 100 * 1024 },
  { name: 'chart-b.js', gzip: 30 * 1024 },
  { name: 'index-c.css', gzip: 9 * 1024 },
]

describe('bundle budget', () => {
  it('accepts a build within budget', () => {
    expect(violations(ok)).toEqual([])
  })
  it('flags a chunk over the per-chunk limit', () => {
    const big = [...ok, { name: 'huge.js', gzip: BUDGET.jsLargestChunk + 1 }]
    expect(violations(big).some((v) => v.includes('huge.js'))).toBe(true)
  })
  it('flags a total over the JS or CSS limit', () => {
    const many = Array.from({ length: 4 }, (_, i) => ({ name: `c${i}.js`, gzip: 70 * 1024 }))
    expect(violations(many).some((v) => v.startsWith('total JS'))).toBe(true)
    expect(violations([...ok, { name: 'x.css', gzip: BUDGET.cssTotal }]).some((v) => v.startsWith('total CSS'))).toBe(true)
  })
  it('fails when no JS was built', () => {
    expect(violations([])).toHaveLength(1)
  })
})
