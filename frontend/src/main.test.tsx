import { beforeEach, describe, expect, it, vi } from 'vitest'

// main.tsx puts the theme on <html> before React renders anything, so the first paint of the app
// is in the right theme, and does it as a module of the bundle, not an inline script (index.html
// has one script, a module, and sources.test.ts keeps it so).

const rendered = vi.hoisted(() => [] as (string | null)[])
const languages = vi.hoisted(() => [] as string[])

vi.mock('react-dom/client', () => ({
  createRoot: () => ({
    render: () => {
      rendered.push(document.documentElement.getAttribute('data-theme'))
      languages.push(document.documentElement.lang)
    },
  }),
}))
vi.mock('./App', () => ({ App: () => null }))

async function start() {
  vi.resetModules()
  await import('./main')
}

beforeEach(() => {
  rendered.length = 0
  languages.length = 0
  window.localStorage.clear()
  document.documentElement.removeAttribute('data-theme')
  document.body.innerHTML = '<div id="root"></div>'
})

describe('main.tsx', () => {
  it('applies a stored Dark before the first render', async () => {
    window.localStorage.setItem('ownvitals.theme', 'dark')
    await start()
    expect(rendered).toEqual(['dark'])
  })

  it('applies a stored Light before the first render', async () => {
    window.localStorage.setItem('ownvitals.theme', 'light')
    await start()
    expect(rendered).toEqual(['light'])
  })

  it('sets no attribute for System before the first render', async () => {
    await start()
    expect(rendered).toEqual([null])
  })

  it('renders at once in English', async () => {
    await start()
    expect(languages).toEqual(['en'])
  })

  it('waits for the stored language, and renders in it', async () => {
    window.localStorage.setItem('ov-locale', 'sv')
    await start()
    await vi.waitFor(() => {
      expect(languages).toEqual(['sv'])
    })
  })

  it('adds no inline script or style to the page', async () => {
    window.localStorage.setItem('ownvitals.theme', 'dark')
    await start()
    expect(document.querySelectorAll('script:not([src]), style')).toHaveLength(0)
    expect(document.documentElement.hasAttribute('style')).toBe(false)
  })
})
