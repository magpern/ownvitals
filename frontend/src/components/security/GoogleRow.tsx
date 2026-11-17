import { useCallback, useEffect, useLayoutEffect, useState } from 'react'
import { codeText } from '../../api/codes'
import { getIdentities, startOAuthLink, unlinkIdentity, type Identities } from '../../api/identities'
import { errorMessage, oauthOutcomeMessage, providerName } from '../../api/messages'
import { useT } from '../../i18n'
import { goToProvider } from '../../lib/navigation'
import { useQuery, useRouter } from '../../router/Router'
import { Notice } from '../forms'
import { Cancelled, useProtected } from '../StepUp'
import { Button } from '../ui/Button'

// The Google row of "How you sign in" (docs/design/mockups/src/Security.dc.html; ADR 0003
// sections 1, 5 and 6). A signed-in user links Google here: sign-in with Google never attaches
// itself to an existing account by its email (`oauth.account_exists`). Linking and unlinking are
// on the step-up list, so both run through the shared "Confirm it's you" dialog; the server
// checks again and refuses to remove the last sign-in method, as this row does.
//
// The callback comes back to /account?oauth=<outcome> (App.tsx sends that here with its query):
// the outcome is read once, shown, and removed from the address so a reload does not repeat it.

const PROVIDER = 'google'

type Outcome = { kind: 'success' | 'error'; text: string } | null

function outcomeOf(code: string | null, linked: string): Outcome {
  if (code === null || code === 'reauthed') return null // a step-up came back: nothing to say
  if (code === 'linked') return { kind: 'success', text: linked }
  return { kind: 'error', text: oauthOutcomeMessage(code) } // unknown codes get the generic text
}

export function GoogleRow() {
  const t = useT()
  const protect = useProtected()
  const query = useQuery()
  const { pathname, navigate } = useRouter()
  const [outcome, setOutcome] = useState<Outcome>(() => outcomeOf(query.get('oauth'), t('security.google.linked')))
  const [data, setData] = useState<Identities | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  // A layout effect, not a passive one: the outcome is painted on this commit and the address is
  // cleared before the browser (or a test waiting for the notice) can look again. A passive effect
  // runs later, so the notice could be seen while `?oauth=` was still in the address.
  useLayoutEffect(() => {
    if (query.has('oauth')) navigate(pathname, { replace: true })
  }, [query, pathname, navigate])

  const load = useCallback(
    (signal?: AbortSignal) =>
      getIdentities(signal).then(setData, (caught: unknown) => {
        if (!signal?.aborted) setError(errorMessage(caught))
      }),
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
    setOutcome(null)
    try {
      await task()
    } catch (caught) {
      if (!(caught instanceof Cancelled)) setError(errorMessage(caught))
    } finally {
      setBusy(false)
    }
  }

  const identity = data?.identities.find((row) => row.provider === PROVIDER) ?? null
  // Nothing to show until the list is known, and no row while Google sign-in is off here.
  const shown = data !== null && (identity !== null || data.available.includes(PROVIDER))

  const link = () => {
    void run(async () => {
      goToProvider(await protect(t('security.google.linkPurpose'), () => startOAuthLink(PROVIDER)))
    })
  }
  const unlink = (id: string) => {
    void run(async () => {
      await protect(t('security.google.unlinkPurpose'), () => unlinkIdentity(id))
      await load()
    })
  }
  // The server's rule: an account keeps a password or at least one linked identity.
  const last = identity !== null && data !== null && !data.hasPassword && data.identities.length <= 1

  return (
    <>
      {outcome !== null ? <Notice kind={outcome.kind}>{outcome.text}</Notice> : null}
      {error !== null ? <Notice kind="error">{error}</Notice> : null}
      {shown ? (
        <div className="ov-setting">
          <div className="ov-setting__what">
            <span className="ov-setting__label">{providerName(PROVIDER)}</span>
            <p>
              {identity === null
                ? t('security.google.notLinked')
                : identity.emailAtLink === null
                  ? t('security.google.linkedPlain')
                  : t('security.google.linkedTo', { email: identity.emailAtLink })}
              {last ? ` ${codeText('last_sign_in_method') ?? ''}` : null}
            </p>
          </div>
          {identity === null ? (
            <Button lock disabled={busy} onClick={link}>
              {t('security.google.link')}
            </Button>
          ) : last ? null : (
            <Button
              lock
              disabled={busy}
              onClick={() => {
                unlink(identity.id)
              }}
            >
              {t('security.google.unlink')}
            </Button>
          )}
        </div>
      ) : null}
    </>
  )
}
