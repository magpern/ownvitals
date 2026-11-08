import type { Page } from '@playwright/test'

export interface ApiAnswer {
  status: number
  body: unknown
}

/**
 * A POST from inside the page, as the SPA makes it: same origin, the session cookie, and the CSRF
 * token that GET /api/auth/session returns. For backend actions no screen offers yet.
 */
export async function postFromPage(page: Page, path: string, body: unknown): Promise<ApiAnswer> {
  return page.evaluate(
    async ({ path, body }) => {
      const session = (await (await fetch('/api/auth/session')).json()) as { csrf_token?: string }
      const response = await fetch(path, {
        method: 'POST',
        headers: { 'content-type': 'application/json', 'X-CSRF-Token': session.csrf_token ?? '' },
        body: JSON.stringify(body),
      })
      return { status: response.status, body: (await response.json().catch(() => null)) as unknown }
    },
    { path, body },
  )
}

/** The `reason` of an error answer (`{ detail: { reason } }`), or null. */
export function reasonOf(answer: ApiAnswer): string | null {
  const detail = (answer.body as { detail?: { reason?: unknown } } | null)?.detail
  return typeof detail?.reason === 'string' ? detail.reason : null
}
