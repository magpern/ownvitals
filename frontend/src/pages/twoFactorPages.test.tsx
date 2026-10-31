import { act, fireEvent, screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import {
  MFA_OFF,
  MFA_ON,
  QR_SVG,
  SIGNED_IN,
  SIGNED_IN_NO_CODE,
  fakeApi,
  reason,
} from '../test/fakeApi'
import { renderAt } from '../test/render'

const here = () => window.location.pathname + window.location.search

function type(label: RegExp | string, value: string) {
  fireEvent.change(screen.getByLabelText(label), { target: { value } })
}

const ENROLMENT = { secret: 'JBSWY3DPEHPK3PXP', otpauth_uri: 'otpauth://totp/x', qr_svg: QR_SVG, pending: 'sealed-pending' }
const RECOVERY = ['aaaa-1111', 'bbbb-2222', 'cccc-3333']

/** Routes for a user without TOTP who enrols; the session turns to "through" after the confirm. */
function enrolRoutes() {
  let enrolled = false
  return {
    'GET /api/auth/session': () => (enrolled ? SIGNED_IN : SIGNED_IN_NO_CODE),
    'GET /api/auth/mfa': () => (enrolled ? MFA_ON : MFA_OFF),
    'POST /api/auth/mfa/totp/start': ENROLMENT,
    'POST /api/auth/mfa/totp/confirm': () => {
      enrolled = true
      return { recovery_codes: RECOVERY }
    },
  }
}

describe('TOTP enrolment', () => {
  it('draws the QR code inline from the server SVG, with the key for typing by hand', async () => {
    fakeApi(enrolRoutes())
    renderAt('/two-factor/setup')
    fireEvent.click(await screen.findByRole('button', { name: 'Set up an authenticator app' }))

    const qr = await screen.findByRole('img', { name: 'QR code for your authenticator app' })
    expect(qr.localName).toBe('svg')
    expect(qr.querySelectorAll('path')).toHaveLength(2)
    expect(qr.querySelector('img, image, script, style')).toBeNull()
    expect(document.querySelector('img')).toBeNull()
    expect(screen.getByText('JBSWY3DPEHPK3PXP')).toBeInTheDocument()
  })

  it('confirms with the first code and shows the recovery codes once', async () => {
    const api = fakeApi(enrolRoutes())
    renderAt('/two-factor/setup')
    fireEvent.click(await screen.findByRole('button', { name: 'Set up an authenticator app' }))
    await screen.findByRole('img', { name: 'QR code for your authenticator app' })
    type('Code from the app', ' 123456 ')
    fireEvent.click(screen.getByRole('button', { name: 'Turn on two-factor' }))

    const list = await screen.findByRole('list', { name: 'Recovery codes' })
    expect(within(list).getAllByRole('listitem').map((item) => item.textContent)).toEqual(RECOVERY)
    expect(screen.getByText(/will not be shown again/)).toBeInTheDocument()
    expect(api.to('POST', '/api/auth/mfa/totp/confirm')[0]?.body).toEqual({
      pending: 'sealed-pending',
      code: '123456',
    })
    // The session is read again behind the codes, which stay up: the guard must not whisk them away.
    await waitFor(() => {
      expect(api.to('GET', '/api/auth/session').length).toBeGreaterThanOrEqual(2)
    })
    expect(here()).toBe('/two-factor/setup')
    expect(screen.getByRole('list', { name: 'Recovery codes' })).toBeInTheDocument()
  })

  it('goes on to the home page only after the user says the codes are saved', async () => {
    fakeApi(enrolRoutes())
    renderAt('/two-factor/setup')
    fireEvent.click(await screen.findByRole('button', { name: 'Set up an authenticator app' }))
    await screen.findByRole('img', { name: 'QR code for your authenticator app' })
    type('Code from the app', '123456')
    fireEvent.click(screen.getByRole('button', { name: 'Turn on two-factor' }))
    fireEvent.click(await screen.findByRole('button', { name: 'I have saved them' }))

    expect(await screen.findByText('The dashboard comes in M6.')).toBeInTheDocument()
    expect(here()).toBe('/')
    expect(screen.queryByRole('list', { name: 'Recovery codes' })).toBeNull()
    expect(screen.queryByText('aaaa-1111')).toBeNull()
  })

  it('goes on to the return address after the codes are saved', async () => {
    fakeApi(enrolRoutes())
    renderAt('/two-factor/setup?return_to=%2Fmetrics')
    fireEvent.click(await screen.findByRole('button', { name: 'Set up an authenticator app' }))
    await screen.findByRole('img', { name: 'QR code for your authenticator app' })
    type('Code from the app', '123456')
    fireEvent.click(screen.getByRole('button', { name: 'Turn on two-factor' }))
    fireEvent.click(await screen.findByRole('button', { name: 'I have saved them' }))
    await waitFor(() => {
      expect(here()).toBe('/metrics')
    })
  })

  it('keeps the codes up even when the session cannot be read again', async () => {
    let enrolled = false
    const api = fakeApi({
      ...enrolRoutes(),
      'GET /api/auth/session': () => (enrolled ? reason(500, 'boom') : SIGNED_IN_NO_CODE),
      'POST /api/auth/mfa/totp/confirm': () => {
        enrolled = true
        return { recovery_codes: RECOVERY }
      },
    })
    renderAt('/two-factor/setup')
    fireEvent.click(await screen.findByRole('button', { name: 'Set up an authenticator app' }))
    await screen.findByRole('img', { name: 'QR code for your authenticator app' })
    type('Code from the app', '123456')
    fireEvent.click(screen.getByRole('button', { name: 'Turn on two-factor' }))
    expect(await screen.findByRole('list', { name: 'Recovery codes' })).toBeInTheDocument()
    await waitFor(() => {
      expect(api.to('GET', '/api/auth/session').length).toBeGreaterThanOrEqual(2)
    })
    await act(async () => {
      await Promise.resolve()
    })
    expect(screen.getByRole('list', { name: 'Recovery codes' })).toBeInTheDocument()
  })

  it('shows a wrong code in plain words and keeps the form', async () => {
    fakeApi({ ...enrolRoutes(), 'POST /api/auth/mfa/totp/confirm': reason(400, 'invalid_code') })
    renderAt('/two-factor/setup')
    fireEvent.click(await screen.findByRole('button', { name: 'Set up an authenticator app' }))
    await screen.findByRole('img', { name: 'QR code for your authenticator app' })
    type('Code from the app', '000000')
    fireEvent.click(screen.getByRole('button', { name: 'Turn on two-factor' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('That code is not right')
    expect(screen.getByLabelText('Code from the app')).toBeInTheDocument()
    expect(screen.queryByRole('list', { name: 'Recovery codes' })).toBeNull()
  })

  it('shows an error when the enrolment cannot be started', async () => {
    fakeApi({ ...enrolRoutes(), 'POST /api/auth/mfa/totp/start': reason(403, 'reauth_required') })
    renderAt('/two-factor/setup')
    fireEvent.click(await screen.findByRole('button', { name: 'Set up an authenticator app' }))
    expect(await screen.findByRole('alert')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Set up an authenticator app' })).toBeInTheDocument()
  })

  it('does not draw anything from a server SVG that carries script', async () => {
    fakeApi({
      ...enrolRoutes(),
      'POST /api/auth/mfa/totp/start': {
        ...ENROLMENT,
        qr_svg:
          '<svg xmlns="http://www.w3.org/2000/svg" width="8" height="8" onload="x()"><script>x()</script><image href="https://evil.example/x.png"/><path d="M0 0h1v1z" onclick="x()"/></svg>',
      },
    })
    renderAt('/two-factor/setup')
    fireEvent.click(await screen.findByRole('button', { name: 'Set up an authenticator app' }))
    const qr = await screen.findByRole('img', { name: 'QR code for your authenticator app' })
    expect([...qr.querySelectorAll('*')].map((element) => element.localName)).toEqual(['path'])
    expect(qr.hasAttribute('onload')).toBe(false)
    expect(qr.querySelector('path')?.hasAttribute('onclick')).toBe(false)
  })
})

describe('TOTP challenge', () => {
  const routes = { 'GET /api/auth/session': SIGNED_IN_NO_CODE, 'GET /api/auth/mfa': MFA_ON }

  it('verifies the code and goes on to the home page', async () => {
    let verified = false
    const api = fakeApi({
      ...routes,
      'GET /api/auth/session': () => (verified ? SIGNED_IN : SIGNED_IN_NO_CODE),
      'POST /api/auth/mfa/verify': () => {
        verified = true
        return { ok: true }
      },
    })
    renderAt('/two-factor')
    await screen.findByRole('heading', { name: 'Enter your code' })
    type('Authenticator or recovery code', ' 123456 ')
    fireEvent.click(screen.getByRole('button', { name: 'Verify' }))

    expect(await screen.findByText('The dashboard comes in M6.')).toBeInTheDocument()
    expect(here()).toBe('/')
    expect(api.to('POST', '/api/auth/mfa/verify')[0]?.body).toEqual({ code: '123456' })
  })

  it('goes on to the return address', async () => {
    let verified = false
    fakeApi({
      ...routes,
      'GET /api/auth/session': () => (verified ? SIGNED_IN : SIGNED_IN_NO_CODE),
      'POST /api/auth/mfa/verify': () => {
        verified = true
        return { ok: true }
      },
    })
    renderAt('/two-factor?return_to=%2Fmetrics')
    await screen.findByRole('heading', { name: 'Enter your code' })
    type('Authenticator or recovery code', '123456')
    fireEvent.click(screen.getByRole('button', { name: 'Verify' }))
    await waitFor(() => {
      expect(here()).toBe('/metrics')
    })
  })

  it('accepts a recovery code in the same field', async () => {
    const api = fakeApi({ ...routes, 'POST /api/auth/mfa/verify': { ok: true } })
    renderAt('/two-factor')
    await screen.findByRole('heading', { name: 'Enter your code' })
    type('Authenticator or recovery code', 'aaaa-1111')
    fireEvent.click(screen.getByRole('button', { name: 'Verify' }))
    await waitFor(() => {
      expect(api.to('POST', '/api/auth/mfa/verify')).toHaveLength(1)
    })
    expect(api.to('POST', '/api/auth/mfa/verify')[0]?.body).toEqual({ code: 'aaaa-1111' })
  })

  it('shows a wrong code in plain words and stays on the prompt', async () => {
    fakeApi({ ...routes, 'POST /api/auth/mfa/verify': reason(400, 'invalid_code') })
    renderAt('/two-factor')
    await screen.findByRole('heading', { name: 'Enter your code' })
    type('Authenticator or recovery code', '000000')
    fireEvent.click(screen.getByRole('button', { name: 'Verify' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('That code is not right')
    expect(here()).toBe('/two-factor')
  })

  it('tells the user to wait when the code attempts are rate limited', async () => {
    const limited = new Response(JSON.stringify({ detail: { reason: 'rate_limited' } }), {
      status: 429,
      headers: { 'Content-Type': 'application/json', 'Retry-After': '300' },
    })
    fakeApi({ ...routes, 'POST /api/auth/mfa/verify': limited })
    renderAt('/two-factor')
    await screen.findByRole('heading', { name: 'Enter your code' })
    type('Authenticator or recovery code', '000000')
    fireEvent.click(screen.getByRole('button', { name: 'Verify' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Try again in 5 minutes')
  })
})
