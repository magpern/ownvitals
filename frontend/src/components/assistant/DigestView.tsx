import { useCallback, useEffect, useState } from 'react'
import { getDigest, setDigestEnabled, type Digest, type DigestState } from '../../api/digest'
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

function DigestItem({ digest }: { digest: Digest }) {
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
        <p className="ov-muted">{t(digest.status === 'skipped_budget' ? 'assistant.digest.skipped' : 'assistant.digest.unavailable')}</p>
      )}
    </article>
  )
}

export function DigestView() {
  const t = useT()
  const [state, setState] = useState<DigestState | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

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
            state.digests.map((digest) => <DigestItem key={digest.weekStart} digest={digest} />)
          )}
        </>
      ) : null}
    </Card>
  )
}
