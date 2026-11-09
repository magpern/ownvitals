import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'
import { SIGNED_IN, fakeApi, reason } from '../../test/fakeApi'
import { fakeProfile } from '../../test/profileFixtures'
import { renderAt } from '../../test/render'
import { getThemePreference, setThemePreference } from '../../theme/theme'

// Settings (issue 203): Profile and display, Notifications, Your data. Everything is synthetic: a
// made-up profile, no network, no account.

afterEach(() => {
  setThemePreference('system')
  window.localStorage.clear()
})

/** `GET /api/profile` answers the current copy; `PUT /api/profile` validates nothing and echoes. */
function profileApi(start = fakeProfile(), extra: Record<string, unknown> = {}) {
  let stored = start
  const api = fakeApi({
    'GET /api/auth/session': SIGNED_IN,
    'GET /api/profile': () => stored,
    'PUT /api/profile': (body: unknown) => {
      stored = { ...stored, ...(body as object) }
      return stored
    },
    ...extra,
  })
  return api
}

const lastPut = (api: ReturnType<typeof fakeApi>) => api.to('PUT', '/api/profile').at(-1)?.body as Record<string, unknown>

describe('Profile and display', () => {
  it('shows the four tabs and the cards of the mock-up with the stored values', async () => {
    profileApi(fakeProfile({ birth_year: 1982, sex: 'female' }))
    renderAt('/settings/profile')
    await screen.findByRole('heading', { name: 'Used in your analysis' })
    const tabs = screen.getByRole('navigation', { name: 'Settings' })
    expect(within(tabs).getAllByRole('link').map((a) => a.textContent)).toEqual([
      'Profile and display',
      'Sign-in and security',
      'Notifications',
      'Your data',
    ])
    expect(within(tabs).getByRole('link', { name: 'Profile and display' })).toHaveAttribute('aria-current', 'page')
    expect(screen.getByRole('heading', { name: 'Display' })).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'Overview' })).toBeInTheDocument()
    expect(screen.getByLabelText('Birth year')).toHaveValue('1982')
    expect(screen.getByLabelText('Sex for score bands')).toHaveValue('female')
    expect(screen.getByRole('button', { name: 'kg' })).toHaveAttribute('aria-pressed', 'true')
    expect(screen.getByRole('button', { name: '90 d' })).toHaveAttribute('aria-pressed', 'true')
    // The language row lives in the Display card.
    expect(screen.getByRole('combobox', { name: 'Language' })).toBeInTheDocument()
  })

  it('is the screen of /settings too', async () => {
    profileApi()
    renderAt('/settings')
    expect(await screen.findByRole('heading', { name: 'Used in your analysis' })).toBeInTheDocument()
  })

  it('saves the whole profile on a change, without the read-only states', async () => {
    const api = profileApi()
    renderAt('/settings/profile')
    fireEvent.change(await screen.findByLabelText('Sex for score bands'), { target: { value: 'male' } })
    await screen.findByText('Saved.')
    const body = lastPut(api)
    expect(body.sex).toBe('male')
    expect(body.birth_year).toBe(1982)
    expect(body).not.toHaveProperty('experimental_available')
    expect(body).not.toHaveProperty('ai_available')
    expect(api.to('PUT', '/api/profile')[0]?.headers.get('X-CSRF-Token')).toBeTruthy()
  })

  it('refuses a birth year outside 1900 to this year before asking the server', async () => {
    const api = profileApi()
    renderAt('/settings/profile')
    const field = await screen.findByLabelText('Birth year')
    fireEvent.change(field, { target: { value: '1850' } })
    fireEvent.blur(field)
    expect(await screen.findByRole('alert')).toHaveTextContent('Enter a year from 1900')
    expect(api.to('PUT', '/api/profile')).toHaveLength(0)
    fireEvent.change(field, { target: { value: '1990' } })
    fireEvent.blur(field)
    await waitFor(() => {
      expect(lastPut(api).birth_year).toBe(1990)
    })
  })

  it('clears the birth year with an empty field', async () => {
    const api = profileApi()
    renderAt('/settings/profile')
    const field = await screen.findByLabelText('Birth year')
    fireEvent.change(field, { target: { value: '' } })
    fireEvent.blur(field)
    await waitFor(() => {
      expect(lastPut(api).birth_year).toBeNull()
    })
  })

  it('keeps one time zone for the day when asked, and goes back to each reading own', async () => {
    const api = profileApi()
    renderAt('/settings/profile')
    const select = await screen.findByLabelText('Which day a reading belongs to')
    fireEvent.change(select, { target: { value: 'fixed_tz' } })
    await waitFor(() => {
      expect(lastPut(api).day_assignment).toBe('fixed_tz')
    })
    expect(typeof lastPut(api).day_tz).toBe('string')
    await screen.findByText('Saved.')
    fireEvent.change(select, { target: { value: 'reading_tz' } })
    await waitFor(() => {
      expect(lastPut(api)).toMatchObject({ day_assignment: 'reading_tz', day_tz: null })
    })
  })

  it('applies the theme at once on this device and saves it for the account', async () => {
    const api = profileApi()
    renderAt('/settings/profile')
    fireEvent.click(await screen.findByRole('button', { name: 'Dark' }))
    expect(document.documentElement.getAttribute('data-theme')).toBe('dark')
    await waitFor(() => {
      expect(lastPut(api).theme).toBe('dark')
    })
    expect(screen.getByRole('button', { name: 'Dark' })).toHaveAttribute('aria-pressed', 'true')
  })

  it("follows the account's theme on a page outside Settings, on a device that has none stored", async () => {
    profileApi(fakeProfile({ theme: 'light' }))
    renderAt('/')
    await screen.findByRole('heading', { level: 1, name: 'Overview' })
    await waitFor(() => {
      expect(getThemePreference()).toBe('light')
    })
    expect(document.documentElement.getAttribute('data-theme')).toBe('light')
  })

  it('saves the units, the date format and the default range', async () => {
    const api = profileApi()
    renderAt('/settings/profile')
    fireEvent.click(await screen.findByRole('button', { name: 'lb' }))
    await waitFor(() => {
      expect(lastPut(api).weight_unit).toBe('lb')
    })
    fireEvent.click(await screen.findByRole('button', { name: '°F' }))
    await waitFor(() => {
      expect(lastPut(api).temperature_unit).toBe('f')
    })
    fireEvent.change(screen.getByLabelText('Dates and times'), { target: { value: 'iso' } })
    await waitFor(() => {
      expect(lastPut(api).datetime_format).toBe('iso')
    })
    fireEvent.click(screen.getByRole('button', { name: '1 y' }))
    await waitFor(() => {
      expect(lastPut(api).chart_range).toBe('1y')
    })
  })

  it('reorders and hides the areas of the overview', async () => {
    const api = profileApi()
    renderAt('/settings/profile')
    fireEvent.click(await screen.findByRole('button', { name: 'Move heart up' }))
    await waitFor(() => {
      expect(lastPut(api).overview_order).toEqual(['heart', 'body', 'nerve', 'activity'])
    })
    expect(screen.getByRole('button', { name: 'Move heart up' })).toBeDisabled()
    fireEvent.click(await screen.findByRole('switch', { name: 'Show nerve health' }))
    await waitFor(() => {
      expect(lastPut(api).overview_order).toEqual(['heart', 'body', 'activity'])
    })
    fireEvent.click(await screen.findByRole('switch', { name: 'Show nerve health' }))
    await waitFor(() => {
      expect(lastPut(api).overview_order).toEqual(['heart', 'body', 'activity', 'nerve'])
    })
    fireEvent.click(await screen.findByRole('switch', { name: "Show metrics my devices don't support" }))
    await waitFor(() => {
      expect(lastPut(api).show_unsupported).toBe(false)
    })
  })

  it("shows the server's refusal and keeps the stored value", async () => {
    profileApi(fakeProfile(), { 'PUT /api/profile': reason(400, 'birth_year_in_the_future') })
    renderAt('/settings/profile')
    fireEvent.click(await screen.findByRole('button', { name: 'lb' }))
    expect(await screen.findByRole('alert')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'kg' })).toHaveAttribute('aria-pressed', 'true')
  })

  it('says so when the settings cannot be read', async () => {
    fakeApi({ 'GET /api/auth/session': SIGNED_IN, 'GET /api/profile': reason(500, 'failed') })
    renderAt('/settings/profile')
    expect(await screen.findByRole('alert')).toBeInTheDocument()
  })
})

