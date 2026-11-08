// Sets <html lang> before the first paint, so a screen reader and the browser's hyphenation and
// quotes use the right language from the start. A same-origin static file, never an inline
// script: the CSP is script-src 'self'. The page's own language logic (src/i18n/resolve.ts) makes
// the same choice later; this file only ever writes a code from the manifest.
//
// SUPPORTED must equal src/i18n/locales.json (src/i18n/localeInit.test.ts checks it).
;(function () {
  var SUPPORTED = ['en', 'sv']
  var chosen = 'en'

  function match(tag) {
    var primary = String(tag).trim().toLowerCase().split('-')[0]
    return SUPPORTED.indexOf(primary) === -1 ? null : primary
  }

  function readStored() {
    try {
      return window.localStorage.getItem('ov-locale')
    } catch {
      return null
    }
  }

  var stored = readStored()
  if (stored !== null && SUPPORTED.indexOf(stored) !== -1) {
    chosen = stored
  } else {
    var languages = navigator.languages || []
    for (var i = 0; i < languages.length; i += 1) {
      var found = match(languages[i])
      if (found !== null) {
        chosen = found
        break
      }
    }
  }
  document.documentElement.setAttribute('lang', chosen)
})()
