import { useState } from 'react'
import { leave, startWithingsConnect } from '../../api/connections'
import { errorMessage } from '../../api/messages'
import { useT } from '../../i18n'
import { Notice } from '../forms'
import { Cancelled, useProtected } from '../StepUp'
import { Button, type ButtonVariant } from '../ui/Button'

// Connect (or reconnect) Withings behind the shared "Confirm it's you" dialog: the server wants a
// fresh authenticator code before it hands over the Withings address (ADR 0004 section 7). The
// browser then leaves for Withings; the callback brings it back to /onboarding.

export function ConnectButton({
  label,
  variant = 'primary',
  wide = false,
}: {
  label: string
  variant?: ButtonVariant
  wide?: boolean
}) {
  const t = useT()
  const protect = useProtected()
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const connect = async () => {
    setBusy(true)
    setError(null)
    try {
      const url = await protect(t('connections.connect.purpose'), startWithingsConnect)
      leave.go(url)
    } catch (caught) {
      if (!(caught instanceof Cancelled)) setError(errorMessage(caught))
      setBusy(false)
    }
  }

  return (
    <>
      <Button variant={variant} wide={wide} lock disabled={busy} onClick={() => void connect()}>
        {label}
      </Button>
      {error !== null ? <Notice kind="error">{error}</Notice> : null}
    </>
  )
}
