import { useId } from 'react'

// The legend of the signature chart, in the words of docs/design/mockups/src/Main.dc.html. A
// screen shows it under (or beside) a `TimeSeriesChart`, and leaves out what its series lacks.

export type LegendKey = 'band' | 'smoothed' | 'daily' | 'unconfirmed' | 'gap' | 'change' | 'latest'

const ITEMS: Record<LegendKey, { name: string; text: string }> = {
  band: { name: 'Typical range.', text: 'Your rolling median ± 1.5 MAD over 90 days, by time, not by point count.' },
  smoothed: { name: 'Smoothed line.', text: 'Gap-weighted EWMA. It stops where the data stops.' },
  daily: { name: 'Daily value.', text: 'First morning weigh-in (04:00–11:00), otherwise the median of the day.' },
  unconfirmed: { name: 'Unconfirmed.', text: "The scale couldn't tell who stepped on it. Shown, never used." },
  gap: { name: 'No data.', text: 'Nothing is drawn across a gap, and it is labelled with its length.' },
  change: { name: 'Change point.', text: 'Shown only on series that pass the minimum-data policy.' },
  latest: { name: 'Latest.', text: 'Bigger, labelled. Orange when outside your typical range, never red.' },
}

export const LEGEND_ORDER: LegendKey[] = ['band', 'smoothed', 'daily', 'unconfirmed', 'gap', 'change', 'latest']

function Glyph({ item, hatchId }: { item: LegendKey; hatchId: string }) {
  switch (item) {
    case 'band':
      return <rect x="0" y="2" width="28" height="10" rx="3" className="ov-c-band" />
    case 'smoothed':
      return <path d="M1 10 C 8 2, 18 12, 27 4" className="ov-c-line" />
    case 'daily':
      return <circle cx="14" cy="7" r="3.5" className="ov-c-dots" />
    case 'unconfirmed':
      return <circle cx="14" cy="7" r="4" className="ov-c-hollow" />
    case 'gap':
      return (
        <>
          <defs>
            <pattern id={hatchId} width="5" height="5" patternUnits="userSpaceOnUse" patternTransform="rotate(45)">
              <line x1="0" y1="0" x2="0" y2="5" className="ov-c-hatch" />
            </pattern>
          </defs>
          <rect x="0" y="0" width="28" height="14" rx="3" fill={`url(#${hatchId})`} />
        </>
      )
    case 'change':
      return <line x1="14" y1="0" x2="14" y2="14" className="ov-c-cp" />
    case 'latest':
      return <circle cx="14" cy="7" r="5" className="ov-c-last" />
  }
}

export function ChartLegend({ items = LEGEND_ORDER }: { items?: LegendKey[] }) {
  const id = useId()
  return (
    <ul className="ov-legend">
      {items.map((key) => (
        <li key={key} className="ov-legend__item">
          <svg width="28" height="14" aria-hidden="true" className="ov-legend__glyph">
            <Glyph item={key} hatchId={`${id}-hatch`} />
          </svg>
          <span>
            <strong>{ITEMS[key].name}</strong> <span className="ov-legend__text">{ITEMS[key].text}</span>
          </span>
        </li>
      ))}
    </ul>
  )
}
