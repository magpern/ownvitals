import { useSession } from '../auth/SessionContext'
import { RequireReady } from '../auth/guards'

function greeting(email: string): string {
  const name = email.split('@')[0] ?? ''
  return name === '' ? 'Welcome' : `Welcome, ${name}`
}

/** Home for a signed-in user: a placeholder until the dashboard lands (PLAN section 8, M6). */
export function HomePage() {
  const { session } = useSession()
  return (
    <RequireReady>
      <section className="page" aria-labelledby="home-title">
        <h1 id="home-title" className="page__title">
          {greeting(session?.user?.email ?? '')}
        </h1>
        <p>The dashboard comes in M6.</p>
      </section>
    </RequireReady>
  )
}
