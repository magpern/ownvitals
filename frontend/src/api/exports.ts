import { ApiError, get, post } from './client'
import { DecodeError, list, object, optionalCount, optionalText, text, count, type JsonObject } from './decode'

// /api/exports (issue 211, ADR 0013 section 4): request the JSON/CSV export (recent sign-in and a
// fresh authenticator code), follow its state, and download it once (a fresh code again). The
// download is a same-origin fetch, so the browser sends `Sec-Fetch-Site: same-origin`; the file
// URL holds only the export's id and grants nothing on its own.

export type ExportState = 'building' | 'ready' | 'failed' | 'expired'

export interface ExportItem {
  id: string
  state: ExportState
  createdAt: string
  readyAt: string | null
  expiresAt: string | null
  size: number | null
  failure: string | null
  largest: string | null
  attemptsLeft: number
}

const REVOKE_AFTER_MS = 60_000

const STATES: readonly ExportState[] = ['building', 'ready', 'failed', 'expired']

export function decodeItem(value: unknown): ExportItem {
  const item = object(value, 'export')
  const state = STATES.find((s) => s === text(item, 'state'))
  if (state === undefined) throw new DecodeError('state')
  return {
    id: text(item, 'id'),
    state,
    createdAt: text(item, 'created_at'),
    readyAt: optionalText(item, 'ready_at'),
    expiresAt: optionalText(item, 'expires_at'),
    size: optionalCount(item, 'size'),
    failure: optionalText(item, 'failure'),
    largest: optionalText(item, 'largest'),
    attemptsLeft: count(item, 'attempts_left'),
  }
}

export async function listExports(signal?: AbortSignal): Promise<ExportItem[]> {
  return list(object(await get('/api/exports', signal), 'exports'), 'exports').map(decodeItem)
}

export async function requestExport(): Promise<ExportItem> {
  return decodeItem(object(await post('/api/exports'), 'requested').export)
}

function reasonOf(body: unknown, response: Response): string | null {
  const detail = (body as JsonObject | null)?.detail
  if (typeof detail === 'object' && detail !== null) {
    const why = (detail as JsonObject).reason
    if (typeof why === 'string') return why
  }
  return response.headers.get('X-Ownvitals-Reason')
}

function filenameOf(response: Response, fallback: string): string {
  const header = response.headers.get('Content-Disposition') ?? ''
  const match = /filename="([A-Za-z0-9._-]+)"/.exec(header)
  return match?.[1] ?? fallback
}

/** Fetch a single-use file and hand it to the browser. Refusals throw an `ApiError`. */
export async function downloadFile(path: string, accept: string, fallbackName: string): Promise<void> {
  const response = await fetch(path, {
    headers: { Accept: `${accept}, application/json` },
    credentials: 'same-origin',
  })
  if (!response.ok) {
    let body: unknown
    try {
      body = await response.json()
    } catch {
      body = undefined
    }
    throw new ApiError(response.status, reasonOf(body, response), null, body)
  }
  const blob = await response.blob()
  // A body shorter than its Content-Length is a broken download (the server aborts a stream it
  // cannot finish, issue 333): never hand a truncated archive to the browser as a file. With a
  // Content-Encoding (a proxy compressed it) the header counts the encoded bytes, not the blob's,
  // so the check is skipped and the browser's own length check is the one that applies.
  const expected = response.headers.get('Content-Length')
  const encoded = response.headers.get('Content-Encoding') !== null
  if (expected !== null && !encoded && Number(expected) !== blob.size) {
    throw new TypeError('incomplete download')
  }
  const url = URL.createObjectURL(blob)
  const link = document.createElement('a')
  link.href = url
  link.download = filenameOf(response, fallbackName)
  document.body.appendChild(link)
  link.click()
  link.remove()
  // Revoked later, not at once: some browsers (Firefox) start reading the blob only after the
  // click returns, and the server has already spent the export.
  window.setTimeout(() => {
    URL.revokeObjectURL(url)
  }, REVOKE_AFTER_MS)
}

/** Fetch the archive and hand it to the browser as a file. */
export function downloadExport(id: string): Promise<void> {
  return downloadFile(`/api/exports/${encodeURIComponent(id)}/file`, 'application/zip', 'ownvitals-export.zip')
}
