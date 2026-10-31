import '@testing-library/jest-dom/vitest'
import { cleanup, configure } from '@testing-library/react'
import { afterEach } from 'vitest'
import { networkGuard } from './networkGuard'

// The first render of the whole app in a test file is slow on a loaded machine (cold JIT, CI
// runners): findBy* and waitFor give it 5 s instead of 1 s. Nothing passes sooner or later
// because of this; it only stops a slow start from failing an assertion that would hold.
configure({ asyncUtilTimeout: 5000 })

// Tests never touch the network. Until a test stubs `fetch` (fakeApi, vi.stubGlobal), `fetch`,
// XMLHttpRequest and WebSocket refuse, and the test fails after it ends even if the code under
// test caught the refusal.
networkGuard.install()

// Vitest globals are off, so Testing Library cannot register its own cleanup.
afterEach(() => {
  cleanup()
  const attempts = networkGuard.drain()
  if (attempts.length > 0) {
    throw new Error(
      `The test tried to reach the network (it must stub fetch, see fakeApi):\n  ${attempts.join('\n  ')}`,
    )
  }
})

// jsdom does not implement scrolling. (A test file that runs in Node, such as build.test.ts, has
// no window.)
if (typeof window !== 'undefined') window.scrollTo = () => undefined
