import { useId, useState } from 'react'
import type { FlagView } from '../../api/adminViews'
import { clearFlag, setFlag, type BannerLevel } from '../../api/adminWrites'
import { Button } from '../../components/ui/Button'
import { Icon } from '../../components/ui/Icon'
import { useT } from '../../i18n'
import { Toggle } from '../settings/controls'
import { FlagDialog } from './FlagDialog'

// The change controls of the Flags card (Admin mock-up, issue 550). A stored flag only tightens
// what the server settings allow, so a choice the server would refuse is not offered: open sign-up
// never (it waits for the beyond-household gate), invite only not while the server says closed, and
// a switch the server has off stays an off switch that can't be pressed. Every write goes through
// the step-up dialog and is audited by the server.

export const SWITCH_KEYS: ReadonlySet<string> = new Set(['experimental_findings', 'ai_insights'])

export interface ControlProps {
  flag: FlagView
  name: string
  /** Runs one write through the step-up dialog, then reloads the flags. */
  write: (action: () => Promise<unknown>) => void
  busy: boolean
  onChanged: () => void
}

const SIGNUP = ['closed', 'invite'] as const

/** Sign-up: Closed | Invite only, as the mock-up draws it. */
export function SignupChoice({ flag, name, write, busy }: ControlProps) {
  const t = useT()
  return (
    <div className="ov-seg" role="group" aria-label={name}>
      {SIGNUP.map((value) => (
        <button
          key={value}
          type="button"
          aria-pressed={flag.effective === value}
          aria-haspopup="dialog"
          data-lock=""
          disabled={busy || (value === 'invite' && flag.environment === 'closed')}
          onClick={() => {
            if (flag.effective !== value) write(() => setFlag(flag.key, value))
          }}
        >
          <Icon name="lock" />
          {t(value === 'closed' ? 'admin.flags.value.closed' : 'admin.flags.value.invite')}
        </button>
      ))}
    </div>
  )
}

/** An on/off flag: "Change…" when the server allows it on, else an off switch. */
export function SwitchControl({ flag, name, busy, onChanged }: ControlProps) {
  const t = useT()
  const groupId = useId()
  const [open, setOpen] = useState(false)
  const [choice, setChoice] = useState<'on' | 'off'>(flag.effective === 'on' ? 'on' : 'off')
  if (flag.environment !== 'on') {
    return <Toggle label={t('admin.flags.offLabel', { name })} checked={false} disabled />
  }
  return (
    <>
      <Button
        lock
        disabled={busy}
        onClick={() => {
          setChoice(flag.effective === 'on' ? 'on' : 'off')
          setOpen(true)
        }}
      >
        {t('admin.flags.change')}
      </Button>
      {open ? (
        <FlagDialog
          title={t('admin.flags.dialog.title', { name })}
          purpose={t('admin.flags.purpose')}
          ready={choice !== flag.effective}
          save={() => setFlag(flag.key, choice)}
          reset={flag.stored ? () => clearFlag(flag.key) : undefined}
          onClose={() => {
            setOpen(false)
          }}
          onDone={() => {
            setOpen(false)
            onChanged()
          }}
        >
          <fieldset className="ov-choice" aria-labelledby={groupId}>
            <legend id={groupId}>{name}</legend>
            {(['on', 'off'] as const).map((value) => (
              <label key={value}>
                <input
                  type="radio"
                  name={flag.key}
                  checked={choice === value}
                  onChange={() => {
                    setChoice(value)
                  }}
                />
                <span>
                  <strong>{t(value === 'on' ? 'admin.flags.value.on' : 'admin.flags.value.off')}</strong>
                </span>
              </label>
            ))}
          </fieldset>
        </FlagDialog>
      ) : null}
    </>
  )
}

const BANNER_MAX = 280

/** The banner: its text and level, or removed. */
export function BannerControl({ flag, name, busy, onChanged }: ControlProps) {
  const t = useT()
  const textId = useId()
  const [open, setOpen] = useState(false)
  const [text, setText] = useState('')
  const [level, setLevel] = useState<BannerLevel>('info')
  const trimmed = text.trim()
  return (
    <>
      <Button
        lock
        disabled={busy}
        onClick={() => {
          setText('')
          setLevel('info')
          setOpen(true)
        }}
      >
        {t('admin.flags.change')}
      </Button>
      {open ? (
        <FlagDialog
          title={t('admin.flags.dialog.title', { name })}
          purpose={t('admin.flags.purpose')}
          ready={trimmed.length > 0 && trimmed.length <= BANNER_MAX}
          save={() => setFlag(flag.key, { text: trimmed, level })}
          reset={flag.stored ? () => clearFlag(flag.key) : undefined}
          resetLabel={t('admin.flags.banner.remove')}
          onClose={() => {
            setOpen(false)
          }}
          onDone={() => {
            setOpen(false)
            onChanged()
          }}
        >
          <div className="field">
            <label htmlFor={textId}>{t('admin.flags.banner.text')}</label>
            <input
              id={textId}
              maxLength={BANNER_MAX}
              value={text}
              onChange={(event) => {
                setText(event.target.value)
              }}
            />
          </div>
          <fieldset className="ov-choice">
            <legend>{t('admin.flags.banner.level')}</legend>
            {(['info', 'warning'] as const).map((value) => (
              <label key={value}>
                <input
                  type="radio"
                  name="banner-level"
                  checked={level === value}
                  onChange={() => {
                    setLevel(value)
                  }}
                />
                <span>
                  <strong>{t(value === 'info' ? 'admin.flags.banner.info' : 'admin.flags.banner.warning')}</strong>
                </span>
              </label>
            ))}
          </fieldset>
        </FlagDialog>
      ) : null}
    </>
  )
}
