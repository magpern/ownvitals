import '@testing-library/jest-dom/vitest'
import { cleanup, configure } from '@testing-library/react'
import { afterEach, beforeEach } from 'vitest'
import { COOKIE_NOTICE_KEY, COOKIE_NOTICE_VERSION } from '../components/notice/cookieNoticeStore'
import { networkGuard } from './networkGuard'

// The first render of the whole app in a test file is slow on a loaded machine (cold JIT, CI
// runners): findBy* and waitFor give it 5 s instead of 1 s. Nothing passes sooner or later
// because of this; it only stops a slow start from failing an assertion that would hold.
configure({ asyncUtilTimeout: 5000 })

// Tests never touch the network. Until a test stubs it (fakeApi, vi.stubGlobal), `fetch`,
// XMLHttpRequest, WebSocket, EventSource and navigator.sendBeacon refuse, and the test fails after
// it ends even if the code under test caught the refusal.
networkGuard.install()

// The first-visit cookie notice is a dialog; every test starts as a visitor who has seen it, and
// its own test file clears the key.
beforeEach(() => {
  // Some files run in the node environment, with no window.
  if (typeof window !== 'undefined') window.localStorage.setItem(COOKIE_NOTICE_KEY, COOKIE_NOTICE_VERSION)
})

// Vitest globals are off, so Testing Library cannot register its own cleanup.
afterEach(() => {
  cleanup()
  const attempts = networkGuard.drain()
  if (attempts.length > 0) {
    throw new Error(
      `The test tried to reach the network (it must stub what it uses; for fetch see fakeApi):\n  ${attempts.join('\n  ')}`,
    )
  }
})

// jsdom does not implement scrolling. (A test file that runs in Node, such as build.test.ts, has
// no window.)
if (typeof window !== 'undefined') window.scrollTo = () => undefined
