import type { MessageKey } from '../i18n/en'

// The only images the public pages may show (ADR 0016 section 4): the owner-approved synthetic
// mock-ups of docs/design/mockups/png/, copied by the build to `/img/<name>`. A page names an
// image by id; a test holds the list fixed.

export interface PublicImage {
  id: 'dashboard' | 'findings' | 'connections'
  /** The file in docs/design/mockups/png/ for the light theme and for the dark one. */
  light: string
  dark: string
  width: number
  height: number
  alt: MessageKey
}

export const PUBLIC_IMAGES: readonly PublicImage[] = [
  { id: 'dashboard', light: 'Dashboard.png', dark: 'Dashboard.dark.png', width: 1440, height: 1500, alt: 'site.shot.dashboard.alt' },
  { id: 'findings', light: 'Insights.png', dark: 'Insights.dark.png', width: 1440, height: 1300, alt: 'site.shot.findings.alt' },
  { id: 'connections', light: 'Connections.png', dark: 'Connections.dark.png', width: 1440, height: 1650, alt: 'site.shot.connections.alt' },
]

/** The mock-up file in docs/design/mockups/png/ a served name is copied from. */
export function sourceFile(file: string): string {
  return file.startsWith('Insights') ? file.replace('Insights', 'Findings') : file
}

/** The URL path a build serves an image file at. */
export function imageUrl(file: string): string {
  return `/img/${file}`
}

/** The file names the build copies. */
export function imageFiles(): string[] {
  return PUBLIC_IMAGES.flatMap((image) => [image.light, image.dark])
}
