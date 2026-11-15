// The body composition page (issue 201; docs/design/README.md, "Body composition"). The metric
// names are `detail.metric.*`; the states are `overview.tile.detail.*`. sv/body.ts satisfies this shape.
export const body = {
  'section.composition': 'Body composition',
  'section.segments': 'Arms and legs',
  'section.findings': 'Body findings',
  'segment.title': 'Muscle mass by arm and leg',
  'segment.left_arm': 'Left arm',
  'segment.right_arm': 'Right arm',
  'segment.left_leg': 'Left leg',
  'segment.right_leg': 'Right leg',
  'segment.arms': 'arms',
  'segment.legs': 'legs',
  'segment.difference': 'Left and right {pair} differ by {percent} %.',
  'segment.noDifference': 'Left and right {pair}: not enough values to compare.',
  'findings.title': 'Body findings',
  'findings.empty': 'No body findings right now.',
  'findings.all': 'All findings',
} as const
