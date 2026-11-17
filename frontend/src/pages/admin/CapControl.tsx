import { useId, useState } from 'react'
import { clearFlag, setFlag } from '../../api/adminWrites'
import { ApiError } from '../../api/client'
import { Button } from '../../components/ui/Button'
import { useFormat } from '../../i18n/useFormat'
import { isMessageKey, useT } from '../../i18n'
import { microFromFlag, microFromUsd, MICRO_PER_USD, usdDigits } from './capAmount'
import type { ControlProps } from './FlagControls'
import { FlagDialog } from './FlagDialog'

// The AI monthly caps on the Flags card. A stored cap only lowers the server setting of the same
// name, so the dialog takes an amount from 0 up to the server's and no higher; the server also
// keeps the per-user cap at or below the global one and says so when a change would break that.
// Amounts are typed and shown in USD and stored in micro-USD.

export const CAP_KEYS: ReadonlySet<string> = new Set(['ai_user_monthly_cap_microusd', 'ai_global_monthly_cap_microusd'])

/** "10.00 USD" for a cap in micro-USD, in the current language. */
export function useUsd(): (micro: number) => string {
  const t = useT()
  const format = useFormat()
  return (micro) => t('admin.flags.cap.amount', { amount: format.number(micro / MICRO_PER_USD, usdDigits(micro)) })
}

export function CapControl({ flag, name, busy, onChanged }: ControlProps) {
  const t = useT()
  const usd = useUsd()
  const inputId = useId()
  const helpId = useId()
  const [open, setOpen] = useState(false)
  const [typed, setTyped] = useState('')
  const ceiling = microFromFlag(flag.environment) ?? 0
  const current = microFromFlag(flag.effective)
  const amount = microFromUsd(typed)
  const ready = amount !== null && amount <= ceiling && amount !== current
  return (
    <>
      <Button
        lock
        disabled={busy}
        onClick={() => {
          setTyped('')
          setOpen(true)
        }}
      >
        {t('admin.flags.change')}
      </Button>
      {open ? (
        <FlagDialog
          title={t('admin.flags.dialog.title', { name })}
          purpose={t('admin.flags.purpose')}
          ready={ready}
          save={() => setFlag(flag.key, amount ?? 0)}
          reset={flag.stored ? () => clearFlag(flag.key) : undefined}
          onClose={() => {
            setOpen(false)
          }}
          onDone={() => {
            setOpen(false)
            onChanged()
          }}
          describe={(error) => {
            const key = error instanceof ApiError ? `admin.flags.cap.refused.${error.reason ?? ''}` : ''
            return isMessageKey(key) ? t(key) : undefined
          }}
        >
          <div className="field">
            <label htmlFor={inputId}>{t('admin.flags.cap.label')}</label>
            <input
              id={inputId}
              inputMode="decimal"
              autoComplete="off"
              aria-describedby={helpId}
              aria-invalid={typed !== '' && (amount === null || amount > ceiling) ? true : undefined}
              value={typed}
              onChange={(event) => {
                setTyped(event.target.value)
              }}
            />
          </div>
          <p id={helpId} className="ov-small ov-muted">
            {t('admin.flags.cap.limit', { amount: usd(ceiling) })}
          </p>
        </FlagDialog>
      ) : null}
    </>
  )
}
