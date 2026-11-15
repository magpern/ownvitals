import { useEffect, useId, useRef, useState } from 'react'
import { getAiConsent, giveAiConsent, withdrawAiConsent, type AiConsent } from '../../api/ai'
import { errorMessage } from '../../api/messages'
import { Notice } from '../../components/forms'
import { Cancelled, useProtected } from '../../components/StepUp'
import { Button } from '../../components/ui/Button'
import { useI18n } from '../../i18n'
import { Link } from '../../router/Router'
import { Row, Toggle } from './controls'

// The AI row of "Your data" (issue 206, ADR 0014 section 3). While AI insights are not available
// and no consent is live, it is the read-only switch of the mock-up. Once they are: the consent
// text as the server sends it (plain text, one paragraph per block), an unticked box, and "Give
// consent" behind the step-up dialog. Whenever a consent is live (given or for an older text),
// whatever the flag, "Withdraw consent" (no step-up) stops AI at once and deletes the
// conversation history. The consent screen is not in the mock-ups: its shape
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

// Issue 208: the assistant page shows this same row (`embedded`) for its consent prompt, and
// hears of every change (`onChange`); here, a given consent links to the assistant.
export function AiSection({
  available,
  embedded = false,
  onChange,
}: {
  available: boolean
  embedded?: boolean
  onChange?: (consent: AiConsent | null) => void
}) {
  const { t, locale } = useI18n()
  const [consent, setConsent] = useState<AiConsent | null>(null)
  const [failed, setFailed] = useState(false)
  const [withdrawn, setWithdrawn] = useState(false)
  const [reload, setReload] = useState(0)
  const notify = useRef(onChange)
  useEffect(() => {
    notify.current = onChange
  })

  // Read even while AI is not available: a consent given before the flag went off must still be
  // withdrawable (leaving is always possible, ADR 0014 section 3).
  useEffect(() => {
    const controller = new AbortController()
    getAiConsent(locale, controller.signal).then(
      (found) => {
        setConsent(found)
        setFailed(false)
        notify.current?.(found)
      },
      () => {
        if (!controller.signal.aborted) setFailed(true)
      },
    )
    return () => {
      controller.abort()
    }
  }, [locale, reload])

  if (consent === null) {
    if (!available) return <Unavailable />
    return <Row label={t('settings.flags.ai.label')} help={t(failed ? 'settings.ai.loadFailed' : 'common.loadingDots')} />
  }
  const live = consent.state !== 'none' // given or outdated: withdrawal is always offered
  // One place for the notice, so it stays mounted while the row below it changes.
  const done = withdrawn ? <Notice kind="success">{t('settings.ai.withdraw.done')}</Notice> : null
  if (!live && !(available && consent.enabled)) {
    return (
      <>
        {done}
        <Unavailable />
      </>
    )
  }

  const given = consent.state === 'given'
  const offerConsent = consent.enabled && consent.state !== 'given'
  const below = (
    <>
      {offerConsent ? (
        <ConsentForm
          consent={consent}
          onGiven={(next) => {
            setWithdrawn(false)
            setConsent(next)
            onChange?.(next)
          }}
        />
      ) : null}
      {given && consent.enabled && !embedded ? (
        <p>
          <Link to="/assistant">{t('settings.ai.open')}</Link>
        </p>
      ) : null}
      {live ? (
        <Withdraw
          onWithdrawn={() => {
            setWithdrawn(true)
            setReload((n) => n + 1)
          }}
        />
      ) : null}
    </>
  )
  return (
    <>
      {done}
      <Row
        label={t('settings.flags.ai.label')}
        help={given && consent.enabled ? t('settings.ai.on') : t('settings.ai.off')}
        below={below}
      >
        <Toggle label={t('settings.flags.ai.label')} checked={given && consent.enabled} disabled />
      </Row>
    </>
  )
}