describe('Notifications', () => {
  it('says what the email is, and saves the choices', async () => {
    const api = profileApi()
    renderAt('/settings/notifications')
    await screen.findByRole('heading', { name: 'Notifications' })
    expect(screen.getByText(/The email only says something is waiting, with a link; never values/)).toBeInTheDocument()
    expect(screen.getByRole('switch', { name: 'In-app notifications, always on' })).toBeDisabled()
    expect(screen.getByRole('switch', { name: 'In-app notifications, always on' })).toBeChecked()
    fireEvent.click(screen.getByRole('switch', { name: 'Email when something needs a look' }))
    await waitFor(() => {
      expect(lastPut(api).notify_email).toBe(false)
    })
    fireEvent.click(screen.getByRole('switch', { name: 'Weekly reminder while findings are unseen' }))
    await waitFor(() => {
      expect(lastPut(api).notify_weekly).toBe(true)
    })
    fireEvent.click(screen.getByRole('button', { name: 'Trend' }))
    await waitFor(() => {
      expect(lastPut(api).notify_tiers).toEqual(['device', 'system'])
    })
  })

  it('offers the experimental chip only to an account that may use it', async () => {
    profileApi()
    renderAt('/settings/notifications')
    expect(await screen.findByRole('button', { name: 'Experimental' })).toBeDisabled()
  })
})

