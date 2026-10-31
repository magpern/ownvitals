import '@testing-library/jest-dom/vitest'
import { cleanup, configure } from '@testing-library/react'
import { afterEach } from 'vitest'

// The first render of the whole app in a test file is slow on a loaded machine (cold JIT, CI
// runners): findBy* and waitFor give it 5 s instead of 1 s. Nothing passes sooner or later
// because of this; it only stops a slow start from failing an assertion that would hold.
configure({ asyncUtilTimeout: 5000 })

// Vitest globals are off, so Testing Library cannot register its own cleanup.
afterEach(() => {
  cleanup()
})

// jsdom does not implement scrolling. (A test file that runs in Node, such as build.test.ts, has
// no window.)
if (typeof window !== 'undefined') window.scrollTo = () => undefined
