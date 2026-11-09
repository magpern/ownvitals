import { fireEvent, screen, waitFor } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { SIGNED_IN, fakeApi } from '../../test/fakeApi'
import { fakeProfile } from '../../test/profileFixtures'
import { renderAt } from '../../test/render'

// The AI row of "Your data" (issue 206): consent and withdrawal. Synthetic: a made-up consent
// text and fingerprint, no network, no account.

const TEXT = 'Synthetic consent text, paragraph one.\n\nParagraph two <script>alert(1)</script> https://example.test'
const SHOWN = { version: 1, locale: 'en', text: TEXT, sha256: 'a'.repeat(64) }

function consent(state: 'none' | 'given' | 'outdated', enabled = true) {
  return {
    enabled,
    consent: { state, version: state === 'none' ? null : 1, given_at: state === 'none' ? null : '2026-11-09T10:00:00Z' },
    consent_text: SHOWN,
  }
}

/** The profile's `ai_available` is the effective flag, the same the consent's `enabled` reports. */
function aiApi(start: ReturnType<typeof consent>) {
  const available = start.enabled
  let current = start
  return fakeApi({
    'GET /api/auth/session': SIGNED_IN,
    'GET /api/profile': fakeProfile({ ai_available: available }),
    'GET /api/ai/consent?locale=en': () => current,
    'POST /api/ai/consent': () => {
      current = consent('given')
      return current
    },
    'DELETE /api/ai/consent': () => {
      current = consent('none')
      return { withdrawn: true, cancelled_jobs: 0, deleted_messages: 3 }
    },
  })
}

describe('AI insights in Your data', () => {
  it('stays the read-only switch while AI is off and no consent is live', async () => {
    aiApi(consent('none', false))
    renderAt('/settings/data')
    const toggle = await screen.findByRole('switch', { name: 'AI insights' })
    expect(toggle).toBeDisabled()
    expect(toggle).not.toBeChecked()
    expect(screen.queryByRole('button', { name: 'Give consent' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Withdraw consent' })).toBeNull()
  })

  it('shows the text as plain text and gives consent only after the box is ticked', async () => {
    const api = aiApi(consent('none'))
    renderAt('/settings/data')
    expect(await screen.findByText('Synthetic consent text, paragraph one.')).toBeInTheDocument()
    expect(screen.getByText(/Paragraph two <script>alert\(1\)<\/script>/)).toBeInTheDocument()
    expect(document.querySelector('.ov-ai-consent script')).toBeNull()
    expect(document.querySelector('.ov-ai-consent a')).toBeNull()
    const give = screen.getByRole('button', { name: 'Give consent' })
    const box = screen.getByRole('checkbox', { name: 'I have read this and agree.' })
    expect(box).not.toBeChecked()
    expect(give).toBeDisabled()
    fireEvent.click(box)
    fireEvent.click(give)
    await waitFor(() => {
      expect(api.to('POST', '/api/ai/consent')).toHaveLength(1)
    })
    expect(api.to('POST', '/api/ai/consent')[0]?.body).toEqual({ version: 1, locale: 'en', sha256: 'a'.repeat(64), agree: true })
    expect(await screen.findByRole('button', { name: 'Withdraw consent' })).toBeInTheDocument()
    expect(screen.getByRole('switch', { name: 'AI insights' })).toBeChecked()
  })

  it('asks for consent again when the text changed, and still offers withdrawal', async () => {
    aiApi(consent('outdated'))
    renderAt('/settings/data')
    expect(await screen.findByText(/The consent text has changed since you agreed/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Give consent' })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Withdraw consent' })).toBeInTheDocument()
    expect(screen.getByRole('switch', { name: 'AI insights' })).not.toBeChecked()
  })

  it('withdraws after a confirmation, without a step-up, and says the history is deleted', async () => {
    const api = aiApi(consent('given'))
    renderAt('/settings/data')
    fireEvent.click(await screen.findByRole('button', { name: 'Withdraw consent' }))
    expect(screen.getByRole('alert')).toHaveTextContent('deletes your AI conversation history')
    fireEvent.click(screen.getByRole('button', { name: 'Withdraw and delete history' }))
    expect(await screen.findByText('Consent withdrawn. Your AI conversation history is deleted.')).toBeInTheDocument()
    expect(api.to('DELETE', '/api/ai/consent')).toHaveLength(1)
    expect(await screen.findByRole('button', { name: 'Give consent' })).toBeInTheDocument()
  })

  it.each(['given', 'outdated'] as const)(
    'offers withdrawal of a %s consent while AI is switched off, and no new consent',
    async (state) => {
      const api = aiApi(consent(state, false))
      renderAt('/settings/data')
      fireEvent.click(await screen.findByRole('button', { name: 'Withdraw consent' }))
      expect(screen.queryByRole('button', { name: 'Give consent' })).toBeNull()
      expect(screen.getByRole('switch', { name: 'AI insights' })).not.toBeChecked()
      fireEvent.click(screen.getByRole('button', { name: 'Withdraw and delete history' }))
      expect(await screen.findByText('Consent withdrawn. Your AI conversation history is deleted.')).toBeInTheDocument()
      expect(api.to('DELETE', '/api/ai/consent')).toHaveLength(1)
      expect(screen.getByRole('switch', { name: 'AI insights' })).toBeDisabled()
    },
  )
})
