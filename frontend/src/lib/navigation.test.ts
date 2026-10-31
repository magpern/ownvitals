import { describe, expect, it } from 'vitest'
import { goToProvider } from './navigation'

describe('goToProvider', () => {
  it.each(['javascript:alert(1)', 'data:text/html,<p>x</p>', 'file:///etc/passwd', 'ftp://host/x'])(
    'refuses to leave for %s',
    (url) => {
      expect(() => {
        goToProvider(url)
      }).toThrow(/non-http/)
    },
  )

  it('refuses an address that is no URL at all', () => {
    expect(() => {
      goToProvider('not a url')
    }).toThrow()
  })
})
