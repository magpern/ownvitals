import { useCallback, useId, useRef, useState, type InputHTMLAttributes, type ReactNode } from 'react'
import { errorMessage } from '../api/messages'

interface FieldProps extends Omit<InputHTMLAttributes<HTMLInputElement>, 'id'> {
  label: string
  hint?: string
}

export function Field({ label, hint, ...input }: FieldProps) {
  const id = useId()
  const hintId = `${id}-hint`
  return (
    <div className="field">
      <label htmlFor={id}>{label}</label>
      <input id={id} aria-describedby={hint ? hintId : undefined} {...input} />
      {hint ? (
        <p id={hintId} className="field__hint">
          {hint}
        </p>
      ) : null}
    </div>
  )
}

export function Notice({
  kind = 'info',
  children,
}: {
  kind?: 'info' | 'success' | 'error'
  children: ReactNode
}) {
  return (
    <p className="notice" data-kind={kind} role={kind === 'error' ? 'alert' : 'status'}>
      {children}
    </p>
  )
}

/** The narrow, centred card of the sign-in, sign-up, mail-link and two-factor pages. */
export function Card({ title, children }: { title: string; children: ReactNode }) {
  const id = useId()
  return (
    <section className="card" aria-labelledby={id}>
      <h1 id={id} className="card__title">
        {title}
      </h1>
      {children}
    </section>
  )
}

interface Action {
  busy: boolean
  error: string | null
  clear: () => void
  /**
   * Runs `task`; resolves to its value, or `undefined` after an error.
   * `describe` turns an error into its text (default `errorMessage`).
   */
  run: <T>(task: () => Promise<T>, describe?: (error: unknown) => string) => Promise<T | undefined>
}

/** Busy flag and error text around an async action. Only the latest call may set state. */
export function useAction(): Action {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const latest = useRef(0)

  const run = useCallback(
    async <T,>(
      task: () => Promise<T>,
      describe: (error: unknown) => string = errorMessage,
    ): Promise<T | undefined> => {
      const mine = ++latest.current
      setBusy(true)
      setError(null)
      try {
        return await task()
      } catch (caught) {
        if (mine === latest.current) setError(describe(caught))
        return undefined
      } finally {
        if (mine === latest.current) setBusy(false)
      }
    },
    [],
  )

  const clear = useCallback(() => {
    setError(null)
  }, [])
  return { busy, error, clear, run }
}
