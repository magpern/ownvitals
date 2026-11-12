import { useCallback, useEffect, useState } from 'react'
import { getMfaStatus, removeTotp, type MfaStatus } from '../../api/auth'
import { errorMessage } from '../../api/messages'
import { ENROL_PATH, withReturnTo } from '../../auth/access'
import { useSession } from '../../auth/SessionContext'
import { plural, useT } from '../../i18n'
import { useRouter } from '../../router/Router'
import { Notice } from '../forms'
import { Cancelled, useProtected } from '../StepUp'
import { Button } from '../ui/Button'
import { Card } from '../ui/Card'
import { StatePill } from '../ui/DataState'
import { Icon } from '../ui/Icon'

// "Second factor" of Settings → Sign-in and security (docs/design/mockups/src/Security.dc.html,
// the card's first two rows). The authenticator app is optional for users and required for
// administrators (owner decision of 2026-11-12, issue 475): while it is off the card recommends it and
// links to the enrolment page; while it is on a user can turn it off, behind the lock (a fresh
// code, through the shared "Confirm it's you" dialog). Replacing it and new recovery codes stay
// with their own issue.

const SECURITY_PATH = '/settings/security'

export function SecondFactorCard() {
  const t = useT()
  const protect = useProtected()
  const { session, refresh } = useSession()
  const { navigate } = useRouter()
  const [status, setStatus] = useState<MfaStatus | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const required = session?.totpRequired === true
  const on = session?.totpEnabled === true

  const load = useCallback((signal?: AbortSignal) => {
    getMfaStatus(signal).then(setStatus, (caught: unknown) => {
      if (!signal?.aborted) setError(errorMessage(caught))
    })
  }, [])

  useEffect(() => {
    const controller = new AbortController()
    load(controller.signal)
    return () => {
      controller.abort()
    }
  }, [load, on])

  const turnOff = async () => {
    setBusy(true)
    setError(null)
    try {
      await protect(t('security.factor.offPurpose'), removeTotp)
      await refresh()
    } catch (caught) {
      if (!(caught instanceof Cancelled)) setError(errorMessage(caught))
    } finally {
      setBusy(false)
    }
  }

  const state = on ? (required ? t('security.factor.onRequired') : t('security.factor.on')) : t('security.factor.off')
  return (
    <Card
      title={t('security.factor.title')}
      aside={
        <StatePill tone={on ? 'ok' : 'attn'} dot={on ? 'ok' : 'attn'}>
          {state}
        </StatePill>
      }
    >
      {error !== null ? <Notice kind="error">{error}</Notice> : null}
      <div className="ov-setting">
        <div className="ov-setting__what">
          <span className="ov-setting__label">{t('security.factor.app')}</span>
          <p>{on ? t('security.factor.appOn') : t('security.factor.appOff')}</p>
        </div>
        {on ? (
          required ? null : (
            <Button
              variant="danger"
              disabled={busy}
              onClick={() => {
                void turnOff()
              }}
            >
              <Icon name="lock" />
              {t('security.factor.turnOff')}
            </Button>
          )
        ) : (
          <Button
            variant="primary"
            onClick={() => {
              navigate(withReturnTo(ENROL_PATH, SECURITY_PATH))
            }}
          >
            {t('security.factor.setUp')}
          </Button>
        )}
      </div>
      {on && status !== null ? (
        <div className="ov-setting">
          <div className="ov-setting__what">
            <span className="ov-setting__label">{t('security.factor.codes')}</span>
            <p>{plural('security.factor.codesLeft', status.recoveryCodesLeft)}</p>
          </div>
        </div>
      ) : null}
    </Card>
  )
}
