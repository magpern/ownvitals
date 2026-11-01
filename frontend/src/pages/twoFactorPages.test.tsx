import { act, fireEvent, screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { QR_SVG, SIGNED_IN, SIGNED_IN_NO_CODE, SIGNED_IN_NO_TOTP, fakeApi, json, reason } from '../test/fakeApi'
import { renderAt } from '../test/render'

// The second-factor pages (#11, Auth mock-up): forced enrolment with its recovery codes, and the
// code prompt after sign-in. Everything is synthetic.

const here = () => window.location.pathname + window.location.search

/** Types a whole code into the six boxes (the first box spreads it, as autofill does). */
function typeCode(code: string, group = '6-digit code') {
  const boxes = within(screen.getByRole('group', { name: group })).getAllByRole('textbox')
  fireEvent.change(boxes[0] as HTMLElement, { target: { value: code } })
}

const ENROLMENT = { secret: 'JBSWY3DPEHPK3PXP', otpauth_uri: 'otpauth://totp/x', qr_svg: QR_SVG, pending: 'sealed-pending' }
const RECOVERY = ['aaaa-1111', 'bbbb-2222', 'cccc-3333']

/** Routes for a user without TOTP who enrols; the session turns full after the confirm. */
function enrolRoutes() {
  let enrolled = false
  return {
    'GET /api/auth/session': () => (enrolled ? SIGNED_IN : SIGNED_IN_NO_TOTP),
    'POST /api/auth/mfa/totp/start': ENROLMENT,
    'POST /api/auth/mfa/totp/confirm': () => {
      enrolled = true
      return { recovery_codes: RECOVERY }
    },
  }
}

async function confirmEnrolment(code = '123456') {
  await screen.findByRole('img', { name: 'QR code for your authenticator app' })
  typeCode(code, 'Code from the app')
  fireEvent.click(screen.getByRole('button', { name: 'Confirm' }))
}

describe('forced enrolment', () => {
  it('starts on its own and draws the QR code inline, with the key in groups of four', async () => {
    const api = fakeApi(enrolRoutes())
    renderAt('/two-factor/setup')
    expect(await screen.findByRole('heading', { name: 'Set up your authenticator app' })).toBeInTheDocument()
    const qr = await screen.findByRole('img', { name: 'QR code for your authenticator app' })
    expect(qr.localName).toBe('svg')
    expect(qr.querySelector('img, image, script, style')).toBeNull()
    expect(screen.getByText('JBSW Y3DP EHPK 3PXP')).toBeInTheDocument()
    expect(api.to('POST', '/api/auth/mfa/totp/start')).toHaveLength(1)
    expect(screen.getByRole('button', { name: 'Confirm' })).toBeDisabled()
  })

  it('confirms with the first code and keeps the recovery codes up while the session is read', async () => {
    const api = fakeApi(enrolRoutes())
    renderAt('/two-factor/setup')
    await confirmEnrolment()
    const list = await screen.findByRole('list', { name: 'Recovery codes' })
    expect(within(list).getAllByRole('listitem').map((item) => item.textContent)).toEqual(RECOVERY)
    expect(api.to('POST', '/api/auth/mfa/totp/confirm')[0]?.body).toEqual({ pending: 'sealed-pending', code: '123456' })
    await waitFor(() => {
      expect(api.to('GET', '/api/auth/session').length).toBeGreaterThanOrEqual(2)
    })
    expect(here()).toBe('/two-factor/setup')
    expect(screen.getByRole('list', { name: 'Recovery codes' })).toBeInTheDocument()
  })

  it('goes on only after the user says the codes are saved', async () => {
    fakeApi(enrolRoutes())
    renderAt('/two-factor/setup')
    await confirmEnrolment()
    const go = await screen.findByRole('button', { name: 'Continue to Connect Withings' })
    expect(go).toBeDisabled()
    fireEvent.click(screen.getByRole('checkbox', { name: "I've saved them somewhere safe" }))
    fireEvent.click(go)
    expect(await screen.findByText('The connections page comes in M6.')).toBeInTheDocument()
    expect(here()).toBe('/connections')
    expect(screen.queryByText('aaaa-1111')).toBeNull()
  })

  it('goes on to the return address after the codes are saved', async () => {
    fakeApi(enrolRoutes())
    renderAt('/two-factor/setup?return_to=%2Fmetrics')
    await confirmEnrolment()
    fireEvent.click(await screen.findByRole('checkbox', { name: "I've saved them somewhere safe" }))
    fireEvent.click(screen.getByRole('button', { name: 'Continue to Connect Withings' }))
    await waitFor(() => {
      expect(here()).toBe('/metrics')
    })
  })

  it('shows the codes when the confirmation answers 401 with them, then sends the user to sign in', async () => {
    let gone = false
    fakeApi({
      ...enrolRoutes(),
      'GET /api/auth/session': () => (gone ? { authenticated: false, csrf_token: 'a' } : SIGNED_IN_NO_TOTP),
      'POST /api/auth/mfa/totp/confirm': () => {
        gone = true
        return json({ detail: { reason: 'not_authenticated' }, enrolled: true, recovery_codes: RECOVERY }, 401)
      },
    })
    renderAt('/two-factor/setup')
    await confirmEnrolment()
    const list = await screen.findByRole('list', { name: 'Recovery codes' })
    expect(within(list).getAllByRole('listitem')).toHaveLength(3)
    expect(screen.getByText(/you were signed out meanwhile/)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('checkbox', { name: "I've saved them somewhere safe" }))
    fireEvent.click(screen.getByRole('button', { name: 'Sign in again' }))
    expect(await screen.findByRole('heading', { name: 'Sign in' })).toBeInTheDocument()
    expect(here()).toBe('/signin')
  })

  it('keeps the codes up even when the session cannot be read again', async () => {
    let enrolled = false
    fakeApi({
      ...enrolRoutes(),
      'GET /api/auth/session': () => (enrolled ? reason(500, 'boom') : SIGNED_IN_NO_TOTP),
      'POST /api/auth/mfa/totp/confirm': () => {
        enrolled = true
        return { recovery_codes: RECOVERY }
      },
    })
    renderAt('/two-factor/setup')
    await confirmEnrolment()
    expect(await screen.findByRole('list', { name: 'Recovery codes' })).toBeInTheDocument()
    await act(async () => {
      await Promise.resolve()
    })
    expect(screen.getByRole('list', { name: 'Recovery codes' })).toBeInTheDocument()
  })

  it('shows a wrong code in plain words and keeps the form', async () => {
    fakeApi({ ...enrolRoutes(), 'POST /api/auth/mfa/totp/confirm': reason(400, 'invalid_code') })
    renderAt('/two-factor/setup')
    await confirmEnrolment('000000')
    expect(await screen.findByRole('alert')).toHaveTextContent('That code is not right')
    expect(screen.getByRole('group', { name: 'Code from the app' })).toBeInTheDocument()
    expect(screen.queryByRole('list', { name: 'Recovery codes' })).toBeNull()
  })

  it('shows an error when the enrolment cannot be started', async () => {
    fakeApi({ ...enrolRoutes(), 'POST /api/auth/mfa/totp/start': reason(403, 'reauth_required') })
    renderAt('/two-factor/setup')
    expect(await screen.findByRole('alert')).toHaveTextContent('This needs your password again.')
    expect(screen.getByRole('button', { name: 'Confirm' })).toBeDisabled()
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
    const qr = await screen.findByRole('img', { name: 'QR code for your authenticator app' })
    expect([...qr.querySelectorAll('*')].map((element) => element.localName)).toEqual(['path'])
    expect(qr.hasAttribute('onload')).toBe(false)
  })
})

describe('the code prompt after sign-in', () => {
  const routes = { 'GET /api/auth/session': SIGNED_IN_NO_CODE }

  function verifying() {
    let verified = false
    return {
      'GET /api/auth/session': () => (verified ? SIGNED_IN : SIGNED_IN_NO_CODE),
      'POST /api/auth/mfa/verify': () => {
        verified = true
        return { ok: true }
      },
    }
  }

  it('verifies the six digits and goes on to the home page', async () => {
    const api = fakeApi(verifying())
    renderAt('/two-factor')
    await screen.findByRole('heading', { name: 'Enter your code' })
    expect(screen.getByText('Step 2 of 2')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Verify' })).toBeDisabled()
    typeCode('123456')
    fireEvent.click(screen.getByRole('button', { name: 'Verify' }))
    expect(await screen.findByText('The dashboard comes in M6.')).toBeInTheDocument()
    expect(here()).toBe('/')
    expect(api.to('POST', '/api/auth/mfa/verify')[0]?.body).toEqual({ code: '123456' })
  })

  it('goes on to the return address', async () => {
    fakeApi(verifying())
    renderAt('/two-factor?return_to=%2Fmetrics')
    await screen.findByRole('heading', { name: 'Enter your code' })
    typeCode('123456')
    fireEvent.click(screen.getByRole('button', { name: 'Verify' }))
    await waitFor(() => {
      expect(here()).toBe('/metrics')
    })
  })

  it('takes a recovery code instead', async () => {
    const api = fakeApi({ ...routes, 'POST /api/auth/mfa/verify': { ok: true } })
    renderAt('/two-factor')
    await screen.findByRole('heading', { name: 'Enter your code' })
    fireEvent.click(screen.getByRole('button', { name: 'Use a recovery code instead' }))
    fireEvent.change(screen.getByLabelText('Recovery code'), { target: { value: ' aaaa-1111 ' } })
    fireEvent.click(screen.getByRole('button', { name: 'Verify' }))
    await waitFor(() => {
      expect(api.to('POST', '/api/auth/mfa/verify')[0]?.body).toEqual({ code: 'aaaa-1111' })
    })
  })

  it('shows a wrong code in plain words and stays on the prompt', async () => {
    fakeApi({ ...routes, 'POST /api/auth/mfa/verify': reason(400, 'invalid_code') })
    renderAt('/two-factor')
    await screen.findByRole('heading', { name: 'Enter your code' })
    typeCode('000000')
    fireEvent.click(screen.getByRole('button', { name: 'Verify' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('That code is not right')
    expect(here()).toBe('/two-factor')
  })

  it('tells the user to wait when the code attempts are rate limited', async () => {
    const limited = json({ detail: { reason: 'rate_limited' } }, 429, { 'Retry-After': '300' })
    fakeApi({ ...routes, 'POST /api/auth/mfa/verify': limited })
    renderAt('/two-factor')
    await screen.findByRole('heading', { name: 'Enter your code' })
    typeCode('000000')
    fireEvent.click(screen.getByRole('button', { name: 'Verify' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Try again in 5 minutes')
  })

  it('says who is signed in, and lets someone else sign out', async () => {
    let out = false
    fakeApi({
      'GET /api/auth/session': () => (out ? { authenticated: false, csrf_token: 'a' } : SIGNED_IN_NO_CODE),
      'POST /api/auth/logout': () => {
        out = true
        return new Response(null, { status: 204 })
      },
    })
    renderAt('/two-factor')
    expect(await screen.findByText(/Signed in as ann@example\.test\. This step expires in 10 minutes\./)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Not you? Sign out' }))
    expect(await screen.findByRole('heading', { name: 'Sign in' })).toBeInTheDocument()
  })
})
