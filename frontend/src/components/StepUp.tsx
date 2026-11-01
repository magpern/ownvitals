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
// `Cancelled`, and the caller shows nothing.

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

function proofFor(error: unknown): Step | null {
  if (!(error instanceof ApiError) || error.status !== 403) return null
  if (error.reason === STEP_UP_REQUIRED) return 'code'
  if (error.reason === REAUTH_REQUIRED) return 'password'
  return null
}

export function StepUpProvider({ children }: { children: ReactNode }) {
  const [request, setRequest] = useState<Request | null>(null)
  const opener = useRef<HTMLElement | null>(null)

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
            const step = proofFor(error)
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
      {request !== null ? <StepUpDialog key={request.step} request={request} /> : null}
    </ProtectContext.Provider>
  )
}

/** Runs an action of the step-up list, asking for the proofs the server wants. */
export function useProtected(): Protect {
  const protect = useContext(ProtectContext)
  if (protect === null) throw new Error('useProtected needs a StepUpProvider')
  return protect
}

function StepUpDialog({ request }: { request: Request }) {
  const titleId = useId()
  const textId = useId()
  const action = useAction()
  const [password, setPassword] = useState('')
  const [factor, setFactor] = useState<SecondFactor>(NO_CODE)
  const [google, setGoogle] = useState(false)
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
          Confirm it&apos;s you
        </h2>
        {request.step === 'password' ? (
          <p id={textId} className="ov-small ov-muted">
            {request.purpose} needs your password, then a fresh code from your authenticator app.
          </p>
        ) : (
          <p id={textId} className="ov-small ov-muted">
            {request.purpose} needs a fresh code from your authenticator app. It also covers other
            protected actions for the next 10 minutes.
          </p>
        )}
        {action.error ? <Notice kind="error">{action.error}</Notice> : null}
        {request.step === 'password' ? (
          <>
            <Field
              label="Password"
              type="password"
              autoComplete="current-password"
              autoFocus
              value={password}
              onChange={(event) => {
                setPassword(event.target.value)
              }}
            />
            {google ? (
              <button className="ov-linkbutton" type="button" disabled={action.busy} onClick={reauthWithGoogle}>
                Use Google instead
              </button>
            ) : null}
          </>
        ) : (
          <SecondFactorField value={factor} onChange={setFactor} autoFocus disabled={action.busy} />
        )}
        <div className="ov-dialog__actions">
          <Button variant="ghost" onClick={request.cancel}>
            Cancel
          </Button>
          <Button variant="primary" type="submit" disabled={action.busy || !ready}>
            Confirm
          </Button>
        </div>
      </form>
    </div>
  )
}
