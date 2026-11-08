import { render } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'
import { ApiError } from '../api/client'
import { errorMessage, oauthOutcomeMessage } from '../api/messages'
import { Trans } from './Trans'
import { EN } from './en'
import { interpolate, lookup, pluralText, splitTemplate } from './translate'
import { plural, setLocale, t } from './store'

describe('interpolate', () => {
  it('fills named placeholders', () => {
    expect(interpolate('{a} and {b}', { a: 'x', b: 2 })).toBe('x and 2')
  })

  it('leaves a name without a value as written, and never reads inherited properties', () => {
    expect(interpolate('Hi {name} {constructor}', {})).toBe('Hi {name} {constructor}')
  })

  it('returns the template when there are no params', () => {
    expect(interpolate('Plain {text}')).toBe('Plain {text}')
  })
})

describe('lookup', () => {
  it('uses the language, then English, then the key', () => {
    expect(lookup({ k: 'sv' }, { k: 'en' }, 'k')).toBe('sv')
    expect(lookup({}, { k: 'en' }, 'k')).toBe('en')
    expect(lookup(undefined, { k: 'en' }, 'k')).toBe('en')
    expect(lookup(undefined, {}, 'missing.key')).toBe('missing.key')
  })
})

describe('plurals', () => {
  const messages = { 'a.one': '{count} apple', 'a.other': '{count} apples' }

  it('picks the form of the category and fills {count}', () => {
    expect(pluralText(messages, {}, 'en', 'a', 1, '1')).toBe('1 apple')
    expect(pluralText(messages, {}, 'en', 'a', 5, '5')).toBe('5 apples')
    expect(pluralText(messages, {}, 'sv', 'a', 0, '0')).toBe('0 apples')
  })

  it('falls back to the other form when a language lacks the category', () => {
    expect(pluralText({ 'a.other': '{count} things' }, {}, 'en', 'a', 1, '1')).toBe('1 things')
  })

  it('has the English forms in the catalogue', () => {
    expect(plural('errors.minutes', 1)).toBe('1 minute')
    expect(plural('errors.minutes', 7)).toBe('7 minutes')
    expect(plural('errors.seconds', 1)).toBe('1 second')
    expect(EN['errors.minutes.other']).toBe('{count} minutes')
  })
})

describe('Trans', () => {
  it('puts each slot where its placeholder is, and adds no markup of its own', () => {
    const { container } = render(<Trans k="auth.provider.sent" slots={{ email: <strong>ann@example.test</strong> }} />)
    expect(container.querySelector('strong')?.textContent).toBe('ann@example.test')
    expect(container.textContent).toBe(t('auth.provider.sent', { email: 'ann@example.test' }))
  })

  it('shows a placeholder with no slot as written', () => {
    const { container } = render(<Trans k="auth.reset.remove" slots={{}} />)
    expect(container.textContent).toBe('Remove {provider}')
  })

  it('shows text slots as text, not as markup', () => {
    const { container } = render(<Trans k="auth.reset.remove" slots={{ provider: '<b>x</b>' }} />)
    expect(container.querySelector('b')).toBeNull()
    expect(container.textContent).toBe('Remove <b>x</b>')
  })

  it('splits a template into text and names', () => {
    expect(splitTemplate('a {x} b {y}')).toEqual(['a ', 'x', ' b ', 'y', ''])
  })
})

describe('the auth reason codes', () => {
  afterEach(() => setLocale('en'))

  it('are worded by the catalogue, with a generic text for an unknown code', () => {
    const known = new ApiError(401, 'invalid_credentials', null, null)
    expect(errorMessage(known)).toBe('The email or password is not right.')
    expect(errorMessage(new ApiError(401, 'who_knows', null, null))).toBe('That did not work. Try again.')
    expect(errorMessage(new ApiError(403, 'minutes.one', null, null))).toBe('That did not work. Try again.')
    expect(oauthOutcomeMessage('denied')).toBe('The sign-in was cancelled at Google.')
    expect(oauthOutcomeMessage('nonsense')).toBe('The sign-in could not be completed. Try again.')
  })

  it('say how long to wait, with the right plural', () => {
    expect(errorMessage(new ApiError(429, null, 1, null))).toBe('Too many attempts. Try again in 1 second.')
    expect(errorMessage(new ApiError(429, null, 30, null))).toBe('Too many attempts. Try again in 30 seconds.')
    expect(errorMessage(new ApiError(429, null, 300, null))).toBe('Too many attempts. Try again in 5 minutes.')
    expect(errorMessage(new ApiError(429, null, null, null))).toBe('Too many attempts. Wait a little and try again.')
  })
})
