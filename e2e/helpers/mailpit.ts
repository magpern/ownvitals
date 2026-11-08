import { MAILPIT_URL } from './env.ts'

// Reads mails from mailpit's HTTP API. Every test uses an address of its own, so a message is
// found by recipient and subject.

interface Summary {
  ID: string
  Subject: string
  To: { Address: string }[]
}

const sleep = (ms: number): Promise<void> => new Promise((resolve) => setTimeout(resolve, ms))

async function json<T>(path: string): Promise<T> {
  const response = await fetch(`${MAILPIT_URL}${path}`)
  if (!response.ok) throw new Error(`mailpit ${path}: ${String(response.status)}`)
  return (await response.json()) as T
}

async function waitForSummary(to: string, subject: string, timeoutMs: number): Promise<Summary> {
  const deadline = Date.now() + timeoutMs
  for (;;) {
    const { messages } = await json<{ messages: Summary[] }>('/api/v1/messages?limit=100')
    const found = messages.find(
      (m) => m.Subject === subject && m.To.some((t) => t.Address.toLowerCase() === to.toLowerCase()),
    )
    if (found) return found
    if (Date.now() > deadline) throw new Error(`no mail "${subject}" arrived`)
    await sleep(500)
  }
}

/** The text of the newest mail to `to` with this subject; waits up to `timeoutMs` for it. */
export async function waitForMail(to: string, subject: string, timeoutMs = 20_000): Promise<string> {
  const found = await waitForSummary(to, subject, timeoutMs)
  return (await json<{ Text: string }>(`/api/v1/message/${found.ID}`)).Text
}

/** One header (first value) of the newest mail to `to` with this subject, or null if absent. */
export async function waitForMailHeader(
  to: string,
  subject: string,
  header: string,
  timeoutMs = 20_000,
): Promise<string | null> {
  const found = await waitForSummary(to, subject, timeoutMs)
  const headers = await json<Record<string, string[]>>(`/api/v1/message/${found.ID}/headers`)
  const key = Object.keys(headers).find((k) => k.toLowerCase() === header.toLowerCase())
  return key === undefined ? null : (headers[key]?.[0] ?? null)
}

/** The link with a `#token=` fragment in a mail's text. */
export function tokenLink(text: string): string {
  const match = /https?:\/\/[^\s]+#token=[\w-]+/.exec(text)
  if (!match) throw new Error('the mail holds no link with a token')
  return match[0]
}
