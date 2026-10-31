// @vitest-environment node
import { spawnSync } from 'node:child_process'
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { afterAll, beforeAll, describe, expect, it } from 'vitest'
import { ROOT, SRC } from './sourceScan'

// src/test/setup.ts fails a test in its `afterEach` when the code under test reached for the
// network and swallowed the refusal. networkGuard.test.ts tests the guard's own `drain()`, so it
// cannot notice if that hook is removed or stops throwing. This file runs Vitest, as a child
// process, on scratch test files that swallow a refused call, with the real setup file, and
// checks that the run fails the right tests and passes the others.
//
// The scratch files only name relative URLs, and the run happens in a scratch folder with its own
// configuration (the real one is a function of the mode, with plugins this run does not need), so
// even with the guard gone nothing would leave the machine: Node's `fetch` rejects a relative URL,
// and jsdom has no `EventSource` and no `navigator.sendBeacon`.

interface Report {
  success: boolean
  numPassedTests: number
  numFailedTests: number
  testResults: {
    name: string
    status: string
    message: string
    assertionResults: { title: string; status: string; failureMessages: string[] }[]
  }[]
}

// Each test swallows a different kind of refusal; the last one never touches the network and
// must pass: the failure belongs to the test that made the attempt, not to the file.
const SWALLOWING = `
it('swallows a refused fetch', async () => {
  const outcome = await fetch('/api/auth/session?token=abc123').catch(() => 'recovered')
  expect(outcome).toBe('recovered')
})
it('swallows a refused EventSource', () => {
  try {
    new EventSource('/api/stream')
  } catch {
    // carried on
  }
})
it('ignores the answer of sendBeacon', () => {
  navigator.sendBeacon('/api/log', 'payload')
})
it('does not touch the network', () => {
  expect(1 + 1).toBe(2)
})
`

// What a test is allowed to do: stub what it uses.
const STUBBING = `
it('stubs fetch', async () => {
  vi.stubGlobal('fetch', () => Promise.resolve(new Response('ok')))
  expect(await (await fetch('/api/auth/session')).text()).toBe('ok')
})
`

let scratch = ''
let status: number | null = null
let report: Report

beforeAll(() => {
  scratch = mkdtempSync(join(tmpdir(), 'ownvitals-setup-'))
  const config = {
    // The setup file lives outside the scratch root.
    server: { fs: { allow: [scratch, ROOT] } },
    test: {
      environment: 'jsdom',
      setupFiles: [join(SRC, 'test', 'setup.ts')],
      include: ['*.test.mjs'],
      globals: true, // the scratch files import nothing: the scratch folder has no node_modules
      unstubGlobals: true,
      restoreMocks: true,
    },
  }
  writeFileSync(join(scratch, 'vitest.config.mjs'), `export default ${JSON.stringify(config)}\n`)
  writeFileSync(join(scratch, 'swallowing.test.mjs'), SWALLOWING)
  writeFileSync(join(scratch, 'stubbing.test.mjs'), STUBBING)

  // Not the variables of the running worker: this is a separate, top-level run.
  const env = Object.fromEntries(Object.entries(process.env).filter(([name]) => !/^VITEST/i.test(name)))
  const result = spawnSync(
    process.execPath,
    [
      join(ROOT, 'node_modules', 'vitest', 'vitest.mjs'),
      'run',
      '--root',
      scratch,
      '--config',
      join(scratch, 'vitest.config.mjs'),
      '--reporter=json',
      `--outputFile=${join(scratch, 'report.json')}`,
    ],
    { cwd: scratch, env, encoding: 'utf8', timeout: 90_000 },
  )
  status = result.status
  try {
    report = JSON.parse(readFileSync(join(scratch, 'report.json'), 'utf8')) as Report
  } catch {
    throw new Error(`the nested Vitest run left no report (exit ${String(status)}):\n${result.stdout}\n${result.stderr}`)
  }
}, 120_000)

afterAll(() => {
  if (scratch !== '') rmSync(scratch, { recursive: true, force: true })
})

function file(suffix: string) {
  const found = report.testResults.find((result) => result.name.endsWith(suffix))
  if (found === undefined) throw new Error(`the nested run has no ${suffix}`)
  return found
}

function testOf(suffix: string, title: string) {
  const found = file(suffix).assertionResults.find((result) => result.title === title)
  if (found === undefined) throw new Error(`the nested run has no test "${title}" in ${suffix}`)
  return found
}

describe('the afterEach hook of src/test/setup.ts, run on scratch test files', () => {
  it('ran both scratch files to the end, so a failure below is the hook and not a broken run', () => {
    expect(file('swallowing.test.mjs').message).toBe('')
    expect(file('stubbing.test.mjs').message).toBe('')
    expect(file('swallowing.test.mjs').assertionResults).toHaveLength(4)
    expect(file('stubbing.test.mjs').assertionResults).toHaveLength(1)
  })

  it.each([
    ['swallows a refused fetch', 'GET /api/auth/session'],
    ['swallows a refused EventSource', 'new EventSource /api/stream'],
    ['ignores the answer of sendBeacon', 'POST /api/log (sendBeacon)'],
  ])('fails a test that swallowed a refusal: %s', (title, attempt) => {
    const result = testOf('swallowing.test.mjs', title)
    expect(result.status).toBe('failed')
    const message = result.failureMessages.join('\n')
    expect(message).toContain('The test tried to reach the network')
    expect(message).toContain(`\n  ${attempt}\n`)
  })

  it('names the address without its query, which may hold a secret', () => {
    const messages = file('swallowing.test.mjs')
      .assertionResults.flatMap((result) => result.failureMessages)
      .join('\n')
    expect(messages).toContain('GET /api/auth/session\n')
    expect(messages).not.toMatch(/abc123|token=/)
  })

  it('fails only the tests that made an attempt', () => {
    expect(testOf('swallowing.test.mjs', 'does not touch the network').status).toBe('passed')
    expect(testOf('stubbing.test.mjs', 'stubs fetch').status).toBe('passed')
    expect(report.numFailedTests).toBe(3)
    expect(report.numPassedTests).toBe(2)
  })

  it('makes the run exit with a failure', () => {
    expect(report.success).toBe(false)
    expect(status).not.toBe(0)
  })
})
