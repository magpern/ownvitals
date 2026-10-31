import { useEffect, useState } from 'react'
import { getOAuthProviders, startOAuth } from '../api/auth'
import { providerName } from '../api/messages'
import { goToProvider } from '../lib/navigation'
import { Field, Notice, useAction } from './forms'

/**
 * "Continue with ..." buttons for the enabled providers (Google), plus the invite field a first
 * sign-in with a provider may need while sign-up is by invite.
 */
export function ProviderButtons({ returnTo }: { returnTo: string | null }) {
  const [providers, setProviders] = useState<string[]>([])
  const [invite, setInvite] = useState('')
  const action = useAction()

  useEffect(() => {
    const controller = new AbortController()
    getOAuthProviders(controller.signal).then(setProviders, () => undefined)
    return () => {
      controller.abort()
    }
  }, [])

  if (providers.length === 0) return null

  const start = (provider: string) => {
    void action.run(async () => {
      const url = await startOAuth(provider, {
        inviteCode: invite.trim(),
        returnTo: returnTo ?? undefined,
      })
      goToProvider(url)
    })
  }

  return (
    <div className="providers">
      <p className="divider">or</p>
      {action.error ? <Notice kind="error">{action.error}</Notice> : null}
      <Field
        label="Invite code (only for your first sign-in with Google)"
        autoComplete="off"
        value={invite}
        onChange={(e) => {
          setInvite(e.target.value)
        }}
      />
      {providers.map((provider) => (
        <button
          key={provider}
          type="button"
          className="button button--secondary"
          disabled={action.busy}
          onClick={() => {
            start(provider)
          }}
        >
          Continue with {providerName(provider)}
        </button>
      ))}
    </div>
  )
}
