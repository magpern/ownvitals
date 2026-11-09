import { useCallback, useEffect, useId, useState, type FormEvent } from 'react'
import { ApiError } from '../api/client'
import { errorMessage } from '../api/messages'
import {
  PasskeyCancelled,
  createPasskey,
  listPasskeys,
  passkeysSupported,
  registerPasskey,
  registrationOptions,
  removePasskey,
  renamePasskey,
  type Passkey,
  type Passkeys,
} from '../api/passkeys'
import { plural, useT } from '../i18n'
import { useFormat } from '../i18n/useFormat'
import { INTL_TAG } from '../i18n/locale'
import { Notice } from './forms'
import { Cancelled, useProtected } from './StepUp'
import { Button } from './ui/Button'
import { StatePill } from './ui/DataState'

// The passkey rows of Settings → Sign-in and security (the "Passkeys" row of the Security mock-up,
// ADR 0011 sections 2, 8 and 10): the list with its dates, "synced" and a suspended passkey, rename
// (a label: no proof), and add and remove behind the lock, which run through the shared "Confirm
// it's you" dialog (`useProtected`: recent authentication, then a step-up) and are tried again
// after it.

/** The sentence for a failed action; nothing for a dialog the user closed. */
export function passkeyError(error: unknown, t: ReturnType<typeof useT>): string | null {
  if (error instanceof Cancelled) return null
  if (error instanceof PasskeyCancelled) return t(error.why === 'exists' ? 'passkeys.exists' : 'passkeys.cancelled')
  return errorMessage(error)
}

export function PasskeyList() {
  const t = useT()
  const listId = useId()
  const protect = useProtected()
  const [data, setData] = useState<Passkeys | null>(null)
  const [unavailable, setUnavailable] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [editing, setEditing] = useState<{ id: string; name: string } | null>(null)
  const supported = passkeysSupported()

  const load = useCallback(
    (signal?: AbortSignal) =>
      listPasskeys(signal).then(
        (found) => {
          setData(found)
          setUnavailable(null)
        },
        (caught: unknown) => {
          if (signal?.aborted) return
          // No relying party (an IP-address origin): the feature is off, not broken.
          if (caught instanceof ApiError && caught.status === 404) setUnavailable(errorMessage(caught))
          else setError(errorMessage(caught))
        },
      ),
    [],
  )

  useEffect(() => {
    const controller = new AbortController()
    void load(controller.signal)
    return () => {
      controller.abort()
    }
  }, [load])

  const run = async (task: () => Promise<void>) => {
    setBusy(true)
    setError(null)
    try {
      await task()
    } catch (caught) {
      setError(passkeyError(caught, t))
    } finally {
      setBusy(false)
    }
  }

  const add = () => {
    void run(async () => {
      const purpose = t('passkeys.addPurpose')
      const options = await protect(purpose, registrationOptions)
      const credential = await createPasskey(options)
      await protect(purpose, () => registerPasskey(credential))
      await load()
    })
  }

  const remove = (passkey: Passkey) => {
    void run(async () => {
      await protect(t('passkeys.removePurpose'), () => removePasskey(passkey.id))
      await load()
    })
  }

  const rename = (event: FormEvent) => {
    event.preventDefault()
    if (editing === null) return
    const { id, name } = editing
    void run(async () => {
      await renamePasskey(id, name.trim())
      setEditing(null)
      await load()
    })
  }

  const full = data !== null && data.passkeys.length >= data.limit
  return (
    <>
      <div className="ov-setting">
        <div className="ov-setting__what">
          <span className="ov-setting__label">{t('passkeys.row.label')}</span>
          <p>{t('passkeys.row.text')}</p>
        </div>
        {unavailable === null && supported ? (
          <Button lock disabled={busy || data === null || full} onClick={add}>
            {t('passkeys.add')}
          </Button>
        ) : null}
      </div>
      {unavailable !== null ? <Notice>{unavailable}</Notice> : null}
      {unavailable === null && !supported ? <Notice>{t('passkeys.unsupported')}</Notice> : null}
      {error !== null ? <Notice kind="error">{error}</Notice> : null}
      {data === null ? null : data.passkeys.length === 0 ? (
        <p className="ov-small ov-muted">{t('passkeys.none')}</p>
      ) : (
        <ul id={listId} className="ov-settings-list" aria-label={t('passkeys.listLabel')}>
          {data.passkeys.map((passkey) =>
            editing?.id === passkey.id ? (
              <li key={passkey.id} className="ov-setting">
                <RenameForm
                  value={editing.name}
                  busy={busy}
                  onChange={(name) => {
                    setEditing({ id: passkey.id, name })
                  }}
                  onSubmit={rename}
                  onCancel={() => {
                    setEditing(null)
                  }}
                />
              </li>
            ) : (
              <PasskeyRow
                key={passkey.id}
                passkey={passkey}
                busy={busy}
                onRename={() => {
                  setError(null)
                  setEditing({ id: passkey.id, name: passkey.name })
                }}
                onRemove={() => {
                  remove(passkey)
                }}
              />
            ),
          )}
        </ul>
      )}
      {full ? <p className="ov-small ov-muted">{plural('passkeys.limit', data.limit)}</p> : null}
    </>
  )
}

