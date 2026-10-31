import { render } from '@testing-library/react'
import { App } from '../App'

/** Renders the whole app at `url` (path, query and fragment), like a browser opening it. */
export function renderAt(url: string) {
  window.history.replaceState(null, '', url)
  return render(<App />)
}
