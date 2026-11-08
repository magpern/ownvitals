import { describe, expect, it } from 'vitest'
import { scanAll, scanSource } from '../test/jsxStrings'

// The hard-coded string guard (ADR 0015 section 9): user-facing English belongs in the catalogue.
// The allowlists below only shrink. Adding to them needs the Architect; a new screen goes through
// `t()` from its first commit.

/** Whole files that stay English: the admin area (ADR 0015 section 3). */
const ENGLISH_FILES = ['pages/AdminPage.tsx']

/** The product name: the same in every language. */
const BRAND = [
  { file: 'components/PlainFrame.tsx', text: 'ownvitals' },
  { file: 'components/shell/AppFrame.tsx', text: 'ownvitals' },
  { file: 'components/shell/Rail.tsx', text: 'ownvitals' },
]

describe('the shipped .tsx files', () => {
  const found = scanAll().filter((finding) => !ENGLISH_FILES.includes(finding.file))

  it('hold no user-facing text outside the catalogue, apart from the allowlist', () => {
    const unlisted = found.filter(
      (finding) => !BRAND.some((brand) => brand.file === finding.file && brand.text === finding.text),
    )
    expect(unlisted).toEqual([])
  })

  it('leave no stale entry in the allowlist', () => {
    for (const brand of BRAND) expect(found, `${brand.file} ${brand.text}`).toContainEqual(brand)
    for (const file of ENGLISH_FILES) expect(scanAll().some((finding) => finding.file === file), file).toBe(true)
  })

  it('keep the allowlist from growing', () => {
    expect(ENGLISH_FILES).toEqual(['pages/AdminPage.tsx'])
    expect(BRAND).toHaveLength(3)
  })
})

describe('the scanner', () => {
  const scan = (body: string) => scanSource('Example.tsx', `export const X = () => (${body})`).map((f) => f.text)

  it('finds text between tags', () => {
    expect(scan('<p>Hello there</p>')).toEqual(['Hello there'])
  })

  it('finds a string shown as a child, also in a branch or after &&', () => {
    expect(scan("<p>{ok ? 'Yes' : 'No'}</p>")).toEqual(['Yes', 'No'])
    expect(scan("<p>{ok && 'Saved'}</p>")).toEqual(['Saved'])
  })

  it.each(['aria-label', 'title', 'placeholder', 'alt', 'label', 'hint'])('finds a literal in %s', (name) => {
    expect(scan(`<input ${name}="Search" />`)).toEqual(['Search'])
    expect(scan(`<input ${name}={'Search'} />`)).toEqual(['Search'])
  })

  it('ignores the catalogue, class names, numbers and punctuation', () => {
    expect(scan("<p className=\"card\" id=\"a\">{t('x.y')}{count}</p>")).toEqual([])
    expect(scan('<p>·</p>')).toEqual([])
    expect(scan('<p>12 / 3</p>')).toEqual([])
    expect(scan("<input aria-label={t('x.y')} />")).toEqual([])
  })
})
