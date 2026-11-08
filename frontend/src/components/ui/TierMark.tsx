// Who said it: the marks of the four finding tiers (Device, Trend, Experimental, System) and the
// three value styles' source marks (reported by Withings, calculated by ownvitals, estimated by
// ownvitals). Docs/design/mockups/src/Main.dc.html. The glyphs are shared: the source mark of a
// Withings value is the Device square, a calculated value the Trend band, an estimate the
// Experimental diamond.

import type { MessageKey } from '../../i18n/en'
import { useT } from '../../i18n'

export type Tier = 'device' | 'trend' | 'experimental' | 'system'

/** The catalogue key of each tier's name. */
export const TIER_LABELS: Record<Tier, MessageKey> = {
  device: 'ui.tier.device',
  trend: 'ui.tier.trend',
  experimental: 'ui.tier.experimental',
  system: 'ui.tier.system',
}

/** The small shape in front of a tier name or a source line. Decorative. */
export function TierGlyph({ tier }: { tier: Tier }) {
  return (
    <svg viewBox="0 0 12 12" aria-hidden="true" focusable="false">
      {tier === 'device' ? <rect x="2" y="2" width="8" height="8" rx="1.5" className="ov-glyph-solid" /> : null}
      {tier === 'trend' ? (
        <>
          <rect x="0.5" y="3.5" width="11" height="5" rx="2.5" className="ov-glyph-soft" />
          <circle cx="8" cy="6" r="2.2" className="ov-glyph-solid" />
        </>
      ) : null}
      {tier === 'experimental' ? <path d="M6 1.2 L10.8 6 L6 10.8 L1.2 6 Z" className="ov-glyph-dashed" /> : null}
      {tier === 'system' ? <circle cx="6" cy="6" r="4" className="ov-ic" /> : null}
    </svg>
  )
}

/** The badge of a finding's tier: the glyph and the tier's name. */
export function TierMark({ tier }: { tier: Tier }) {
  const t = useT()
  return (
    <span className={`ov-tier ov-tier--${tier}`}>
      <TierGlyph tier={tier} />
      {t(TIER_LABELS[tier])}
    </span>
  )
}

export type Source = 'withings' | 'ownvitals' | 'estimate'

const SOURCES: Record<Source, { tier: Tier; label: MessageKey }> = {
  withings: { tier: 'device', label: 'ui.source.withings' },
  ownvitals: { tier: 'trend', label: 'ui.source.ownvitals' },
  estimate: { tier: 'experimental', label: 'ui.source.estimate' },
}

/**
 * The line above a value that says who produced it: "Reported by Withings · Body Scan",
 * "Calculated by ownvitals · analysis 1.2". `detail` is what follows the dot (device, version).
 */
export function SourceMark({ source, detail }: { source: Source; detail?: string }) {
  const t = useT()
  const { tier, label } = SOURCES[source]
  return (
    <span className={source === 'estimate' ? 'ov-src ov-src--estimate' : 'ov-src'}>
      <TierGlyph tier={tier} />
      {detail === undefined ? t(label) : `${t(label)} · ${detail}`}
    </span>
  )
}
