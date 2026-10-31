import { RequireSignedOut } from './auth/guards'
import { SessionProvider } from './auth/SessionContext'
import { AppShell } from './components/AppShell'
import { HomePage } from './pages/HomePage'
import { ProviderEmailPage } from './pages/ProviderEmailPage'
import { ResetPage } from './pages/ResetPage'
import { SignInPage } from './pages/SignInPage'
import { SignUpConfirmPage } from './pages/SignUpConfirmPage'
import { SignUpPage } from './pages/SignUpPage'
import { TwoFactorChallengePage } from './pages/TwoFactorChallengePage'
import { TwoFactorSetupPage } from './pages/TwoFactorSetupPage'
import { Link, RouterProvider, useRouter } from './router/Router'

function NotFound() {
  return (
    <section className="card">
      <h1 className="card__title">Page not found</h1>
      <p>
        <Link to="/">Back to the start</Link>
      </p>
    </section>
  )
}

// Guarded: the home page (signed in and through the second factor), sign-in and sign-up (signed
// out only) and the two second-factor pages (their own step only). The mail-link pages stay open:
// a link is followed in whatever state the browser is in.
function Routes() {
  const { pathname } = useRouter()
  const path = pathname.replace(/\/+$/, '') || '/'
  switch (path) {
    case '/':
      return <HomePage />
    case '/signin':
      return (
        <RequireSignedOut>
          <SignInPage />
        </RequireSignedOut>
      )
    case '/signin/email':
      return (
        <RequireSignedOut>
          <ProviderEmailPage />
        </RequireSignedOut>
      )
    case '/signup':
      return (
        <RequireSignedOut>
          <SignUpPage />
        </RequireSignedOut>
      )
    case '/signup/confirm':
      return <SignUpConfirmPage />
    case '/reset':
      return <ResetPage />
    case '/two-factor/setup':
      return <TwoFactorSetupPage />
    case '/two-factor':
      return <TwoFactorChallengePage />
    default:
      return <NotFound />
  }
}

export function App() {
  return (
    <RouterProvider>
      <SessionProvider>
        <AppShell>
          <Routes />
        </AppShell>
      </SessionProvider>
    </RouterProvider>
  )
}
