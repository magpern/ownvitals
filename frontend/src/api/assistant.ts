import { del, get, post } from './client'
import { DecodeError, count, flag, isObject, list, object, optionalText, text } from './decode'

// The chat assistant (issue 208, ADR 0014 section 5): the user's conversations, their messages
// and the latest turn, sending a message, deleting a conversation, and whether this month's
// budget is used up. Web never calls the model: a message queues a turn in the worker; the page
// follows the turn's job events (ids and states only) and then reads the answer here.

export type Role = 'user' | 'assistant'

export interface Conversation {
  id: string
  createdAt: string
  updatedAt: string
  messages: number
}

export interface ChatMessage {
  id: string
  role: Role
  /** The stored text: the user's own, or the model's answer. Shown as text only. */
  content: string
  createdAt: string
}

export interface Turn {
  jobId: string
  state: string
  /** A closed failure category (`ai_unavailable`, `ai_budget_exhausted`, ...). */
  failure: string | null
}

export interface ConversationDetail {
  conversation: Conversation
  messages: ChatMessage[]
  turn: Turn | null
}

export interface Budget {
  paused: boolean
  resetsOn: string
}

const BASE = '/api/ai/conversations'

function conversation(value: unknown): Conversation {
  const row = object(value, 'conversation')
  return {
    id: text(row, 'id'),
    createdAt: text(row, 'created_at'),
    updatedAt: text(row, 'updated_at'),
    messages: count(row, 'messages'),
  }
}

function message(value: unknown): ChatMessage {
  const row = object(value, 'message')
  const role = text(row, 'role')
  if (role !== 'user' && role !== 'assistant') throw new DecodeError('role')
  return { id: text(row, 'id'), role, content: text(row, 'content'), createdAt: text(row, 'created_at') }
}

function turn(value: unknown): Turn | null {
  if (value === null || value === undefined) return null
  const row = object(value, 'turn')
  return { jobId: text(row, 'job_id'), state: text(row, 'state'), failure: optionalText(row, 'failure') }
}

export async function listConversations(signal?: AbortSignal): Promise<Conversation[]> {
  const body = object(await get(BASE, signal), 'conversations')
  return list(body, 'conversations').map(conversation)
}

export async function createConversation(): Promise<Conversation> {
  return conversation(object(await post(BASE), 'created').conversation)
}

export async function getConversation(id: string, signal?: AbortSignal): Promise<ConversationDetail> {
  const body = object(await get(`${BASE}/${encodeURIComponent(id)}`, signal), 'detail')
  return {
    conversation: conversation(body.conversation),
    messages: list(body, 'messages').map(message),
    turn: turn(body.turn),
  }
}

export async function deleteConversation(id: string): Promise<boolean> {
  return flag(object(await del(`${BASE}/${encodeURIComponent(id)}`), 'deleted'), 'deleted')
}

/** A random request id, so a repeated send is the same message (the server's idempotency key). */
export function newRequestId(): string {
  const bytes = new Uint8Array(16)
  crypto.getRandomValues(bytes)
  return Array.from(bytes, (b) => b.toString(16).padStart(2, '0')).join('')
}

export async function sendMessage(id: string, content: string, requestId: string): Promise<{ jobId: string }> {
  const body = object(await post(`${BASE}/${encodeURIComponent(id)}/messages`, { content, request_id: requestId }), 'posted')
  return { jobId: text(body, 'job_id') }
}

export async function getBudget(signal?: AbortSignal): Promise<Budget> {
  const body = object(await get('/api/ai/budget', signal), 'budget')
  return { paused: flag(body, 'paused'), resetsOn: text(body, 'resets_on') }
}

export type TurnEnd = { kind: 'completed' } | { kind: 'failed'; category: string | null } | { kind: 'cancelled' } | { kind: 'lost' }

const ENDS = ['completed', 'failed', 'dead_letter', 'cancelled'] as const

/**
 * Follows a turn's job events until it ends. The events carry states and a closed category,
 * never text; the answer is read afterwards with `getConversation`. Returns a function that stops
 * listening. A stream the browser closes for good without an end is reported as `lost`.
 */
export function watchTurn(jobId: string, onEnd: (end: TurnEnd) => void): () => void {
  const source = new EventSource(`/api/jobs/${encodeURIComponent(jobId)}/events`)
  let ended = false
  const finish = (end: TurnEnd) => {
    if (ended) return
    ended = true
    source.close()
    onEnd(end)
  }
  for (const name of ENDS) {
    source.addEventListener(name, (event: MessageEvent) => {
      if (name === 'completed') finish({ kind: 'completed' })
      else if (name === 'cancelled') finish({ kind: 'cancelled' })
      else finish({ kind: 'failed', category: categoryOf(event.data) })
    })
  }
  source.onerror = () => {
    if (source.readyState === EventSource.CLOSED) finish({ kind: 'lost' })
  }
  return () => {
    ended = true
    source.close()
  }
}

function categoryOf(data: unknown): string | null {
  if (typeof data !== 'string') return null
  try {
    const parsed: unknown = JSON.parse(data)
    if (!isObject(parsed)) return null
    return typeof parsed.category === 'string' ? parsed.category : null
  } catch {
    return null
  }
}
