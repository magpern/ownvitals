import { readFileSync, readdirSync } from 'node:fs'
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

/** Files the operating system or the file manager leaves behind; they are not sources. */
const OS_LITTER = /^(?:\.DS_Store|Thumbs\.db|desktop\.ini)$/i
/** Media a browser renders but never parses for URLs or colours (a binary file may be one of these). */
const MEDIA = /\.(?:png|jpe?g|gif|webp|avif|ico|woff2?|ttf|otf)$/i

function files(dir: string): string[] {
  return readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
    const path = join(dir, entry.name)
    if (entry.isDirectory()) return files(path)
    return OS_LITTER.test(entry.name) ? [] : [path]
  })
}

/** Every file under `dir`, or none when there is no such folder. */
function filesIfAny(dir: string): string[] {
  try {
    return files(dir)
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === 'ENOENT') return []
    throw error
  }
}

/** The files the browser gets from the repository, as absolute paths. */
function shippedFiles(root: string): string[] {
  const src = join(root, 'src')
  const inSrc = filesIfAny(src).filter(
    (path) => !/\.test\.tsx?$/.test(path) && !relative(src, path).startsWith(`test${sep}`),
  )
  return [join(root, 'index.html'), ...filesIfAny(join(root, 'public')), ...inSrc]
}

function pathOf(root: string, path: string): string {
  return relative(root, path).split(sep).join('/')
}

/** A file with a NUL byte in its first 8 KiB (git's own test) is binary and has no text to scan. */
function isBinary(bytes: Buffer): boolean {
  return bytes.subarray(0, 8192).includes(0)
}

/**
 * Everything the browser gets from the repository, as text: `index.html`, every text file in
 * `public/` and every one under `src/` (stylesheets, scripts and components, but also `.svg`,
 * `.json`, `.html`, `.txt`, a web manifest or any other text asset). Tests and `src/test/` are
 * not shipped. A binary file is not here: see `binaryAssets`.
 */
export function shippedSources(root: string = ROOT): Source[] {
  return shippedFiles(root).flatMap((path) => {
    const bytes = readFileSync(path)
    return isBinary(bytes) ? [] : [{ path: pathOf(root, path), text: bytes.toString('utf8') }]
  })
}

/** The shipped files `shippedSources` cannot read as text (images, fonts, anything else binary). */
export function binaryAssets(root: string = ROOT): string[] {
  return shippedFiles(root)
    .filter((path) => isBinary(readFileSync(path)))
    .map((path) => pathOf(root, path))
}

/** The binary assets that are not images or fonts: nothing that is scanned, nothing known to be harmless. */
export function unreadableAssets(root: string = ROOT): string[] {
  return binaryAssets(root).filter((path) => !MEDIA.test(path))
}

// --- third-party hosts ---------------------------------------------------------------------

/** The only hosts a source may name: this machine, spelled exactly like this. */
const LOCAL_HOSTS = new Set(['localhost', '127.0.0.1', '[::1]'])

// XML namespaces are identifiers, not requests (every SVG names one or two). The lookahead stops
// `http://www.w3.org/2000/svg.evil.example` from passing as one.
const XML_NAMESPACE =
  /http:\/\/www\.w3\.org\/(?:2000\/svg|1999\/xlink|1999\/xhtml|XML\/1998\/namespace)(?=["'\s>)]|$)/g

// Where the authority of a URL (userinfo, host and port) starts: after `scheme:` and two or more
// slashes (a browser reads a backslash there as a slash; a one-letter scheme is a Windows drive,
// a path such as `C:\\dir` in a string), or at `//` alone, a protocol-relative URL. It runs to the next slash, quote,
// bracket, whitespace, `?` or `#`.
const URL_AUTHORITY =
  /(?:(?<![a-z0-9+.-])([a-z][a-z0-9+.-]+):[/\\]{2,}|(?<![a-z0-9+.:/\\-])\/{2,})([^\s'"`<>()\\/?#]*)/gi

/** The host of an authority: no userinfo, no port, no trailing dot, lower case. */
function hostOf(authority: string): string {
  let host = authority.replace(/[.,;:]+$/, '') // punctuation after a URL in running text
  host = host.slice(host.lastIndexOf('@') + 1) // `http://localhost@evil.example` is evil.example
  host = host.startsWith('[') ? host.slice(0, host.indexOf(']') + 1) : host.replace(/:\d*$/, '')
  return host.replace(/\.$/, '').toLowerCase()
}

/**
 * The URLs in `text` that name another host: any scheme with a host (`https://localhost.evil.example`,
 * `http://203.0.113.5`, `wss://x.example`, `http://[2001:db8::1]`, `http://localhost@evil.example`,
 * `https://127.0.0.1.nip.io`), and protocol-relative URLs (`//cdn.example.org`). Only `localhost`,
 * `127.0.0.1` and `[::1]`, spelled exactly like that, and XML namespaces are let through. A
 * protocol-relative host needs a dot or a bracket, or an opening quote, `(` or `=` in front, so a
 * comment such as `//TODO` is not one. JSON's escaped `\/\/` is read as `//`.
 */
export function remoteUrls(text: string): string[] {
  const plain = text.replaceAll('\\/\\/', '//').replace(XML_NAMESPACE, '')
  const found: string[] = []
  for (const match of plain.matchAll(URL_AUTHORITY)) {
    const [whole = '', scheme, authority = ''] = match
    const host = hostOf(authority)
    if (host === '' || LOCAL_HOSTS.has(host)) continue
    if (scheme === undefined) {
      const quoted = '\'"(='.includes(plain.charAt(match.index - 1) || ' ')
      if (!host.includes('.') && !host.startsWith('[') && !quoted) continue
    }
    found.push(whole.replace(/[.,;:]+$/, ''))
  }
  return found
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
  // Declarations (`fill: red`) are also written in the style element and attribute of an SVG.
  const hasDeclarations = isCss || path.endsWith('.svg')
  const text = isCss ? source.replace(/\/\*[\s\S]*?\*\//g, '') : source
  const found: string[] = [
    ...(text.match(HEX_COLOUR) ?? []),
    ...(text.match(COLOUR_FUNCTION) ?? []),
    ...[...text.matchAll(COLOUR_ATTRIBUTE)]
      .map((match) => match[1] ?? '')
      .filter((word) => CSS_COLOUR_NAMES.has(word.toLowerCase())),
  ]
  if (hasDeclarations) {
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
  if (/\.html?$/i.test(path)) {
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
  } else if (path.endsWith('.svg')) {
    // Fine as an image, blocked by the CSP as soon as the file is opened as a page.
    add(/<script\b/i, 'a script element')
    add(/\son[a-z]+\s*=/i, 'an inline event handler')
  } else if (path.endsWith('.tsx') || path.endsWith('.ts')) {
    add(/\bstyle=[{"']/, 'an inline style attribute')
    add(/<style\b|<script\b/i, 'a style or script element')
  }
  return found
}