function PasskeyRow({
  passkey,
  busy,
  onRename,
  onRemove,
}: {
  passkey: Passkey
  busy: boolean
  onRename: () => void
  onRemove: () => void
}) {
  const t = useT()
  const format = useFormat()
  const zone = format.browserZone()
  // "3 Mar 2026", as the mock-up dates the authenticator: a passkey may be years old.
  const dates = new Intl.DateTimeFormat(INTL_TAG[format.locale], { timeZone: zone, day: 'numeric', month: 'short', year: 'numeric' })
  const day = (iso: string) => dates.format(new Date(iso))
  const facts = [
    t('passkeys.added', { date: day(passkey.createdAt) }),
    passkey.lastUsedAt === null ? t('passkeys.neverUsed') : t('passkeys.lastUsed', { date: day(passkey.lastUsedAt) }),
  ]
  if (passkey.synced) facts.push(t('passkeys.synced'))
  return (
    <li className="ov-setting">
      <div className="ov-setting__what">
        <span className="ov-setting__label">{passkey.name}</span>{' '}
        {passkey.suspended ? <StatePill tone="attn">{t('passkeys.suspended')}</StatePill> : null}
        <p>{facts.join(' · ')}</p>
        {passkey.suspended ? <p>{t('passkeys.suspendedText')}</p> : null}
      </div>
      <div className="ov-settings-actions">
        <Button
          variant="ghost"
          disabled={busy}
          aria-label={t('passkeys.renameAction', { name: passkey.name })}
          onClick={onRename}
        >
          {t('passkeys.rename')}
        </Button>
        <Button
          variant="danger"
          lock
          disabled={busy}
          aria-label={t('passkeys.removeAction', { name: passkey.name })}
          onClick={onRemove}
        >
          {t('passkeys.remove')}
        </Button>
      </div>
    </li>
  )
}

function RenameForm({
  value,
  busy,
  onChange,
  onSubmit,
  onCancel,
}: {
  value: string
  busy: boolean
  onChange: (value: string) => void
  onSubmit: (event: FormEvent) => void
  onCancel: () => void
}) {
  const t = useT()
  const id = useId()
  const ready = value.trim() !== '' && value.trim().length <= 64
  return (
    <form className="ov-settings-actions" onSubmit={onSubmit}>
      <label className="ov-sr" htmlFor={id}>
        {t('passkeys.renameLabel')}
      </label>
      <input
        id={id}
        className="ov-input"
        maxLength={64}
        autoComplete="off"
        autoFocus
        value={value}
        onChange={(event) => {
          onChange(event.target.value)
        }}
      />
      <Button variant="primary" type="submit" disabled={busy || !ready}>
        {t('passkeys.save')}
      </Button>
      <Button variant="ghost" disabled={busy} onClick={onCancel}>
        {t('common.cancel')}
      </Button>
    </form>
  )
}
