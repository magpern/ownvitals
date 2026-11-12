import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useId,
  useRef,
  useState,
  type FormEvent,
  type KeyboardEvent,
  type ReactNode,
} from 'react'
import { getLinkedProviders, reauthenticate, startOAuthReauth, stepUp } from '../api/auth'
import { ApiError } from '../api/client'
import { errorMessage } from '../api/messages'
import { listPasskeys, passkeysSupported, reauthWithPasskey } from '../api/passkeys'
import { useSession } from '../auth/SessionContext'
import { useT } from '../i18n'
import { useRouter } from '../router/Router'
import { Field, Notice, useAction } from './forms'
import { NO_CODE, SecondFactorField, complete, type SecondFactor } from './SecondFactorField'
import { Button } from './ui/Button'
import { Icon } from './ui/Icon'

// The shared "Confirm it's you" dialog (docs/design/mockups/src/Security.dc.html; ADR 0003
// section 4). Every action on the step-up list runs through `useProtected()`: when the server
// answers 403 `step_up_required` the dialog asks for a fresh authenticator or recovery code, and
// for `reauth_required` it asks for the password first (or Google, when linked), then the code,
// in the same dialog. After each proof the action is tried again. Cancel ends it with
// `Cancelled`, and the caller shows nothing. An account without an authenticator app (optional
// for users, issue 475) steps up with the password, Google or a passkey alone: the server counts that
// first factor as the step-up, so the dialog asks for it on `step_up_required` too. A passkey is
// offered next to the password whenever the account has one.

export const STEP_UP_REQUIRED = 'step_up_required'
export const REAUTH_REQUIRED = 'reauth_required'

/** The user closed the dialog: the action was not done, and there is nothing to report. */
export class Cancelled extends Error {
  constructor() {
    super('cancelled')
    this.name = 'Cancelled'
  }
}

type Step = 'password' | 'code'

interface Request {
  step: Step
  /** "Signing out everywhere": what the dialog says needs the proof. */
  purpose: string
  done: () => void
  cancel: () => void
}

type Protect = <T>(purpose: string, action: () => Promise<T>) => Promise<T>

const ProtectContext = createContext<Protect | null>(null)

/** How many proofs one action may ask for (the password, then the code). */
const MAX_PROOFS = 3

function proofFor(error: unknown, totpEnabled: boolean): Step | null {
  if (!(error instanceof ApiError) || error.status !== 403) return null
  if (error.reason === STEP_UP_REQUIRED) return totpEnabled ? 'code' : 'password'
  if (error.reason === REAUTH_REQUIRED) return 'password'
  return null
}

export function StepUpProvider({ children }: { children: ReactNode }) {
  const [request, setRequest] = useState<Request | null>(null)
  const opener = useRef<HTMLElement | null>(null)
  const { session } = useSession()
  // Read when a refusal comes in, so an enrolment or removal meanwhile is seen.
  const totpEnabled = useRef(true)
  useEffect(() => {
    totpEnabled.current = session?.totpEnabled ?? true
  }, [session])

  const ask = useCallback(
    (step: Step, purpose: string) =>
      new Promise<void>((resolve, reject) => {
        setRequest({
          step,
          purpose,
          done: resolve,
          cancel: () => {
            reject(new Cancelled())
          },
        })
      }),
    [],
  )

  const protect: Protect = useCallback(
    async <T,>(purpose: string, action: () => Promise<T>): Promise<T> => {
      if (document.activeElement instanceof HTMLElement) opener.current = document.activeElement
      try {
        for (let proofs = 0; ; proofs += 1) {
          try {
            return await action()
          } catch (error) {
            const step = proofFor(error, totpEnabled.current)
            if (step === null || proofs >= MAX_PROOFS) throw error
            await ask(step, purpose)
          }
        }
      } finally {
        // One dialog for the whole action: it closes only when the action is over.
        setRequest(null)
        opener.current?.focus()
      }
    },
    [ask],
  )

  return (
    <ProtectContext.Provider value={protect}>
      {children}
      {request !== null ? (
        <StepUpDialog key={request.step} request={request} withCode={session?.totpEnabled ?? true} />
      ) : null}
    </ProtectContext.Provider>
  )
}

