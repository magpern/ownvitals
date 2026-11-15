import { fireEvent, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { SIGNED_IN, fakeApi, reason } from '../../test/fakeApi'
import { fakeProfile } from '../../test/profileFixtures'
import { renderAt } from '../../test/render'

// The weekly digest on the assistant page (issue 209): the opt-in (default off, refused while AI
// is not available), the digests as text only with the AI label, the skipped and failed weeks,
// and no digest card without consent. Synthetic: made-up dates and texts; no network, no
// account, no health data, no model.

const SHOWN = { version: 1, locale: 'en', text: 'Synthetic consent text.', sha256: 'a'.repeat(64) }
const LABEL = 'AI-generated from your data. It can be wrong, and it is not medical advice.'

function consent(state: 'none' | 'given') {
  return {
    enabled: true,
    consent: { state, version: state === 'none' ? null : 1, given_at: state === 'none' ? null : '2026-11-09T10:00:00Z' },
    consent_text: SHOWN,
  }
}

function digest(
  week_start: string,
  status: string,
  content: string | null = null,
  created_at = '2026-11-10T03:00:00Z',
) {
  return { week_start, status, content, created_at }
}

interface State {
  available: boolean
  enabled: boolean
  digests: ReturnType<typeof digest>[]
}

function digestApi(start: Partial<State> = {}, given = true, extra: Record<string, unknown> = {}) {
  const state: State = { available: true, enabled: false, digests: [], ...start }
  const api = fakeApi({
    'GET /api/auth/session': SIGNED_IN,
    'GET /api/profile': fakeProfile({ ai_available: true }),
    'GET /api/ai/consent?locale=en': consent(given ? 'given' : 'none'),
    'GET /api/ai/conversations': { conversations: [] },
    'GET /api/ai/budget': { paused: false, resets_on: '2026-12-01' },
    'GET /api/ai/digest': () => state,
    'PUT /api/ai/digest': (body: unknown) => {
      state.enabled = (body as { enabled: boolean }).enabled
      return { enabled: state.enabled }
    },
    ...extra,
  })
  return { api, state }
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('the weekly digest', () => {
  it('is off by default and turns on with the toggle, sending only the flag', async () => {
    const { api } = digestApi()
    renderAt('/assistant')
    const toggle = await screen.findByRole('switch', { name: 'Write a weekly summary' })
    expect(toggle).not.toBeChecked()
    expect(screen.getByText('No weekly summary yet.')).toBeInTheDocument()
    fireEvent.click(toggle)
    await waitFor(() => {
      expect(api.to('PUT', '/api/ai/digest')).toHaveLength(1)
    })
    expect(api.to('PUT', '/api/ai/digest')[0]?.body).toEqual({ enabled: true })
    await waitFor(() => {
      expect(screen.getByRole('switch', { name: 'Write a weekly summary' })).toBeChecked()
    })
  })

  it('shows a ready digest as text, labelled, and words a skipped or failed week quietly', async () => {
    digestApi({
      enabled: true,
      digests: [
        digest('2026-11-02', 'ready', 'Synthetic **steady** week. See https://synthetic-attacker.example.test <img src=x>'),
        digest('2026-10-26', 'skipped_budget'),
        digest('2026-10-19', 'unavailable'),
      ],
    })
    const { container } = renderAt('/assistant')
    expect(await screen.findByText('steady')).toBeInTheDocument()
    expect(screen.getByText('steady').tagName).toBe('STRONG')
    expect(container.querySelector('article a, article img, article script')).toBeNull()
    expect(screen.getAllByText(LABEL)).toHaveLength(1)
    expect(screen.getByText("This week's summary was skipped: this month's AI budget for your account was used up.")).toBeInTheDocument()
    expect(screen.getByText("This week's summary couldn't be written.")).toBeInTheDocument()
  })

  it('names the weekly email in the toggle help', async () => {
    digestApi()
    renderAt('/assistant')
    expect(await screen.findByText(/You get an email when a summary is ready\./)).toBeInTheDocument()
  })

  it('gives the reset date of a skipped week while the budget is used up', async () => {
    digestApi({ enabled: true, digests: [digest('2026-10-26', 'skipped_budget')] }, true, {
      'GET /api/ai/budget': { paused: true, resets_on: '2026-12-01' },
    })
    renderAt('/assistant')
    expect(await screen.findByText(/This week's summary was skipped.*It resets on .*2026/)).toBeInTheDocument()
  })

  it('gives no reset date for a skipped week of an earlier month', async () => {
    digestApi({ enabled: true, digests: [digest('2026-09-28', 'skipped_budget', null, '2026-10-06T03:00:00Z')] }, true, {
      'GET /api/ai/budget': { paused: true, resets_on: '2026-12-01' },
    })
    renderAt('/assistant')
    expect(await screen.findByText("This week's summary was skipped: this month's AI budget for your account was used up.")).toBeInTheDocument()
    expect(screen.queryByText(/It resets on/)).not.toBeInTheDocument()
  })

  it('reads the creation time as a UTC day, whatever offset the API wrote', async () => {
    // 2026-11-01T00:30+02:00 is 2026-10-31 in UTC: an earlier budget month, so no reset date.
    digestApi({ enabled: true, digests: [digest('2026-11-23', 'skipped_budget', null, '2026-11-01T00:30:00+02:00')] }, true, {
      'GET /api/ai/budget': { paused: true, resets_on: '2026-12-01' },
    })
    renderAt('/assistant')
    expect(await screen.findByText("This week's summary was skipped: this month's AI budget for your account was used up.")).toBeInTheDocument()
    expect(screen.queryByText(/It resets on/)).not.toBeInTheDocument()
  })

  it('cannot be turned on while AI is not available, but can be turned off', async () => {
    digestApi({ available: false, enabled: false })
    renderAt('/assistant')
    expect(await screen.findByRole('switch', { name: 'Write a weekly summary' })).toBeDisabled()
  })

  it('words a refused opt-in', async () => {
    digestApi({}, true, { 'PUT /api/ai/digest': () => reason(409, 'ai_not_enabled') })
    renderAt('/assistant')
    fireEvent.click(await screen.findByRole('switch', { name: 'Write a weekly summary' }))
    await waitFor(() => {
      expect(screen.getByRole('switch', { name: 'Write a weekly summary' })).not.toBeChecked()
    })
    expect(document.querySelector('.ov-notice, [role="alert"]')).not.toBeNull()
  })

  it('is not shown, and not read, without a given consent', async () => {
    const { api } = digestApi({}, false)
    renderAt('/assistant')
    expect(await screen.findByRole('checkbox', { name: 'I have read this and agree.' })).toBeInTheDocument()
    expect(screen.queryByRole('switch', { name: 'Write a weekly summary' })).toBeNull()
    expect(api.to('GET', '/api/ai/digest')).toHaveLength(0)
  })
})
