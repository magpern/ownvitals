import { useCallback, useEffect, useState } from 'react'
import { getDigest, setDigestEnabled, type Digest, type DigestState } from '../../api/digest'
import { getBudget } from '../../api/assistant'
import { errorMessage } from '../../api/messages'
import { useT } from '../../i18n'
import { useFormat } from '../../i18n/useFormat'
import { Row, Toggle } from '../../pages/settings/controls'
import { Notice } from '../forms'
import { Card } from '../ui/Card'
import { MessageText } from './MessageText'

// The weekly digest (issue 209; ADR 0014 sections 3 and 5), shown on the assistant page under the
// conversations while AI consent is given. Built only from components the design has: a card with
// the Settings row and toggle for the opt-in (default off), then the newest digests, each with its
// week, the text as text only (MessageText: no link, HTML or image) and the AI label. A skipped or
// failed week shows one quiet sentence. No nudge, no count of unread digests: the mail ("your
// weekly summary is waiting") links to this page and says nothing else.

// The first day of the budget month that ends on `resetsOn` (the first of the next month). A
// skipped week stored before it (by `createdAt`: a week's digest runs at least 7 days after the
// week starts, so it is skipped under the budget of the month it ran in) was skipped under an
// earlier month's budget: no reset date for it.
function budgetMonthStart(resetsOn: string): string {
  const year = Number(resetsOn.slice(0, 4))
  const month = Number(resetsOn.slice(5, 7))
  const [y, m] = month === 1 ? [year - 1, 12] : [year, month - 1]
  return `${String(y).padStart(4, '0')}-${String(m).padStart(2, '0')}-01`
}

function DigestItem({ digest, resetsOn }: { digest: Digest; resetsOn: string | null }) {
  const t = useT()
  const f = useFormat()
  return (
    <article className="ov-stack" aria-label={t('assistant.digest.week', { date: f.day(digest.weekStart) })}>
      <h3 className="ov-fcard__name">{t('assistant.digest.week', { date: f.day(digest.weekStart) })}</h3>
      {digest.status === 'ready' && digest.content !== null ? (
        <>
          <MessageText content={digest.content} />
          <p className="ov-muted">{t('assistant.label')}</p>
        </>
      ) : (
        <p className="ov-muted">
          {digest.status !== 'skipped_budget'
            ? t('assistant.digest.unavailable')
            : resetsOn !== null && digest.createdAt.slice(0, 10) >= budgetMonthStart(resetsOn)
              ? t('assistant.digest.skippedUntil', { date: f.day(resetsOn) })
              : t('assistant.digest.skipped')}
        </p>
      )}
    </article>
  )
}

export function DigestView() {
  const t = useT()
  const [state, setState] = useState<DigestState | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  // The reset date of the month's budget, only while it is used up: it is the date a skipped
  // week's reason ends. Best effort: without it the skipped sentence has no date.
  const [resetsOn, setResetsOn] = useState<string | null>(null)

  const load = useCallback(() => {
    getDigest().then(
      (found) => {
        setState(found)
        setError(null)
      },
      () => {
        setError(t('assistant.digest.loadFailed'))
      },
    )
  }, [t])

  useEffect(() => {
    load()
    getBudget().then(
      (budget) => {
        setResetsOn(budget.paused ? budget.resetsOn : null)
      },
      () => {
        setResetsOn(null)
      },
    )
  }, [load])

  const flip = async (enabled: boolean) => {
    setBusy(true)
    setError(null)
    try {
      await setDigestEnabled(enabled)
      load()
    } catch (caught) {
      setError(errorMessage(caught))
    } finally {
      setBusy(false)
    }
  }

  return (
    <Card title={t('assistant.digest.title')}>
      {error !== null ? <Notice kind="error">{error}</Notice> : null}
      {state !== null ? (
        <>
          <Row label={t('assistant.digest.toggle')} help={t('assistant.digest.help')}>
            <Toggle
              label={t('assistant.digest.toggle')}
              checked={state.enabled}
              disabled={busy || (!state.available && !state.enabled)}
              onChange={(checked) => {
                void flip(checked)
              }}
            />
          </Row>
          {state.digests.length === 0 ? (
            <p className="ov-muted">{t('assistant.digest.empty')}</p>
          ) : (
            state.digests.map((digest) => <DigestItem key={digest.weekStart} digest={digest} resetsOn={resetsOn} />)
          )}
        </>
      ) : null}
    </Card>
  )
}