/** Runs an action of the step-up list, asking for the proofs the server wants. */
export function useProtected(): Protect {
  const protect = useContext(ProtectContext)
  if (protect === null) throw new Error('useProtected needs a StepUpProvider')
  return protect
}

function StepUpDialog({ request, withCode }: { request: Request; withCode: boolean }) {
  const t = useT()
  const titleId = useId()
  const textId = useId()
  const action = useAction()
  const [password, setPassword] = useState('')
  const [factor, setFactor] = useState<SecondFactor>(NO_CODE)
  const [google, setGoogle] = useState(false)
  const [passkey, setPasskey] = useState(false)
  const { pathname, search } = useRouter()

  useEffect(() => {
    if (request.step !== 'password') return
    const controller = new AbortController()
    getLinkedProviders(controller.signal).then(
      (providers) => {
        setGoogle(providers.includes('google'))
      },
      () => {
        setGoogle(false)
      },
    )
    if (passkeysSupported()) {
      listPasskeys(controller.signal).then(
        (found) => {
          setPasskey(found.passkeys.some((row) => !row.suspended))
        },
        () => {
          setPasskey(false)
        },
      )
    }
    return () => {
      controller.abort()
    }
  }, [request.step])

  const submit = (event: FormEvent) => {
    event.preventDefault()
    void action.run(async () => {
      if (request.step === 'password') await reauthenticate(password)
      else await stepUp(factor.code.trim())
      request.done()
    }, errorMessage)
  }

  const confirmWithPasskey = () => {
    void action.run(async () => {
      await reauthWithPasskey()
      request.done()
    }, errorMessage)
  }

  const reauthWithGoogle = () => {
    void action.run(async () => {
      window.location.assign(await startOAuthReauth('google', pathname + search))
    })
  }

  const onKey = (event: KeyboardEvent) => {
    if (event.key === 'Escape') {
      event.preventDefault()
      request.cancel()
    }
  }

  const ready = request.step === 'password' ? password !== '' : complete(factor)
  return (
    <div className="ov-scrim" onKeyDown={onKey}>
      <form
        className="ov-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        aria-describedby={textId}
        onSubmit={submit}
      >
        <span className="ov-dialog__icon">
          <Icon name="lock" size={40} />
        </span>
        <h2 id={titleId} className="ov-dialog__title">
          {t('security.stepUp.title')}
        </h2>
        {request.step === 'password' ? (
          <p id={textId} className="ov-small ov-muted">
            {withCode
              ? t('security.stepUp.password', { purpose: request.purpose })
              : t('security.stepUp.passwordOnly', { purpose: request.purpose })}
          </p>
        ) : (
          <p id={textId} className="ov-small ov-muted">
            {t('security.stepUp.code', { purpose: request.purpose })}
          </p>
        )}
        {action.error ? <Notice kind="error">{action.error}</Notice> : null}
        {request.step === 'password' ? (
          <>
            <Field
              label={t('common.password')}
              type="password"
              autoComplete="current-password"
              autoFocus
              value={password}
              onChange={(event) => {
                setPassword(event.target.value)
              }}
            />
            {passkey ? (
              <button className="ov-linkbutton" type="button" disabled={action.busy} onClick={confirmWithPasskey}>
                {t('security.stepUp.passkey')}
              </button>
            ) : null}
            {google ? (
              <button className="ov-linkbutton" type="button" disabled={action.busy} onClick={reauthWithGoogle}>
                {t('security.stepUp.google')}
              </button>
            ) : null}
          </>
        ) : (
          <SecondFactorField value={factor} onChange={setFactor} autoFocus disabled={action.busy} />
        )}
        <div className="ov-dialog__actions">
          <Button variant="ghost" onClick={request.cancel}>
            {t('common.cancel')}
          </Button>
          <Button variant="primary" type="submit" disabled={action.busy || !ready}>
            {t('common.confirm')}
          </Button>
        </div>
      </form>
    </div>
  )
}
