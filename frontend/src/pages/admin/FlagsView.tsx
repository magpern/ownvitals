import { useState } from 'react'
import { decodeFlags, SETTINGS_PATH, type FlagView } from '../../api/adminViews'
import { errorMessage } from '../../api/messages'
import { useResource } from '../../api/useResource'
import { Notice } from '../../components/forms'
import { Cancelled, useProtected } from '../../components/StepUp'
import { Card } from '../../components/ui/Card'
import { ResourceView, StatePill } from '../../components/ui/DataState'
import { isMessageKey, useT } from '../../i18n'
import { BannerControl, SignupChoice, SWITCH_KEYS, SwitchControl, type ControlProps } from './FlagControls'

// Flags: what is in force, where it comes from, and the controls that tighten it (issue 550).
// Each change needs a code from the last 5 minutes (the shared step-up dialog) and is audited.

function FlagRow({ flag, write, busy, onChanged }: Omit<ControlProps, 'name'>) {
  const t = useT()
  const nameKey = `admin.flags.name.${flag.key}`
  const sourceKey = `admin.flags.source.${flag.source}`
  const name = isMessageKey(nameKey) ? t(nameKey) : flag.key
  // A known value is translated; anything else (a banner's text, a value added later) is shown as sent.
  const valueLabel = (value: string | null) => {
    if (value === null) return t('admin.flags.none')
    const key = `admin.flags.value.${value}`
    return isMessageKey(key) ? t(key) : value
  }
  const props = { flag, name, write, busy, onChanged }
  const isSwitch = SWITCH_KEYS.has(flag.key)
  let control = <StatePill tone={flag.storedValid ? 'neutral' : 'attn'}>{valueLabel(flag.effective)}</StatePill>
  if (flag.key === 'signup_mode') control = <SignupChoice {...props} />
  else if (isSwitch) control = <SwitchControl {...props} />
  else if (flag.key === 'banner') control = <BannerControl {...props} />
  const help = [
    flag.key === 'signup_mode' ? t('admin.flags.signup.help') : `${valueLabel(flag.effective)}.`,
    isMessageKey(sourceKey) ? t(sourceKey) : flag.source,
  ]
  if (isSwitch && flag.environment !== 'on') help.push(t('admin.flags.environmentOff'))
  if (!flag.storedValid) help.push(t('admin.flags.invalid'))
  return (
    <div className="ov-setting">
      <div className="ov-setting__what">
        <span className="ov-setting__label">{name}</span>
        <p>{help.join(' ')}</p>
      </div>
      {control}
    </div>
  )
}

export function FlagsView() {
  const t = useT()
  const protect = useProtected()
  const flags = useResource(SETTINGS_PATH, decodeFlags)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const write = (action: () => Promise<unknown>) => {
    setBusy(true)
    setError(null)
    protect(t('admin.flags.purpose'), action).then(
      () => {
        setBusy(false)
        flags.reload()
      },
      (caught: unknown) => {
        if (!(caught instanceof Cancelled)) setError(errorMessage(caught))
        setBusy(false)
      },
    )
  }

  return (
    <Card title={t('admin.flags.title')}>
      {error !== null ? <Notice kind="error">{error}</Notice> : null}
      <ResourceView resource={flags}>
        {(rows: FlagView[]) =>
          rows.length === 0 ? (
            <p className="ov-muted">{t('admin.flags.empty')}</p>
          ) : (
            <>
              {rows.map((flag) => (
                <FlagRow key={flag.key} flag={flag} write={write} busy={busy} onChanged={flags.reload} />
              ))}
            </>
          )
        }
      </ResourceView>
      <p className="ov-small ov-muted">{t('admin.flags.note')}</p>
    </Card>
  )
}
