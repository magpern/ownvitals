import { get, request } from './client'
import { DecodeError, flag, list, object, optionalText, text } from './decode'

// The weekly AI digest (issue 209, ADR 0014 sections 3 and 5): whether AI is available to the
// user, their digest opt-in, and their newest digests. Web never calls the model: the worker
// writes a digest once a week for a user who opted in; this only reads them and sets the opt-in.

export type DigestStatus = 'ready' | 'skipped_budget' | 'unavailable'

export interface Digest {
  /** The Monday of the week the digest covers (YYYY-MM-DD). */
  weekStart: string
  status: DigestStatus
  /** The model's text, only when `ready`. Shown as text only. */
  content: string | null
  createdAt: string
}

export interface DigestState {
  /** AI is on and the user has a live consent to the current text. */
  available: boolean
  /** The user's digest opt-in. */
  enabled: boolean
  digests: Digest[]
}

const PATH = '/api/ai/digest'
const STATUSES: readonly string[] = ['ready', 'skipped_budget', 'unavailable']

function digest(value: unknown): Digest {
  const row = object(value, 'digest')
  const status = text(row, 'status')
  if (!STATUSES.includes(status)) throw new DecodeError('status')
  const content = optionalText(row, 'content')
  if ((status === 'ready') !== (content !== null)) throw new DecodeError('content')
  return { weekStart: text(row, 'week_start'), status: status as DigestStatus, content, createdAt: text(row, 'created_at') }
}

export async function getDigest(signal?: AbortSignal): Promise<DigestState> {
  const body = object(await get(PATH, signal), 'digest state')
  return { available: flag(body, 'available'), enabled: flag(body, 'enabled'), digests: list(body, 'digests').map(digest) }
}

export async function setDigestEnabled(enabled: boolean): Promise<boolean> {
  return flag(object(await request('PUT', PATH, { body: { enabled } }), 'opt-in'), 'enabled')
}
