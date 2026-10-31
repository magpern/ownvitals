import { readdirSync, readFileSync } from 'node:fs'
import { join, relative, sep } from 'node:path'

// The scanners behind src/sources.test.ts (what the shipped sources may contain) and
// src/build.test.ts (what the production build may contain). They read files from disk, so a
// stylesheet is checked as written.

export const SRC = join(import.meta.dirname, '..')
export const ROOT = join(SRC, '..')
/** The one file that may hold colour literals. */
export const TOKENS = 'src/styles/tokens.css'

export interface Source {
  /** Relative to `frontend/`, with forward slashes. */
  path: string
  text: string
}

function files(dir: string): string[] {
  return readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
    const path = join(dir, entry.name)
    return entry.isDirectory() ? files(path) : [path]
  })
}

function read(path: string): Source {
  return { path: relative(ROOT, path).split(sep).join('/'), text: readFileSync(path, 'utf8') }
}

/**
 * Everything the browser gets from the repository: `index.html`, `public/`, and the stylesheets,
 * scripts and components under `src/`. Tests and `src/test/` are not shipped.
 */
export function shippedSources(): Source[] {
  const inSrc = files(SRC).filter(
    (path) =>
      /\.(?:css|ts|tsx)$/.test(path) &&
      !/\.test\.tsx?$/.test(path) &&
      !relative(SRC, path).startsWith(`test${sep}`),
  )
  let inPublic: string[] = []
  try {
    inPublic = files(join(ROOT, 'public'))
  } catch {
    // No public/ folder: nothing to scan there.
  }
  return [join(ROOT, 'index.html'), ...inPublic, ...inSrc].map(read)
}

// --- third-party hosts ---------------------------------------------------------------------

const REMOTE_URL = /(?:https?:)?\/\/(?!localhost\b|127\.0\.0\.1\b)[a-z0-9.-]+\.[a-z]{2,}/gi

/** The remote URLs in `text`; the SVG namespace is an identifier, not a request. */
export function remoteUrls(text: string): string[] {
  return text.replaceAll('http://www.w3.org/2000/svg', '').match(REMOTE_URL) ?? []
}

// --- colour literals -----------------------------------------------------------------------

const CSS_COLOUR_NAMES = new Set(
  (
    'aliceblue antiquewhite aqua aquamarine azure beige bisque black blanchedalmond blue blueviolet brown ' +
    'burlywood cadetblue chartreuse chocolate coral cornflowerblue cornsilk crimson cyan darkblue darkcyan ' +
    'darkgoldenrod darkgray darkgreen darkgrey darkkhaki darkmagenta darkolivegreen darkorange darkorchid ' +
    'darkred darksalmon darkseagreen darkslateblue darkslategray darkslategrey darkturquoise darkviolet ' +
    'deeppink deepskyblue dimgray dimgrey dodgerblue firebrick floralwhite forestgreen fuchsia gainsboro ' +
    'ghostwhite gold goldenrod gray green greenyellow grey honeydew hotpink indianred indigo ivory khaki ' +
    'lavender lavenderblush lawngreen lemonchiffon lightblue lightcoral lightcyan lightgoldenrodyellow ' +
    'lightgray lightgreen lightgrey lightpink lightsalmon lightseagreen lightskyblue lightslategray ' +
    'lightslategrey lightsteelblue lightyellow lime limegreen linen magenta maroon mediumaquamarine ' +
    'mediumblue mediumorchid mediumpurple mediumseagreen mediumslateblue mediumspringgreen mediumturquoise ' +
    'mediumvioletred midnightblue mintcream mistyrose moccasin navajowhite navy oldlace olive olivedrab ' +
    'orange orangered orchid palegoldenrod palegreen paleturquoise palevioletred papayawhip peachpuff peru ' +
    'pink plum powderblue purple rebeccapurple red rosybrown royalblue saddlebrown salmon sandybrown ' +
    'seagreen seashell sienna silver skyblue slateblue slategray slategrey snow springgreen steelblue tan ' +
    'teal thistle tomato turquoise violet wheat white whitesmoke yellow yellowgreen'
  ).split(' '),
)

