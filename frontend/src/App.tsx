import { RequireReady, RequireSignedOut } from './auth/guards'
import { accessOf, isAdmin } from './auth/access'
import { SessionProvider, useSession } from './auth/SessionContext'
import { PlainFrame } from './components/PlainFrame'
import { StepUpProvider } from './components/StepUp'
import { AppFrame } from './components/shell/AppFrame'
import { AppPage } from './pages/appPages'
import { ProviderEmailPage } from './pages/ProviderEmailPage'
import { ResetPage } from './pages/ResetPage'
import { SignInPage } from './pages/SignInPage'
import { SignUpConfirmPage } from './pages/SignUpConfirmPage'
import { SignUpPage } from './pages/SignUpPage'
import { TwoFactorChallengePage } from './pages/TwoFactorChallengePage'
import { TwoFactorSetupPage } from './pages/TwoFactorSetupPage'
import { matchAppRoute, type AppRoute } from './router/appRoutes'
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

/**
 * A screen of the app (src/router/appRoutes.ts): for a signed-in user through the second factor,
 * in the app frame (rail, top bar); anyone else is sent to their own page by the guard, with the
 * plain frame around the waiting text. An admin-only screen does not exist for anyone else.
 */
function AppScreen({ route, path }: { route: AppRoute; path: string }) {
  const { status, session } = useSession()
  const ready = status === 'ready' && accessOf(session) === 'ready'
  if (ready && route.adminOnly === true && !isAdmin(session)) {
    return (
      <PlainFrame>
        <NotFound />
      </PlainFrame>
    )
  }
  const page = (
    <RequireReady>
      <AppPage route={route} />
    </RequireReady>
  )
  return ready ? (
    <AppFrame title={route.title} active={route.nav} path={path}>
      {page}
    </AppFrame>
  ) : (
    <PlainFrame>{page}</PlainFrame>
  )
}

// Guarded: the app screens (signed in and through the second factor), sign-in and sign-up
// (signed out only) and the two second-factor pages (their own step only). The mail-link pages
// stay open: a link is followed in whatever state the browser is in. Everything but the app
// screens is in the plain frame, so the page does not move when the session arrives.
function Routes() {
  const { pathname } = useRouter()
  const path = pathname.replace(/\/+$/, '') || '/'
  const match = matchAppRoute(path)
  if (match !== null) return <AppScreen route={match.route} path={path} />
  return <PlainFrame>{otherPage(path)}</PlainFrame>
}

function otherPage(path: string) {
  switch (path) {
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
        <StepUpProvider>
          <Routes />
        </StepUpProvider>
      </SessionProvider>
    </RouterProvider>
  )
}
