import type { body as en } from '../en/body'

// Swedish for the body composition page (glossary: docs/dev/i18n.md). `satisfies` makes a missing or extra key a type error.
export const body = {
  'section.composition': 'Kroppssammansättning',
  'section.segments': 'Armar och ben',
  'section.findings': 'Fynd om kroppen',
  'segment.title': 'Muskelmassa i armar och ben',
  'segment.left_arm': 'Vänster arm',
  'segment.right_arm': 'Höger arm',
  'segment.left_leg': 'Vänster ben',
  'segment.right_leg': 'Höger ben',
  'segment.arms': 'armarna',
  'segment.legs': 'benen',
  'segment.difference': 'Vänster och höger i {pair} skiljer sig med {percent} %.',
  'segment.noDifference': 'Vänster och höger i {pair}: för få värden att jämföra.',
  'findings.title': 'Fynd om kroppen',
  'findings.empty': 'Inga fynd om kroppen just nu.',
  'findings.all': 'Alla fynd',
} as const satisfies Record<keyof typeof en, string>
