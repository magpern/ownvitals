// The nerve health page (issue 202; docs/design/README.md, "Nerve health"). The metric names are
// `detail.metric.*`; the states are `overview.tile.detail.*`. sv/nerve.ts satisfies this shape.
export const nerve = {
  'section.measures': 'Nerve measures',
  'section.months': 'Month by month',
  'section.findings': 'Nerve findings',
  'foot.title': 'Left and right foot',
  'month.title': 'Monthly summary',
  'month.line': 'median {median}, lowest {low}, highest {high}, {days} days with data',
  'month.none': 'No readings',
  'findings.title': 'Nerve findings',
  'findings.empty': 'No nerve findings right now.',
  'findings.all': 'All findings',
} as const