const HEX_COLOUR = /#(?:[0-9a-f]{8}|[0-9a-f]{6}|[0-9a-f]{3,4})(?![0-9a-z_-])/gi
const COLOUR_FUNCTION = /\b(?:rgba?|hsla?|hwb|lab|lch|oklab|oklch|color|color-mix|device-cmyk)\(/gi
const COLOUR_PROPERTY = /color|background|border|outline|fill|stroke|shadow|accent|caret|decoration|column-rule|scrollbar|^--/
// fill="red", stroke: 'blue', color={'tan'}
const COLOUR_ATTRIBUTE = /\b(?:fill|stroke|color|stopColor|floodColor|lightingColor)\s*[=:]\s*\{?\s*['"]([a-z]+)['"]/gi

/** Named colours among the words of a CSS value (`var()`, `url()` and strings left out). */
function namedColours(value: string): string[] {
  const bare = value
    .replace(/var\([^)]*\)/gi, ' ')
    .replace(/url\([^)]*\)/gi, ' ')
    .replace(/(['"]).*?\1/g, ' ')
  return (bare.match(/[a-z]+/gi) ?? []).filter((word) => CSS_COLOUR_NAMES.has(word.toLowerCase()))
}

/**
 * The colour literals in a shipped file: hex, colour functions and, in stylesheets and in
 * colour attributes, named colours. `tokens.css` is where colours are defined, so it has none to
 * report. Colours reach every other file through `var(--ov-...)`.
 */
export function colourLiterals(path: string, source: string): string[] {
  if (path === TOKENS) return []
  const isCss = path.endsWith('.css')
  const text = isCss ? source.replace(/\/\*[\s\S]*?\*\//g, '') : source
  const found: string[] = [
    ...(text.match(HEX_COLOUR) ?? []),
    ...(text.match(COLOUR_FUNCTION) ?? []),
    ...[...text.matchAll(COLOUR_ATTRIBUTE)]
      .map((match) => match[1] ?? '')
      .filter((word) => CSS_COLOUR_NAMES.has(word.toLowerCase())),
  ]
  if (isCss) {
    for (const [, property = '', value = ''] of text.matchAll(/([a-z-]+)\s*:\s*([^;{}]+)/gi)) {
      if (COLOUR_PROPERTY.test(property.toLowerCase())) found.push(...namedColours(value))
    }
  }
  return found
}

// --- the content security policy -----------------------------------------------------------

const DATA_URL = /\bdata:(?:(?:image|font|text|application|audio|video)\/[a-z0-9.+-]*)?[;,]|\bdata:(?:image|font|text|application|audio|video)\//gi

/** The `data:` URLs in `text` (the CSP allows none: `img-src` and `font-src` are 'self'). */
export function dataUrls(text: string): string[] {
  return text.match(DATA_URL) ?? []
}

/**
 * What the CSP (`default-src 'self'`, no inline script or style, no `data:`) forbids or makes
 * useless in a shipped source file or in the built `index.html`.
 */
export function cspViolations(path: string, text: string): string[] {
  const found: string[] = dataUrls(text).length > 0 ? ['a data: URL'] : []
  const add = (pattern: RegExp, what: string) => {
    if (pattern.test(text)) found.push(what)
  }
  add(/\bjavascript:/i, 'a javascript: URL')
  add(/\bdangerouslySetInnerHTML\b|\.(?:inner|outer)HTML\b|\binsertAdjacentHTML\b|\bdocument\.write\b/, 'markup inserted as a string')
  add(/\beval\s*\(|\bnew\s+Function\s*\(/, 'eval or new Function')
  if (path.endsWith('.html')) {
    add(/<script\b(?![^>]*\bsrc=)/i, 'an inline script')
    add(/<style\b/i, 'an inline style element')
    add(/\sstyle\s*=/i, 'a style attribute')
    add(/\son[a-z]+\s*=/i, 'an inline event handler')
    add(/<base\b/i, 'a base element')
    add(/http-equiv/i, 'a meta policy (the policy is a response header)')
    for (const tag of text.match(/<(?:script|link)\b[^>]*>/gi) ?? []) {
      const url = /\b(?:src|href)=["']([^"']+)["']/i.exec(tag)?.[1]
      if (url !== undefined && !(url.startsWith('/') && !url.startsWith('//'))) {
        found.push(`a script or stylesheet not from our own origin: ${url}`)
      }
    }
  } else if (path.endsWith('.tsx') || path.endsWith('.ts')) {
    add(/\bstyle=[{"']/, 'an inline style attribute')
    add(/<style\b|<script\b/i, 'a style or script element')
  }
  return found
}
