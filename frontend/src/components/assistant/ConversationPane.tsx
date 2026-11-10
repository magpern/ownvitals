import { useEffect, useId, useState } from 'react'
import { ApiError } from '../../api/client'
import {
  deleteConversation,
  getConversation,
  newRequestId,
  sendMessage,
  watchTurn,
  type Budget,
  type ConversationDetail,
} from '../../api/assistant'
import { codeMessage } from '../../api/codes'
import { errorMessage } from '../../api/messages'
import { useT } from '../../i18n'
import { useFormat } from '../../i18n/useFormat'
import { Notice } from '../forms'
import { Button } from '../ui/Button'
import { Card } from '../ui/Card'
import { MessageText } from './MessageText'

// One conversation (docs/design/README.md, "Assistant"): its messages in order, every answer
// labelled, the running turn's status, the composer and "Delete conversation". A sent message
// queues a turn in the worker; the page follows the turn's job events and then re-reads the
// conversation. Nothing the server sends is rendered as HTML.

const RUNNING = new Set(['queued', 'running'])

function useConversation(id: string) {
  const [detail, setDetail] = useState<ConversationDetail | null>(null)
  const [failed, setFailed] = useState(false)
  const [version, setVersion] = useState(0)
  useEffect(() => {
    const controller = new AbortController()
    getConversation(id, controller.signal).then(
      (found) => {
        setDetail(found)
        setFailed(false)
      },
      () => {
        if (!controller.signal.aborted) setFailed(true)
      },
    )
    return () => {
      controller.abort()
    }
  }, [id, version])
  return { detail, failed, reload: () => { setVersion((n) => n + 1) } }
}

function Delete({ id, onDeleted }: { id: string; onDeleted: () => void }) {
  const t = useT()
  const [asking, setAsking] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const run = async () => {
    setBusy(true)
    setError(null)
    try {
      await deleteConversation(id)
      onDeleted()
    } catch (caught) {
      setError(errorMessage(caught))
      setBusy(false)
      setAsking(false)
    }
  }
  return (
    <div>
      {error !== null ? <Notice kind="error">{error}</Notice> : null}
      {asking ? (
        <>
          <p role="alert">{t('assistant.delete.confirm')}</p>
          <div className="ov-settings-actions">
            <Button disabled={busy} onClick={() => { setAsking(false) }}>
              {t('common.cancel')}
            </Button>
            <Button variant="danger-solid" disabled={busy} onClick={() => void run()}>
              {t('assistant.delete.yes')}
            </Button>
          </div>
        </>
      ) : (
        <Button variant="danger" onClick={() => { setAsking(true) }}>
          {t('assistant.delete.button')}
        </Button>
      )}
    </div>
  )
}

function refusalText(t: ReturnType<typeof useT>, error: unknown, budget: Budget | null, day: (d: string) => string): string {
  if (error instanceof ApiError) {
    if (error.reason === 'ai_budget_exhausted' && budget !== null) return t('assistant.paused', { date: day(budget.resetsOn) })
    if (error.reason === 'rate_limited') return t('assistant.limit')
  }
  return errorMessage(error)
}

export function ConversationPane({
  id,
  budget,
  onChanged,
  onDeleted,
}: {
  id: string
  budget: Budget | null
  onChanged: () => void
  onDeleted: () => void
}) {
  const t = useT()
  const f = useFormat()
  const boxId = useId()
  const { detail, failed, reload } = useConversation(id)
  const [draft, setDraft] = useState('')
  const [sending, setSending] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [requestId, setRequestId] = useState(newRequestId)
  const turn = detail?.turn ?? null
  const running = turn !== null && RUNNING.has(turn.state)

  // Follow a running turn (also one found on a reload) until it ends, then read the answer.
  useEffect(() => {
    if (turn === null || !RUNNING.has(turn.state)) return
    return watchTurn(turn.jobId, () => {
      reload()
      onChanged()
    })
    // eslint-disable-next-line react-hooks/exhaustive-deps -- one watch per turn
  }, [turn?.jobId, turn?.state])

  if (detail === null) {
    return <Card>{failed ? <Notice kind="error">{t('assistant.loadFailed')}</Notice> : <p className="ov-muted">{t('common.loadingDots')}</p>}</Card>
  }

  const paused = budget?.paused ?? false
  const last = detail.messages.at(-1)
  const unanswered = turn !== null && turn.state === 'failed' && last?.role === 'user'
  const send = async () => {
    const content = draft.trim()
    if (content === '') return
    setSending(true)
    setError(null)
    try {
      await sendMessage(id, content, requestId)
      setDraft('')
      setRequestId(newRequestId())
      reload()
      onChanged()
    } catch (caught) {
      setError(refusalText(t, caught, budget, (d) => f.day(d)))
    } finally {
      setSending(false)
    }
  }

  return (
    <Card>
      <div className="ov-stack">
        {detail.messages.length === 0 ? <p className="ov-muted">{t('assistant.empty')}</p> : null}
        <ol className="ov-chat" aria-label={t('assistant.list.label')}>
          {detail.messages.map((message) => (
            <li key={message.id} className="ov-chat__message" data-role={message.role}>
              <span className="ov-setting__label">{t(message.role === 'user' ? 'assistant.you' : 'assistant.answer')}</span>
              <MessageText content={message.content} />
              {message.role === 'assistant' ? <p className="ov-muted ov-small">{t('assistant.label')}</p> : null}
            </li>
          ))}
        </ol>
        {running ? <p className="ov-muted" role="status">{t('assistant.preparing')}</p> : null}
        {unanswered ? (
          <Notice kind="error">
            {turn.failure === 'ai_budget_exhausted' && budget !== null
              ? t('assistant.paused', { date: f.day(budget.resetsOn) })
              : turn.failure === 'ai_unavailable' || turn.failure === null
                ? t('assistant.failed')
                : codeMessage(turn.failure)}
          </Notice>
        ) : null}
        {paused && budget !== null ? <Notice kind="info">{t('assistant.paused', { date: f.day(budget.resetsOn) })}</Notice> : null}
        {error !== null ? <Notice kind="error">{error}</Notice> : null}
        <label htmlFor={boxId} className="ov-setting__label">
          {t('assistant.composer.label')}
        </label>
        <textarea
          id={boxId}
          className="ov-input ov-chat__composer"
          value={draft}
          maxLength={16000}
          disabled={running || paused || sending}
          onChange={(event) => {
            setDraft(event.target.value)
          }}
        />
        <div className="ov-settings-actions">
          <Button variant="primary" disabled={running || paused || sending || draft.trim() === ''} onClick={() => void send()}>
            {t('assistant.composer.send')}
          </Button>
        </div>
        <Delete id={id} onDeleted={onDeleted} />
      </div>
    </Card>
  )
}
