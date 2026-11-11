import { Redirect, RequireReady, RequireSignedOut } from './auth/guards'
import { HOME_PATH, accessOf, isAdmin } from './auth/access'
import { SessionProvider, useSession } from './auth/SessionContext'
import { PlainFrame } from './components/PlainFrame'
import { CookieNotice } from './components/notice/CookieNotice'
import { StepUpProvider } from './components/StepUp'
import { AppFrame } from './components/shell/AppFrame'
import { AppPage } from './pages/appPages'
import { ApproveLoginPage } from './pages/ApproveLoginPage'
import { OnboardingPage } from './pages/OnboardingPage'
import { ProviderEmailPage } from './pages/ProviderEmailPage'
import { ResetPage } from './pages/ResetPage'
import { SignInPage } from './pages/SignInPage'
import { SignUpConfirmPage } from './pages/SignUpConfirmPage'
import { SignUpPage } from './pages/SignUpPage'
import { TwoFactorChallengePage } from './pages/TwoFactorChallengePage'
import { TwoFactorSetupPage } from './pages/TwoFactorSetupPage'
import { matchAppRoute, type AppRoute } from './router/appRoutes'
import { useLocale, useT } from './i18n'
import { Link, RouterProvider, useRouter } from './router/Router'

function NotFound() {
  const t = useT()
  return (
    <section className="card">
      <h1 className="card__title">{t('shell.notFound')}</h1>
      <p>
        <Link to={HOME_PATH}>{t('shell.backToStart')}</Link>
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
  const t = useT()
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
    <AppFrame title={t(route.title)} active={route.nav} path={path}>
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

// An old `/?range=90d` keeps its query.
function RootRedirect() {
  const { search } = useRouter()
  return <Redirect to={`${HOME_PATH}${search}`} />
}

function otherPage(path: string) {
  switch (path) {
    case '/':
      // The public home page owns `/` when the server has it; without it (the dev server) the
      // app's start page is the signed-in one.
      return <RootRedirect />
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
    case '/onboarding':
      return (
        <RequireReady>
          <OnboardingPage />
        </RequireReady>
      )
    case '/qr':
      // The phone's approval of a QR sign-in: it checks the session itself (ADR 0011 section 5).
      return <ApproveLoginPage />
    default:
      return <NotFound />
  }
}

export function App() {
  // The language is read here too, so a change repaints the whole tree.
  useLocale()
  return (
    <RouterProvider>
      <SessionProvider>
        <StepUpProvider>
          <Routes />
          <CookieNotice />
        </StepUpProvider>
      </SessionProvider>
    </RouterProvider>
  )
}