describe('Your data', () => {
  it('shows the experimental and AI states read-only', async () => {
    const api = profileApi(fakeProfile({ experimental_available: true, show_experimental: false }))
    renderAt('/settings/data')
    const experimental = await screen.findByRole('switch', { name: 'Show experimental results' })
    expect(experimental).toBeChecked()
    expect(experimental).toBeDisabled()
    expect(screen.getByRole('switch', { name: 'AI insights' })).toBeDisabled()
    expect(screen.getByRole('switch', { name: 'AI insights' })).not.toBeChecked()
    expect(api.to('PUT', '/api/profile')).toHaveLength(0)
  })

  it('shows the experimental switch off when the account may not use it', async () => {
    profileApi(fakeProfile({ experimental_available: false, show_experimental: true }))
    renderAt('/settings/data')
    expect(await screen.findByRole('switch', { name: 'Show experimental results' })).not.toBeChecked()
  })

  it('shows the export as not there yet', async () => {
    profileApi()
    renderAt('/settings/data')
    const button = await screen.findByRole('button', { name: 'Export…' })
    expect(button).toBeDisabled()
    expect(button).toHaveAttribute('data-lock')
  })

  /** Deleting is refused until a recent password and a step-up code are given. */
  function deleteRoutes() {
    let code = false
    let gone = false
    return {
      'GET /api/auth/session': () => (gone ? { authenticated: false, csrf_token: 'a' } : SIGNED_IN),
      'DELETE /api/auth/account': () => {
        if (!code) return reason(403, 'step_up_required')
        gone = true
        return { revoke_failed: false, removal: null }
      },
      'POST /api/auth/mfa/stepup': () => {
        code = true
        return { ok: true }
      },
    }
  }

  it('asks to confirm, then runs the delete behind the shared step-up dialog', async () => {
    const api = profileApi(fakeProfile(), deleteRoutes())
    renderAt('/settings/data')
    const open = await screen.findByRole('button', { name: 'Delete account…' })
    expect(open).toHaveAttribute('data-lock')
    fireEvent.click(open)
    expect(screen.getByRole('alert')).toHaveTextContent('cannot be undone')
    expect(api.to('DELETE', '/api/auth/account')).toHaveLength(0)
    fireEvent.click(screen.getByRole('button', { name: 'Delete my account' }))
    const dialog = await screen.findByRole('dialog', { name: "Confirm it's you" })
    expect(within(dialog).getByText(/Deleting your account needs a fresh code/)).toBeInTheDocument()
    const boxes = within(within(dialog).getByRole('group', { name: '6-digit code' })).getAllByRole('textbox')
    fireEvent.change(boxes[0] as HTMLElement, { target: { value: '123456' } })
    fireEvent.click(within(dialog).getByRole('button', { name: 'Confirm' }))
    expect(await screen.findByRole('heading', { name: 'Sign in' })).toBeInTheDocument()
    expect(window.location.pathname).toBe('/signin')
    expect(api.to('DELETE', '/api/auth/account')).toHaveLength(2)
  })

  it('does nothing when the confirmation is cancelled', async () => {
    const api = profileApi(fakeProfile(), deleteRoutes())
    renderAt('/settings/data')
    fireEvent.click(await screen.findByRole('button', { name: 'Delete account…' }))
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(screen.getByRole('button', { name: 'Delete account…' })).toBeInTheDocument()
    expect(api.to('DELETE', '/api/auth/account')).toHaveLength(0)
  })

  it('tells the person when Withings could not be reached to revoke access', async () => {
    profileApi(fakeProfile(), {
      ...deleteRoutes(),
      'DELETE /api/auth/account': { revoke_failed: true, removal: 'x' },
    })
    renderAt('/settings/data')
    fireEvent.click(await screen.findByRole('button', { name: 'Delete account…' }))
    fireEvent.click(screen.getByRole('button', { name: 'Delete my account' }))
    expect(await screen.findByText(/couldn't be reached to revoke access/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Continue' })).toBeInTheDocument()
  })
})
