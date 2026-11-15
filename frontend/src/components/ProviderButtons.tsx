import { useEffect, useState } from 'react'
import { getOAuthProviders, startOAuth } from '../api/auth'
import { providerName } from '../api/messages'
import { useT } from '../i18n'
import { goToProvider } from '../lib/navigation'
import { Notice, useAction } from './forms'
import { GoogleLogo } from './GoogleLogo'

/**
 * "Continue with Google" when Google sign-in is enabled (the only provider). `inviteCode` is the
 * code a first sign-in with Google needs while sign-up is by invite: the sign-up page passes the
 * one it already has, the sign-in page passes none. The button follows Google's branding
 * guidelines: its "G" mark (GoogleLogo, from this origin) and an approved label.
 */
export function ProviderButtons({ returnTo, inviteCode }: { returnTo: string | null; inviteCode?: string }) {
  const t = useT()
  const [providers, setProviders] = useState<string[]>([])
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
        inviteCode: inviteCode?.trim(),
        returnTo: returnTo ?? undefined,
      })
      goToProvider(url)
    })
  }

  return (
    <div className="providers">
      <p className="divider">{t('auth.providers.or')}</p>
      {action.error ? <Notice kind="error">{action.error}</Notice> : null}
      {providers.map((provider) => (
        <button
          key={provider}
          type="button"
          className={provider === 'google' ? 'google-button' : 'button button--secondary'}
          disabled={action.busy}
          onClick={() => {
            start(provider)
          }}
        >
          {provider === 'google' ? <GoogleLogo /> : null}
          <span>{t('auth.providers.continue', { provider: providerName(provider) })}</span>
        </button>
      ))}
    </div>
  )
}
