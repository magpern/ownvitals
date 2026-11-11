import type { ReactNode } from 'react'
import type { MessageKey } from '../i18n/en'
import { LOCALES, LOCALE_NAMES, type Locale } from '../i18n/locale'
import { PUBLIC_IMAGES, imageUrl } from './images'
import { APP_LINKS, publicPath, type PublicPage } from './routes'

// The body of the public pages: plain elements and ordinary links, no script, no `style`
// attribute (ADR 0016 section 5). Text comes from the catalogue through `t`.

export type Text = (key: MessageKey) => string

const BRAND = 'ownvitals'

const POINTS = ['trends', 'findings', 'yours'] as const
const FEATURES = ['overview', 'body', 'heart', 'sleep', 'nerve', 'findings', 'connections', 'assistant', 'data', 'security', 'languages'] as const

interface Props {
  page: PublicPage
  locale: Locale
  t: Text
}

function Header({ page, locale, t }: Props) {
  const current = (target: PublicPage) => (target === page ? 'page' : undefined)
  return (
    <header className="pub-header">
      <a className="pub-brand" href={publicPath('home', locale)}>
        {BRAND}
      </a>
      <nav className="pub-nav" aria-label={t('site.nav.label')}>
        <a href={publicPath('home', locale)} aria-current={current('home')}>
          {t('site.nav.home')}
        </a>
        <a href={publicPath('features', locale)} aria-current={current('features')}>
          {t('site.nav.features')}
        </a>
        <a href={APP_LINKS.signIn}>{t('site.cta.signIn')}</a>
      </nav>
    </header>
  )
}

function Footer({ page, locale, t }: Props) {
  return (
    <footer className="pub-footer">
      <p>{t('site.footer.disclaimer')}</p>
      <p>{t('site.footer.nocookies')}</p>
      <nav aria-label={t('site.nav.language')}>
        {LOCALES.map((code) => (
          <a key={code} href={publicPath(page, code)} lang={code} hrefLang={code} aria-current={code === locale ? 'true' : undefined}>
            {LOCALE_NAMES[code]}
          </a>
        ))}
      </nav>
    </footer>
  )
}

function Actions({ t }: { t: Text }) {
  return (
    <p className="pub-actions">
      <a className="pub-button pub-button--primary" href={APP_LINKS.signUp}>
        {t('site.cta.signUp')}
      </a>
      <a className="pub-button" href={APP_LINKS.signIn}>
        {t('site.cta.signIn')}
      </a>
    </p>
  )
}

function Home({ locale, t }: Omit<Props, 'page'>) {
  return (
    <>
      <section className="pub-hero">
        <h1>{t('site.home.title')}</h1>
        <p className="pub-lead">{t('site.home.lead')}</p>
        <Actions t={t} />
      </section>
      <section aria-labelledby="points">
        <h2 id="points">{t('site.home.points.title')}</h2>
        <ul className="pub-cards">
          {POINTS.map((id) => (
            <li key={id}>
              <h3>{t(`site.home.point.${id}.title`)}</h3>
              <p>{t(`site.home.point.${id}.text`)}</p>
            </li>
          ))}
        </ul>
        <p>
          <a href={publicPath('features', locale)}>{t('site.cta.features')}</a>
        </p>
      </section>
      <section aria-labelledby="shots">
        <h2 id="shots">{t('site.home.shots.title')}</h2>
        <p className="pub-note">{t('site.home.shots.note')}</p>
        {PUBLIC_IMAGES.map((image) => (
          <figure key={image.id} className="pub-shot">
            <picture>
              <source media="(prefers-color-scheme: dark)" srcSet={imageUrl(image.dark)} />
              <img src={imageUrl(image.light)} width={image.width} height={image.height} alt={t(image.alt)} loading="lazy" decoding="async" />
            </picture>
          </figure>
        ))}
      </section>
      <section className="pub-hero" aria-labelledby="end">
        <h2 id="end">{t('site.home.end.title')}</h2>
        <p className="pub-lead">{t('site.home.end.text')}</p>
        <Actions t={t} />
      </section>
    </>
  )
}

function Features({ t }: { t: Text }) {
  return (
    <>
      <section className="pub-hero">
        <h1>{t('site.features.title')}</h1>
        <p className="pub-lead">{t('site.features.lead')}</p>
      </section>
      <section aria-label={t('site.features.title')}>
        <ul className="pub-cards">
          {FEATURES.map((id) => (
            <li key={id}>
              <h2>{t(`site.features.${id}.title`)}</h2>
              <p>{t(`site.features.${id}.text`)}</p>
            </li>
          ))}
        </ul>
      </section>
      <section className="pub-hero" aria-label={t('site.home.end.title')}>
        <Actions t={t} />
      </section>
    </>
  )
}

export function PublicBody({ page, locale, t }: Props): ReactNode {
  return (
    <>
      <a className="pub-skip" href="#content">
        {t('site.skip')}
      </a>
      <Header page={page} locale={locale} t={t} />
      <main id="content" className="pub-main">
        {page === 'home' ? <Home locale={locale} t={t} /> : <Features t={t} />}
      </main>
      <Footer page={page} locale={locale} t={t} />
    </>
  )
}
