import { screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'
import { setLocale } from '../../i18n'
import { fakeApi, reason } from '../../test/fakeApi'
import { renderAt } from '../../test/render'
import { microFromFlag, microFromUsd, usdDigits } from './capAmount'

// The AI monthly caps on the Flags card: shown and typed in USD, stored in micro-USD, never above
// the server setting, each change behind the shared step-up dialog. Synthetic amounts only
// (synthetic: true).

const ADMIN = {
  authenticated: true,
  csrf_token: 'csrf-session',
  user: { id: 'u-admin', email: 'alex@example.com', role: 'admin' },
  session: {
    id: 's1', expires_at: '2030-01-01T00:00:00Z', recent_auth: true, mfa: true, stepup: true,
    totp_enabled: true, totp_required: true, second_factor: null, admin_pending_mfa: false,
  },
}
const USER = 'ai_user_monthly_cap_microusd'
const GLOBAL = 'ai_global_monthly_cap_microusd'

function cap(key: string, effective: number, environment: number, stored: number | null = null) {
  return {
    key, source: effective === environment ? 'environment' : 'database', effective, environment, stored,
    stored_valid: true, updated_at: null, updated_by: null,
  }
}

describe('the cap amounts', () => {
  it('reads dollars with at most two decimals as micro-USD', () => {
    expect(microFromUsd('10')).toBe(10_000_000)
    expect(microFromUsd(' 7.5 ')).toBe(7_500_000)
    expect(microFromUsd('2,25')).toBe(2_250_000)
    expect(microFromUsd('0')).toBe(0)
    for (const bad of ['', '-1', '1.234', '1e3', 'ten', '1.', '.5', '1 000']) expect(microFromUsd(bad)).toBeNull()
    expect(microFromFlag('100000000')).toBe(100_000_000)
    expect(microFromFlag('-1')).toBeNull()
    expect(microFromFlag(null)).toBeNull()
    expect(usdDigits(10_000_000)).toBe(2)
    expect(usdDigits(1)).toBe(6)
  })
})

describe('the cap amounts in exact integers', () => {
  // Binary floats are off for several of these: 0.07 * 100 = 7.000000000000001 and
  // 2.01 * 1e6 = 2009999.9999999998, so a float rewrite that does not round yields a fraction.
  it('gives the exact micro-USD for amounts that binary floats cannot hold', () => {
    expect(microFromUsd('0.07')).toBe(70_000)
    expect(microFromUsd('0.29')).toBe(290_000)
    expect(microFromUsd('1.10')).toBe(1_100_000)
    expect(microFromUsd('19.99')).toBe(19_990_000)
    expect(microFromUsd('19,99')).toBe(19_990_000)
    expect(microFromUsd('2.01')).toBe(2_010_000)
    expect(microFromUsd('2.03')).toBe(2_030_000)
  })
})

describe('the AI monthly caps', () => {
  it('shows each cap in USD and stores a lower one in micro-USD', async () => {
    const api = fakeApi({
      'GET /api/auth/session': ADMIN,
      'GET /api/admin/settings': { flags: [cap(USER, 10_000_000, 10_000_000), cap(GLOBAL, 100_000_000, 100_000_000)] },
      [`PUT /api/admin/settings/${USER}`]: cap(USER, 7_500_000, 10_000_000, 7_500_000),
    })
    renderAt('/admin?tab=flags')
    const row = (await screen.findByText('AI budget per account, monthly')).closest('.ov-setting') as HTMLElement
    expect(row).toHaveTextContent('10.00 USD.')
    expect(screen.getByText('AI budget in total, monthly').closest('.ov-setting')).toHaveTextContent('100.00 USD.')
    await userEvent.click(within(row).getByRole('button', { name: 'Change…' }))
    const dialog = screen.getByRole('dialog', { name: 'Change AI budget per account, monthly' })
    expect(dialog).toHaveTextContent('From 0 up to the server setting, 10.00 USD.')
    expect(within(dialog).queryByRole('button', { name: 'Use the server setting' })).toBeNull()
    const input = within(dialog).getByLabelText('Monthly cap in USD')
    const save = within(dialog).getByRole('button', { name: 'Save' })
    await userEvent.type(input, '10.01')
    expect(save).toBeDisabled() // above the server setting
    expect(input).toHaveAttribute('aria-invalid', 'true')
    await userEvent.clear(input)
    await userEvent.type(input, '10')
    expect(save).toBeDisabled() // what is in force already
    await userEvent.clear(input)
    await userEvent.type(input, '7.50')
    await userEvent.click(save)
    expect(api.to('PUT', `/api/admin/settings/${USER}`).map((call) => call.body)).toEqual([{ value: 7_500_000 }])
    await expect.poll(() => api.to('GET', '/api/admin/settings').length).toBe(2)
    expect(screen.queryByRole('dialog')).toBeNull()
  })

  it('says why the server refused a cap, and clears a stored one', async () => {
    const api = fakeApi({
      'GET /api/auth/session': ADMIN,
      'GET /api/admin/settings': {
        flags: [cap(USER, 4_000_000, 10_000_000, 4_000_000), cap(GLOBAL, 5_000_000, 100_000_000, 5_000_000)],
      },
      [`PUT /api/admin/settings/${GLOBAL}`]: reason(422, 'user_above_global'),
      [`DELETE /api/admin/settings/${USER}`]: cap(USER, 5_000_000, 10_000_000),
    })
    renderAt('/admin?tab=flags')
    const total = (await screen.findByText('AI budget in total, monthly')).closest('.ov-setting') as HTMLElement
    expect(total).toHaveTextContent('Stored value in use.')
    await userEvent.click(within(total).getByRole('button', { name: 'Change…' }))
    let dialog = screen.getByRole('dialog', { name: 'Change AI budget in total, monthly' })
    await userEvent.type(within(dialog).getByLabelText('Monthly cap in USD'), '3')
    await userEvent.click(within(dialog).getByRole('button', { name: 'Save' }))
    expect(await within(dialog).findByRole('alert')).toHaveTextContent(
      'The cap per account must stay at or below the total. Lower the cap per account first.',
    )
    expect(api.to('PUT', `/api/admin/settings/${GLOBAL}`).map((call) => call.body)).toEqual([{ value: 3_000_000 }])
    await userEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }))

    const perAccount = screen.getByText('AI budget per account, monthly').closest('.ov-setting') as HTMLElement
    await userEvent.click(within(perAccount).getByRole('button', { name: 'Change…' }))
    dialog = screen.getByRole('dialog', { name: 'Change AI budget per account, monthly' })
    await userEvent.click(within(dialog).getByRole('button', { name: 'Use the server setting' }))
    expect(api.to('DELETE', `/api/admin/settings/${USER}`)).toHaveLength(1)
  })

  it('says a server setting of 0 allows no request and offers nothing above it', async () => {
    fakeApi({
      'GET /api/auth/session': ADMIN,
      'GET /api/admin/settings': { flags: [cap(USER, 0, 0)] },
    })
    renderAt('/admin?tab=flags')
    const row = (await screen.findByText('AI budget per account, monthly')).closest('.ov-setting') as HTMLElement
    expect(row).toHaveTextContent("The server setting is 0, so no AI request can be made, and the cap can't be raised here.")
    await userEvent.click(within(row).getByRole('button', { name: 'Change…' }))
    const dialog = screen.getByRole('dialog', { name: 'Change AI budget per account, monthly' })
    await userEvent.type(within(dialog).getByLabelText('Monthly cap in USD'), '1')
    expect(within(dialog).getByRole('button', { name: 'Save' })).toBeDisabled()
  })

  it('asks for a fresh code first when the server wants one', async () => {
    fakeApi({
      'GET /api/auth/session': ADMIN,
      'GET /api/admin/settings': { flags: [cap(USER, 10_000_000, 10_000_000)] },
      [`PUT /api/admin/settings/${USER}`]: reason(403, 'step_up_required'),
    })
    renderAt('/admin?tab=flags')
    const row = (await screen.findByText('AI budget per account, monthly')).closest('.ov-setting') as HTMLElement
    await userEvent.click(within(row).getByRole('button', { name: 'Change…' }))
    const dialog = screen.getByRole('dialog', { name: 'Change AI budget per account, monthly' })
    await userEvent.type(within(dialog).getByLabelText('Monthly cap in USD'), '5')
    await userEvent.click(within(dialog).getByRole('button', { name: 'Save' }))
    expect(await screen.findByRole('dialog', { name: "Confirm it's you" })).toHaveTextContent('Changing a flag')
  })

  it('shows the amounts in Swedish', async () => {
    fakeApi({
      'GET /api/auth/session': ADMIN,
      'GET /api/admin/settings': { flags: [cap(GLOBAL, 100_000_000, 100_000_000)] },
    })
    await setLocale('sv')
    try {
      renderAt('/admin?tab=flags')
      const row = (await screen.findByText('AI-budget totalt, per månad')).closest('.ov-setting') as HTMLElement
      expect(row).toHaveTextContent('100,00 USD.')
    } finally {
      await setLocale('en')
    }
  })
})
