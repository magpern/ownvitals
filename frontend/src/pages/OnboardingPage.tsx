import { useEffect } from 'react'
import { ConnectButton } from '../components/connections/ConnectButton'
import { stillImporting } from '../components/connections/connectionModel'
import { StreamsTable } from '../components/connections/StreamsTable'
import { useOutcome } from '../components/connections/useOutcome'
import { useStreamName } from '../components/connections/useStreamName'
import { useWithings } from '../components/connections/useWithings'
import { Notice } from '../components/forms'
import { Card, Stack } from '../components/ui/Card'
import { ResourceView } from '../components/ui/DataState'
import { useT } from '../i18n'
import { HOME_PATH } from '../auth/access'
import { Link } from '../router/Router'

// Onboarding (docs/design/mockups/src/Onboarding.dc.html): connect Withings, then watch the
// import per stream, then the overview. The account and authenticator step is done by the time the
// guard lets this page render.

const POLL_MS = 5000

function Steps({ current }: { current: 2 | 3 }) {
  const t = useT()
  const steps = ['account', 'connect', 'import'] as const
  return (
    <ol className="ov-steps" aria-label={t('connections.onboarding.steps')}>
      {steps.map((step, index) => (
        <li key={step} aria-current={index + 1 === current ? 'step' : undefined}>
          {t(`connections.onboarding.step.${step}`)}
        </li>
      ))}
    </ol>
  )
}

function ConnectPanel() {
  const t = useT()
  return (
    <Card title={t('connections.onboarding.connect.title')}>
      <Stack gap="sm">
        <p>{t('connections.onboarding.connect.lead')}</p>
        <h3>{t('connections.onboarding.reads.title')}</h3>
        <ul>
          <li>{t('connections.onboarding.reads.measures')}</li>
          <li>{t('connections.onboarding.reads.activity')}</li>
          <li>{t('connections.onboarding.reads.sleep')}</li>
          <li>{t('connections.onboarding.reads.profile')}</li>
        </ul>
        <p>{t('connections.onboarding.reads.never')}</p>
        <p className="ov-muted">{t('connections.onboarding.plus')}</p>
        <ConnectButton label={t('connections.action.connect')} wide />
        <p className="ov-muted">{t('connections.onboarding.connect.hint')}</p>
      </Stack>
    </Card>
  )
}

export function OnboardingPage() {
  const t = useT()
  const name = useStreamName()
  const { status, reload } = useWithings()
  const outcome = useOutcome(window.location.search)
  const streams = status.data?.streams
  const importing = status.data?.connection === 'connected' && streams !== undefined && stillImporting(streams)

  useEffect(() => {
    if (!importing) return
    const timer = window.setInterval(reload, POLL_MS)
    return () => {
      window.clearInterval(timer)
    }
  }, [importing, reload])

  return (
    <div className="ov-onboarding">
      <p className="ov-muted">{t('connections.onboarding.step', { step: status.data?.connection === 'connected' ? 3 : 2 })}</p>
      <Steps current={status.data?.connection === 'connected' ? 3 : 2} />
      {outcome !== null ? <Notice kind="error">{outcome}</Notice> : null}
      <ResourceView resource={status}>
        {(s) =>
          s.connection === 'connected' ? (
            <Card title={importing ? t('connections.onboarding.import.title') : t('connections.onboarding.import.done')}>
              <Stack gap="sm">
                <p>{t('connections.onboarding.import.lead')}</p>
                {s.streams.length === 0 ? (
                  <p className="ov-muted">{t('connections.onboarding.import.empty')}</p>
                ) : (
                  <StreamsTable streams={s.streams} name={name} />
                )}
                <p className="ov-muted">{t('connections.onboarding.import.note')}</p>
                <Link className="ov-btn ov-btn--primary" to={HOME_PATH}>
                  {t('connections.onboarding.import.overview')}
                </Link>
              </Stack>
            </Card>
          ) : (
            <ConnectPanel />
          )
        }
      </ResourceView>
    </div>
  )
}
