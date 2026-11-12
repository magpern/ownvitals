import { act, fireEvent, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { ANONYMOUS, fakeApi, reason } from '../test/fakeApi'
import { renderAt } from '../test/render'
import { forgetTurnstile, loadTurnstile, type RenderOptions, type TurnstileApi } from './Turnstile'

// Cloudflare Turnstile on the three anonymous forms (ADR 0018). The widget is a fake on
// `window.turnstile`: no script is loaded, nothing leaves the test. The keys are synthetic.

const ON = {
  ...ANONYMOUS,
  turnstile: { site_key: 'synthetic-site-key', script_url: '/synthetic/turnstile.js' },
}

interface Rendered {
  sitekey: string
  action: string
  solve: (token: string) => void
  expire: () => void
}

function fakeWidget() {
  const rendered: Rendered[] = []
  const api: TurnstileApi = {
    render: vi.fn((_element: HTMLElement, options: RenderOptions) => {
      rendered.push({
        sitekey: options.sitekey,
        action: options.action,
        solve: options.callback,
        expire: options['expired-callback'],
      })
      return `widget-${rendered.length}`
    }),
    reset: vi.fn(),
    remove: vi.fn(),
  }
  window.turnstile = api
  return { api, rendered }
}

function type(label: string, value: string) {
  fireEvent.change(screen.getByLabelText(label), { target: { value } })
}

afterEach(() => {
  delete window.turnstile
  forgetTurnstile()
})

describe('while Turnstile is off', () => {
  it('shows no widget, loads no script and sends no token', async () => {
    const api = fakeApi({ 'POST /api/auth/login': reason(401, 'invalid_credentials') })
    renderAt('/signin')
    await screen.findByLabelText('Email')
    expect(document.querySelector('.ov-turnstile')).toBeNull()
    expect(document.querySelector('script[src]')).toBeNull()
    type('Email', 'ann@example.test')
    type('Password', 'correct horse battery')
    fireEvent.click(screen.getByRole('button', { name: 'Sign in' }))
    await waitFor(() => {
      expect(api.to('POST', '/api/auth/login')).toHaveLength(1)
    })
    expect(api.to('POST', '/api/auth/login')[0]?.headers.has('X-Turnstile-Token')).toBe(false)
  })
})

describe('while Turnstile is on', () => {
  it('waits for the widget on sign-in, sends its token once and resets it', async () => {
    const { api: widget, rendered } = fakeWidget()
    const api = fakeApi({
      'GET /api/auth/session': ON,
      'POST /api/auth/login': reason(400, 'turnstile_failed'),
    })
    renderAt('/signin')
    await screen.findByLabelText('Email')
    await waitFor(() => {
      expect(rendered).toHaveLength(1)
    })
    expect(rendered[0]).toMatchObject({ sitekey: 'synthetic-site-key', action: 'signin' })
    type('Email', 'ann@example.test')
    type('Password', 'correct horse battery')
    const button = screen.getByRole('button', { name: 'Sign in' })
    expect(button).toBeDisabled()
    act(() => {
      rendered[0]?.solve('synthetic.token')
    })
    expect(button).toBeEnabled()
    fireEvent.click(button)
    expect(await screen.findByText(/not a robot/)).toBeInTheDocument()
    const [call] = api.to('POST', '/api/auth/login')
    expect(call?.headers.get('X-Turnstile-Token')).toBe('synthetic.token')
    expect(widget.reset).toHaveBeenCalledWith('widget-1')
    expect(button).toBeDisabled() // the used token is gone
  })

  it('forgets an expired token', async () => {
    const { rendered } = fakeWidget()
    fakeApi({ 'GET /api/auth/session': ON })
    renderAt('/signin')
    await waitFor(() => {
      expect(rendered).toHaveLength(1)
    })
    const button = screen.getByRole('button', { name: 'Sign in' })
    act(() => {
      rendered[0]?.solve('synthetic.token')
    })
    expect(button).toBeEnabled()
    act(() => {
      rendered[0]?.expire()
    })
    expect(button).toBeDisabled()
  })

  it.each([
    ['/signup', 'signup', 'POST /api/auth/signup'],
    ['/reset', 'reset', 'POST /api/auth/password/reset/request'],
  ])('renders on %s with its own action and sends the token', async (path, action, route) => {
    const { rendered } = fakeWidget()
    const api = fakeApi({ 'GET /api/auth/session': ON, [route]: reason(400, 'turnstile_failed') })
    renderAt(path)
    await waitFor(() => {
      expect(rendered).toHaveLength(1)
    })
    expect(rendered[0]?.action).toBe(action)
    type('Email', 'ann@example.test')
    if (path === '/signup') type('Password', 'correct horse battery')
    act(() => {
      rendered[0]?.solve('synthetic.token')
    })
    const form = document.querySelector('form')
    if (form === null) throw new Error('no form')
    fireEvent.submit(form)
    await waitFor(() => {
      expect(api.calls.filter((c) => `${c.method} ${c.path}` === route)).toHaveLength(1)
    })
    const [call] = api.calls.filter((c) => `${c.method} ${c.path}` === route)
    expect(call?.headers.get('X-Turnstile-Token')).toBe('synthetic.token')
  })
})

describe('loadTurnstile', () => {
  it('adds the script the server named, once, and fails when it does not load', async () => {
    const first = loadTurnstile('/synthetic/turnstile.js')
    expect(loadTurnstile('/synthetic/turnstile.js')).toBe(first)
    const scripts = document.querySelectorAll('script[src$="/synthetic/turnstile.js"]')
    expect(scripts).toHaveLength(1)
    scripts[0]?.dispatchEvent(new Event('error'))
    await expect(first).rejects.toThrow('turnstile unavailable')
    expect(document.querySelectorAll('script[src$="/synthetic/turnstile.js"]')).toHaveLength(0)
  })
})
