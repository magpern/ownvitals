import { useState } from 'react'
import { deleteAccount } from '../../api/profile'
import { errorMessage } from '../../api/messages'
import { SIGN_IN_PATH } from '../../auth/access'
import { useSession } from '../../auth/SessionContext'
import { Notice } from '../../components/forms'
import { DoctorReport } from '../../components/reports/DoctorReport'
import { ExportCard } from '../../components/reports/ExportCard'
import { Cancelled, useProtected } from '../../components/StepUp'
import { Button } from '../../components/ui/Button'
import { Card, Stack } from '../../components/ui/Card'
import { useT } from '../../i18n'
import { useRouter } from '../../router/Router'
import { AiSection } from './AiSection'
import { Row, Toggle } from './controls'
import type { ProfileState } from './useProfile'

// "Your data" (/settings/data): the experimental state (read-only here), AI consent (`AiSection`,
// issue 206), the export (`ExportCard`, issue 211) and deleting the account behind
// the shared "Confirm it's you" dialog: the server wants a recent password and a fresh
// authenticator code.

function Flags({ state }: { state: ProfileState }) {
  const t = useT()
  const { profile } = state
  if (profile === null) return null
  return (
    <Card title={t('settings.flags.title')}>
      <Row
        label={t('settings.flags.experimental.label')}
        help={profile.experimentalAvailable ? t('settings.flags.experimental.help') : t('settings.flags.experimental.off')}
      >
        <Toggle
          label={t('settings.flags.experimental.label')}
          checked={profile.experimentalAvailable}
          disabled
        />
      </Row>
      <AiSection available={profile.aiAvailable} />
    </Card>
  )
}

function DeleteAccount() {
  const t = useT()
  const protect = useProtected()
  const { refresh } = useSession()
  const { navigate } = useRouter()
  const [asking, setAsking] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [revokeFailed, setRevokeFailed] = useState(false)

  const leave = async () => {
    await refresh()
    navigate(SIGN_IN_PATH)
  }

  const run = async () => {
    setBusy(true)
    setError(null)
    try {
      const failed = await protect(t('settings.data.delete.purpose'), deleteAccount)
      if (failed) setRevokeFailed(true)
      else await leave()
    } catch (caught) {
      if (!(caught instanceof Cancelled)) setError(errorMessage(caught))
      setAsking(false)
    } finally {
      setBusy(false)
    }
  }

  const below = (
    <>
      {error !== null ? <Notice kind="error">{error}</Notice> : null}
      {revokeFailed ? <Notice kind="info">{t('settings.data.delete.revokeFailed')}</Notice> : null}
      {asking && !revokeFailed ? <p role="alert">{t('settings.data.delete.confirm')}</p> : null}
    </>
  )

  return (
    <Row label={t('settings.data.delete.label')} help={t('settings.data.delete.help')} below={below}>
      {revokeFailed ? (
        <Button
          onClick={() => {
            void leave()
          }}
        >
          {t('settings.data.delete.continue')}
        </Button>
      ) : asking ? (
        <div className="ov-settings-actions">
          <Button
            onClick={() => {
              setAsking(false)
            }}
            disabled={busy}
          >
            {t('common.cancel')}
          </Button>
          <Button variant="danger-solid" lock disabled={busy} onClick={() => void run()}>
            {t('settings.data.delete.yes')}
          </Button>
        </div>
      ) : (
        <Button
          variant="danger"
          lock
          onClick={() => {
            setAsking(true)
          }}
        >
          {t('settings.data.delete.button')}
        </Button>
      )}
    </Row>
  )
}

export function DataTab({ state }: { state: ProfileState }) {
  const t = useT()
  return (
    <Stack gap="lg">
      <Flags state={state} />
      <Card title={t('settings.data.title')}>
        <ExportCard />
        <DoctorReport />
        <DeleteAccount />
      </Card>
    </Stack>
  )
}
