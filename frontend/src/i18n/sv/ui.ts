import type { ui as en } from '../en/ui'

// Swedish for the ui area (glossary: docs/dev/i18n.md). `satisfies` makes a missing or extra key a type error.
export const ui = {
  comingLater: 'Kommer senare',
  'state.up-to-date': 'Uppdaterad',
  'state.stale': 'Inaktuell',
  'state.importing': 'Importerar',
  'state.importingPercent': 'Importerar {percent} %',
  'state.incomplete-import': 'Ofullständig import',
  'state.insufficient-data': 'För lite data',
  'state.not-supported': 'Stöds inte',
  'state.not-shared': 'Inte delad',
  'state.not-available': 'Inte tillgänglig',
  'state.needs-reconnect': 'Behöver anslutas igen',
  'state.withings-plus': 'Kräver Withings+',
  'bar.importing': 'Importens förlopp',
  'bar.insufficient-data': 'Förlopp mot minimum',
  'tier.device': 'Enhet',
  'tier.trend': 'Trend',
  'tier.experimental': 'Experimentell',
  'tier.system': 'System',
  'source.withings': 'Rapporterat av Withings',
  'source.ownvitals': 'Beräknat av ownvitals',
  'source.estimate': 'Uppskattat av ownvitals',
  estimate: 'ca',
} satisfies Record<keyof typeof en, string>
