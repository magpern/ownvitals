import type { ui as en } from '../en/ui'

// Swedish for the ui area. Until the translation PR (G6) the values are English copies and the
// keys are listed in ../untranslated.ts. `satisfies` makes a missing or extra key a type error.
export const ui = {
  comingLater: 'Coming later',
  'state.up-to-date': 'Up to date',
  'state.stale': 'Stale',
  'state.importing': 'Importing',
  'state.importingPercent': 'Importing {percent}%',
  'state.incomplete-import': 'Incomplete import',
  'state.insufficient-data': 'Insufficient data',
  'state.not-supported': 'Not supported',
  'state.not-shared': 'Not shared',
  'state.not-available': 'Not available',
  'state.needs-reconnect': 'Needs reconnect',
  'state.withings-plus': 'Withings+ needed',
  'bar.importing': 'Import progress',
  'bar.insufficient-data': 'Progress toward the minimum',
  'tier.device': 'Device',
  'tier.trend': 'Trend',
  'tier.experimental': 'Experimental',
  'tier.system': 'System',
  'source.withings': 'Reported by Withings',
  'source.ownvitals': 'Calculated by ownvitals',
  'source.estimate': 'Estimated by ownvitals',
  estimate: 'est.',
} satisfies Record<keyof typeof en, string>
