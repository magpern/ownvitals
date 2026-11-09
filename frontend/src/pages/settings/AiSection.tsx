import { useEffect, useId, useState } from 'react'
import { getAiConsent, giveAiConsent, withdrawAiConsent, type AiConsent } from '../../api/ai'
import { errorMessage } from '../../api/messages'
import { Notice } from '../../components/forms'
import { Cancelled, useProtected } from '../../components/StepUp'
import { Button } from '../../components/ui/Button'
import { useI18n } from '../../i18n'
import { Row, Toggle } from './controls'

// The AI row of "Your data" (issue 206, ADR 0014 section 3). While AI insights are not available
// for the account it is the read-only switch of the mock-up. Once they are: the consent text as
// the server sends it (plain text, one paragraph per block), an unticked box, and "Give consent"
// behind the step-up dialog; with consent given, "Withdraw consent" (no step-up) stops AI at once
// and deletes the conversation history. The consent screen is not in the mock-ups: its shape
// follows the other rows of this tab (Architect, see the PR).

function Unavailable() {
  const { t } = useI18n()
  return (
    <Row label={t('settings.flags.ai.label')} help={t('settings.flags.ai.help')}>
      <Toggle label={t('settings.flags.ai.label')} checked={false} disabled />
    </Row>
  )
}

function ConsentForm({ consent, onGiven }: { consent: AiConsent; onGiven: (next: AiConsent) => void }) {
  const { t } = useI18n()
  const protect = useProtected()
  const boxId = useId()
  const [agreed, setAgreed] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const give = async () => {
    setBusy(true)
    setError(null)
    try {
      onGiven(await protect(t('settings.ai.consent.purpose'), () => giveAiConsent(consent.consentText)))
    } catch (caught) {
      if (!(caught instanceof Cancelled)) setError(errorMessage(caught))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="ov-ai-consent">
      {consent.state === 'outdated' ? <Notice kind="info">{t('settings.ai.consent.outdated')}</Notice> : null}
      <div className="ov-ai-consent__text" lang={consent.consentText.locale}>
        {consent.consentText.text
          .split(/\n\s*\n/)
          .filter((block) => block.trim() !== '')
          .map((block, index) => (
            <p key={index}>{block.trim()}</p>
          ))}
      </div>
      <label htmlFor={boxId} className="ov-ai-consent__agree">
        <input
          id={boxId}
          type="checkbox"
          checked={agreed}
          onChange={(event) => {
            setAgreed(event.target.checked)
          }}
        />{' '}
        {t('settings.ai.consent.agree')}
      </label>
      {error !== null ? <Notice kind="error">{error}</Notice> : null}
      <div className="ov-settings-actions">
        <Button variant="primary" lock disabled={!agreed || busy} onClick={() => void give()}>
          {t('settings.ai.consent.give')}
        </Button>
      </div>
    </div>
  )
}

function Withdraw({ onWithdrawn }: { onWithdrawn: () => void }) {
  const { t } = useI18n()
  const [asking, setAsking] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const run = async () => {
    setBusy(true)
    setError(null)
    try {
      await withdrawAiConsent()
      onWithdrawn()
    } catch (caught) {
      setError(errorMessage(caught))
    } finally {
      setBusy(false)
      setAsking(false)
    }
  }

  return (
    <div>
      {error !== null ? <Notice kind="error">{error}</Notice> : null}
      {asking ? (
        <>
          <p role="alert">{t('settings.ai.withdraw.confirm')}</p>
          <div className="ov-settings-actions">
            <Button
              onClick={() => {
                setAsking(false)
              }}
              disabled={busy}
            >
              {t('common.cancel')}
            </Button>
            <Button variant="danger-solid" disabled={busy} onClick={() => void run()}>
              {t('settings.ai.withdraw.yes')}
            </Button>
          </div>
        </>
      ) : (
        <Button
          variant="danger"
          onClick={() => {
            setAsking(true)
          }}
        >
          {t('settings.ai.withdraw.button')}
        </Button>
      )}
    </div>
  )
}

export function AiSection({ available }: { available: boolean }) {
  const { t, locale } = useI18n()
  const [consent, setConsent] = useState<AiConsent | null>(null)
  const [failed, setFailed] = useState(false)
  const [withdrawn, setWithdrawn] = useState(false)
  const [reload, setReload] = useState(0)

  useEffect(() => {
    if (!available) return
    const controller = new AbortController()
    getAiConsent(locale, controller.signal).then(
      (found) => {
        setConsent(found)
        setFailed(false)
      },
      () => {
        if (!controller.signal.aborted) setFailed(true)
      },
    )
    return () => {
      controller.abort()
    }
  }, [available, locale, reload])

  if (!available) return <Unavailable />
  if (failed) return <Row label={t('settings.flags.ai.label')} help={t('settings.ai.loadFailed')} />
  if (consent === null) return <Row label={t('settings.flags.ai.label')} help={t('common.loadingDots')} />
  if (!consent.enabled && consent.state !== 'given') return <Unavailable />

  const given = consent.state === 'given'
  const below = (
    <>
      {withdrawn ? <Notice kind="success">{t('settings.ai.withdraw.done')}</Notice> : null}
      {given ? (
        <Withdraw
          onWithdrawn={() => {
            setWithdrawn(true)
            setReload((n) => n + 1)
          }}
        />
      ) : (
        <ConsentForm
          consent={consent}
          onGiven={(next) => {
            setWithdrawn(false)
            setConsent(next)
          }}
        />
      )}
    </>
  )
  return (
    <Row
      label={t('settings.flags.ai.label')}
      help={given ? t('settings.ai.on') : t('settings.ai.off')}
      below={below}
    >
      <Toggle label={t('settings.flags.ai.label')} checked={given} disabled />
    </Row>
  )
}
