import type { nerve as en } from '../en/nerve'

// Swedish for the nerve health page (glossary: docs/dev/i18n.md). `satisfies` makes a missing or extra key a type error.
export const nerve = {
  'section.measures': 'Nervmått',
  'section.months': 'Månad för månad',
  'section.findings': 'Fynd om nerverna',
  'foot.title': 'Vänster och höger fot',
  'month.title': 'Månadssammanfattning',
  'month.line': 'median {median}, lägst {low}, högst {high}, {days} dagar med data',
  'month.none': 'Inga mätningar',
  'findings.title': 'Fynd om nerverna',
  'findings.empty': 'Inga fynd om nerverna just nu.',
  'findings.all': 'Alla fynd',
} as const satisfies Record<keyof typeof en, string>
