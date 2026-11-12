// Bundle-size budget (issue #445): gzip sizes of the built JS and CSS in dist/assets, in bytes.
// Headroom is about 25 % over the size at the time of writing; raise a limit in a PR that says why.
import { readdirSync, readFileSync } from 'node:fs'
import { join } from 'node:path'
import { gzipSync } from 'node:zlib'
import { fileURLToPath } from 'node:url'

export const BUDGET = {
  jsTotal: 260 * 1024,
  jsLargestChunk: 180 * 1024,
  cssTotal: 12 * 1024,
}

// sizes: [{ name, gzip }] -> list of human-readable violations (empty when within budget).
export function violations(sizes, budget = BUDGET) {
  const js = sizes.filter((f) => f.name.endsWith('.js'))
  const css = sizes.filter((f) => f.name.endsWith('.css'))
  const sum = (list) => list.reduce((n, f) => n + f.gzip, 0)
  const found = []
  if (js.length === 0) found.push('no JavaScript found in dist/assets (did the build run?)')
  if (sum(js) > budget.jsTotal) found.push(`total JS ${sum(js)} B gzip exceeds ${budget.jsTotal} B`)
  for (const f of js) {
    if (f.gzip > budget.jsLargestChunk) found.push(`${f.name} ${f.gzip} B gzip exceeds ${budget.jsLargestChunk} B`)
  }
  if (sum(css) > budget.cssTotal) found.push(`total CSS ${sum(css)} B gzip exceeds ${budget.cssTotal} B`)
  return found
}

export function measure(dir) {
  return readdirSync(dir)
    .filter((n) => n.endsWith('.js') || n.endsWith('.css'))
    .map((name) => ({ name, gzip: gzipSync(readFileSync(join(dir, name))).length }))
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  const dir = process.argv[2] ?? 'dist/assets'
  const found = violations(measure(dir))
  for (const v of found) console.error(`bundle budget: ${v}`)
  if (found.length > 0) process.exit(1)
  console.log('bundle budget: ok')
}
