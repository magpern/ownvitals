// The icons of the design (docs/design/mockups/src/Rail.dc.html and Main.dc.html): outline paths
// drawn with the stroke of `.ov-ic`, so they take the text colour. Always decorative: the label
// next to an icon says what it is.

const ICONS = {
  overview: { box: 24, d: 'M4 4h7v7H4z M13 4h7v7h-7z M4 13h7v7H4z M13 13h7v7h-7z' },
  body: { box: 24, d: 'M14.5 5.5a2.5 2.5 0 1 1-5 0a2.5 2.5 0 1 1 5 0z M6 10.5h12 M12 10.5v5 M8.5 21l3.5-5.5 3.5 5.5' },
  heart: { box: 24, d: 'M2 12h4l2-5 4 10 3-7 2 2h5' },
  nerve: { box: 24, d: 'M12 3v6 M12 9l-5 5 M12 9l5 5 M7 14v6 M17 14v6 M12 9v11' },
  sleep: { box: 24, d: 'M20 14.5A8 8 0 1 1 9.5 4a6.5 6.5 0 0 0 10.5 10.5z' },
  activity: { box: 24, d: 'M13 2L4 14h7l-1 8 9-12h-7z' },
  findings: { box: 24, d: 'M4 13l2.5-8h11L20 13 M4 13v6h16v-6 M4 13h5l1 2h4l1-2h5' },
  connections: {
    box: 24,
    d: 'M9 15l6-6 M10.5 6.5l1.8-1.8a4 4 0 0 1 5.7 5.7l-1.8 1.8 M13.5 17.5l-1.8 1.8a4 4 0 0 1-5.7-5.7l1.8-1.8',
  },
  settings: { box: 24, d: 'M4 7h10 M18 7h2 M4 17h4 M12 17h8 M16 5v4 M10 15v4' },
  security: { box: 24, d: 'M6 10.5h12v9.5H6z M8.5 10.5V8a3.5 3.5 0 0 1 7 0v2.5 M12 14v2.5' },
  admin: { box: 24, d: 'M12 3l7 3v5c0 5-3.5 8.5-7 10-3.5-1.5-7-5-7-10V6z M9 12l2 2 4-4' },
  signout: { box: 24, d: 'M15 4h4v16h-4 M10 8l-4 4 4 4 M6 12h10' },
  // The lock of a button for an action that asks for a fresh authenticator code (step-up).
  lock: { box: 20, d: 'M6 9h8a2 2 0 0 1 2 2v4a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2v-4a2 2 0 0 1 2-2z M7 9V6.5a3 3 0 0 1 6 0V9' },
  'chevron-down': { box: 20, d: 'M5 8l5 5 5-5' },
  'chevron-up': { box: 20, d: 'M5 12l5-5 5 5' },
} as const

export type IconName = keyof typeof ICONS

export function Icon({ name, size }: { name: IconName; size?: number }) {
  const { box, d } = ICONS[name]
  return (
    <svg className="ov-icon" viewBox={`0 0 ${String(box)} ${String(box)}`} width={size} height={size} aria-hidden="true" focusable="false">
      <path d={d} className="ov-ic" />
    </svg>
  )
}

/** The logo: a reading (the dot) on a personal typical range (the band). */
export function Logo({ size = 26 }: { size?: number }) {
  return (
    <svg className="ov-icon" viewBox="0 0 24 24" width={size} height={size} aria-hidden="true" focusable="false">
      <rect x="1" y="1" width="22" height="22" rx="6" className="ov-logo-bg" />
      <rect x="4" y="10" width="16" height="5" rx="2.5" className="ov-logo-band" />
      <circle cx="15" cy="12.5" r="3" className="ov-logo-dot" />
    </svg>
  )
}
