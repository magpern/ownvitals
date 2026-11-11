import type { Text } from './PublicPages'

// The lively parts of the home page (issue 426): a synthetic dashboard mock-up, a stat strip and
// numbered steps. Plain elements only: the motion is CSS (styles/public-lively.css), the markup is
// final from the first paint, and with reduced motion nothing animates (ADR 0016: no script).

const TILES = ['weight', 'heart', 'sleep'] as const
// The figures are the same in every language, so they are not catalogue text.
const STATS = [
  ['cookies', '0'],
  ['languages', '2'],
  ['dashboard', '1'],
] as const
const STEPS = ['account', 'connect', 'look'] as const

// A made-up series for the mock-up, not a measurement.
const TREND = '0,22 12,20 24,23 36,17 48,19 60,14 72,16 84,11 96,13 108,8 120,10'

export function Mockup({ t }: { t: Text }) {
  return (
    <figure className="pub-mock" role="img" aria-label={t('site.lively.mock.label')}>
      <div aria-hidden="true">
        <p className="pub-mock__badge">{t('site.lively.example')}</p>
        <ul className="pub-mock__tiles">
          {TILES.map((id) => (
            <li key={id}>
              <span className="pub-mock__name">{t(`site.lively.mock.${id}`)}</span>
              <span className="pub-mock__value">{t(`site.lively.mock.${id}.value`)}</span>
              <span className="pub-mock__delta">{t(`site.lively.mock.${id}.delta`)}</span>
            </li>
          ))}
        </ul>
        <p className="pub-mock__name">{t('site.lively.mock.trend')}</p>
        <svg className="pub-mock__chart" viewBox="0 0 120 28" preserveAspectRatio="none" focusable="false">
          <polyline className="pub-mock__band" points="0,14 120,14" />
          <polyline className="pub-mock__line" points={TREND} />
        </svg>
      </div>
    </figure>
  )
}

export function StatStrip({ t }: { t: Text }) {
  return (
    <ul className="pub-stats pub-scroll" aria-label={t('site.lively.stats.label')}>
      {STATS.map(([id, value]) => (
        <li key={id}>
          <span className="pub-stats__value">{value}</span>
          <span className="pub-stats__label">{t(`site.lively.stat.${id}.label`)}</span>
        </li>
      ))}
    </ul>
  )
}

export function Steps({ t }: { t: Text }) {
  return (
    <section aria-labelledby="steps">
      <h2 id="steps">{t('site.lively.steps.title')}</h2>
      <ol className="pub-steps pub-scroll">
        {STEPS.map((id) => (
          <li key={id}>
            <h3>{t(`site.lively.step.${id}.title`)}</h3>
            <p>{t(`site.lively.step.${id}.text`)}</p>
          </li>
        ))}
      </ol>
    </section>
  )
}
