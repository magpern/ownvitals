import { fireEvent, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { SIGNED_IN, fakeApi, reason } from '../../test/fakeApi'
import { installEventSource } from '../../test/eventSourceFake'
import { fakeProfile } from '../../test/profileFixtures'
import { renderAt } from '../../test/render'

// The assistant page (issue 208): the consent prompt, the conversations, a turn followed over its
// job events, the cap, a failed turn, delete, and a withdrawal while a turn runs. Synthetic: made-up
// ids, texts and dates; no network, no account, no health data, no model.

const SHOWN = { version: 1, locale: 'en', text: 'Synthetic consent text.', sha256: 'a'.repeat(64) }

function consent(state: 'none' | 'given', enabled = true) {
  return {
    enabled,
    consent: { state, version: state === 'none' ? null : 1, given_at: state === 'none' ? null : '2026-11-09T10:00:00Z' },
    consent_text: SHOWN,
  }
}

const CONVERSATION = { id: 'c-1', created_at: '2026-11-09T10:00:00Z', updated_at: '2026-11-09T10:05:00Z', messages: 2 }

function message(id: string, role: 'user' | 'assistant', content: string) {
  return { id, role, content, created_at: '2026-11-09T10:01:00Z' }
}

interface State {
  consent: ReturnType<typeof consent>
  messages: ReturnType<typeof message>[]
  turn: { job_id: string; state: string; failure: string | null } | null
  paused: boolean
}

function assistantApi(start: Partial<State> = {}, extra: Record<string, unknown> = {}) {
  const state: State = {
    consent: consent('given'),
    messages: [message('m-1', 'user', 'How is my synthetic weight?'), message('m-2', 'assistant', 'Synthetic **steady** answer.')],
    turn: { job_id: 'j-1', state: 'completed', failure: null },
    paused: false,
    ...start,
  }
  const api = fakeApi({
    'GET /api/auth/session': SIGNED_IN,
    'GET /api/profile': fakeProfile({ ai_available: true }),
    'GET /api/ai/consent?locale=en': () => state.consent,
    'DELETE /api/ai/consent': () => {
      state.consent = consent('none')
      state.messages = []
      return { withdrawn: true, cancelled_jobs: 1, deleted_messages: 2 }
    },
    'GET /api/ai/conversations': () => ({ conversations: state.messages.length === 0 && state.consent.consent.state === 'none' ? [] : [CONVERSATION] }),
    'GET /api/ai/conversations/c-1': () => ({ conversation: CONVERSATION, messages: state.messages, turn: state.turn }),
    'GET /api/ai/budget': () => ({ paused: state.paused, resets_on: '2026-12-01' }),
    'POST /api/ai/conversations/c-1/messages': (body: unknown) => {
      const sent = body as { content: string }
      state.messages = [...state.messages, message('m-3', 'user', sent.content)]
      state.turn = { job_id: 'j-2', state: 'queued', failure: null }
      return { message_id: 'm-3', job_id: 'j-2' }
    },
    'DELETE /api/ai/conversations/c-1': { deleted: true },
    'GET /api/ai/digest': { available: true, enabled: false, digests: [] },
    ...extra,
  })
  return { api, state }
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('the assistant page', () => {
  it('asks for consent first and shows no conversation without it', async () => {
    const { api } = assistantApi({ consent: consent('none') })
    renderAt('/assistant')
    expect(await screen.findByRole('checkbox', { name: 'I have read this and agree.' })).not.toBeChecked()
    expect(screen.queryByRole('button', { name: 'New conversation' })).toBeNull()
    expect(api.to('GET', '/api/ai/conversations')).toHaveLength(0)
  })

  it('shows the conversation as text, every answer labelled', async () => {
    assistantApi()
    renderAt('/assistant')
    expect(await screen.findByText('How is my synthetic weight?')).toBeInTheDocument()
    expect(screen.getByText('steady').tagName).toBe('STRONG')
    expect(screen.getAllByText('AI-generated from your data. It can be wrong, and it is not medical advice.')).toHaveLength(1)
    expect(screen.queryByRole('link', { name: /Open the assistant/ })).toBeNull()
  })

  it('sends a message, follows the turn and reads the answer when it ends', async () => {
    const Source = installEventSource()
    const { api, state } = assistantApi()
    renderAt('/assistant')
    const box = await screen.findByRole('textbox', { name: 'Your message' })
    fireEvent.change(box, { target: { value: 'And my synthetic pulse?' } })
    fireEvent.click(screen.getByRole('button', { name: 'Send' }))
    expect(await screen.findByText('Preparing an answer…')).toBeInTheDocument()
    const [sent] = api.to('POST', '/api/ai/conversations/c-1/messages')
    expect(Object.keys(sent?.body as object).sort()).toEqual(['content', 'request_id'])
    expect((sent?.body as { request_id: string }).request_id).toMatch(/^[0-9a-f]{32}$/)
    await waitFor(() => {
      expect(Source.latest().url).toBe('/api/jobs/j-2/events')
    })
    expect(screen.getByRole('textbox', { name: 'Your message' })).toBeDisabled()
    state.messages = [...state.messages, message('m-4', 'assistant', 'Synthetic pulse answer.')]
    state.turn = { job_id: 'j-2', state: 'completed', failure: null }
    Source.latest().emit('completed')
    expect(await screen.findByText('Synthetic pulse answer.')).toBeInTheDocument()
    expect(Source.latest().readyState).toBe(Source.CLOSED)
  })

  it('says when the month is used up and keeps the composer closed', async () => {
    assistantApi({ paused: true })
    renderAt('/assistant')
    expect(await screen.findByText(/This month's AI budget for your account is used up\. It resets on/)).toBeInTheDocument()
    expect(screen.getByRole('textbox', { name: 'Your message' })).toBeDisabled()
  })

  it('says a turn could not be answered', async () => {
    assistantApi({
      messages: [message('m-1', 'user', 'Synthetic question')],
      turn: { job_id: 'j-1', state: 'failed', failure: 'ai_unavailable' },
    })
    renderAt('/assistant')
    expect(await screen.findByText("The assistant couldn't answer. Try again later.")).toBeInTheDocument()
  })

  it('words the chat limit', async () => {
    assistantApi({}, { 'POST /api/ai/conversations/c-1/messages': () => reason(429, 'rate_limited') })
    renderAt('/assistant')
    fireEvent.change(await screen.findByRole('textbox', { name: 'Your message' }), { target: { value: 'synthetic' } })
    fireEvent.click(screen.getByRole('button', { name: 'Send' }))
    expect(await screen.findByText('You can send 20 messages an hour. Try again later.')).toBeInTheDocument()
  })

  it('deletes a conversation after the confirmation', async () => {
    const { api } = assistantApi()
    renderAt('/assistant')
    fireEvent.click(await screen.findByRole('button', { name: 'Delete conversation…' }))
    expect(api.to('DELETE', '/api/ai/conversations/c-1')).toHaveLength(0)
    fireEvent.click(screen.getByRole('button', { name: 'Delete conversation' }))
    await waitFor(() => {
      expect(api.to('DELETE', '/api/ai/conversations/c-1')).toHaveLength(1)
    })
    expect(await screen.findByText('Conversation deleted.')).toBeInTheDocument()
  })

  it('stops following a running turn and hides the history when consent is withdrawn', async () => {
    const Source = installEventSource()
    const { api } = assistantApi({ turn: { job_id: 'j-9', state: 'running', failure: null } })
    renderAt('/assistant')
    expect(await screen.findByText('Preparing an answer…')).toBeInTheDocument()
    await waitFor(() => {
      expect(Source.latest().url).toBe('/api/jobs/j-9/events')
    })
    fireEvent.click(screen.getByRole('button', { name: 'Withdraw consent' }))
    fireEvent.click(screen.getByRole('button', { name: 'Withdraw and delete history' }))
    await waitFor(() => {
      expect(api.to('DELETE', '/api/ai/consent')).toHaveLength(1)
    })
    await waitFor(() => {
      expect(screen.queryByText('How is my synthetic weight?')).toBeNull()
    })
    expect(Source.latest().readyState).toBe(Source.CLOSED)
    expect(await screen.findByRole('checkbox', { name: 'I have read this and agree.' })).toBeInTheDocument()
  })
})

describe('the AI row of Settings', () => {
  it('links to the assistant while consent is given', async () => {
    assistantApi()
    renderAt('/settings/data')
    expect(await screen.findByRole('link', { name: 'Open the assistant' })).toHaveAttribute('href', '/assistant')
  })
})
