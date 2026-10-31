import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { dirname, join } from 'node:path'
import { afterAll, describe, expect, it } from 'vitest'
import {
  TOKENS,
  binaryAssets,
  colourLiterals,
  cspViolations,
  dataUrls,
  remoteUrls,
  shippedSources,
  unreadableAssets,
} from './test/sourceScan'

// Three rules about what the repository ships, checked on the sources themselves:
//   1. no third-party host: the browser talks to our own origin only (the CSP's `'self'`);
//   2. no colour literal outside src/styles/tokens.css;
//   3. nothing the CSP would block: no inline script or style, no `data:` URL.
// src/build.test.ts checks the third rule again on the production build.
// "The sources" are every text file the browser can get: index.html, all of public/ and all of
// src/ except tests and src/test/, whatever the extension (.svg, .json, .html, .txt, a manifest).

const sources = shippedSources()

const scratch: string[] = []
afterAll(() => {
  for (const dir of scratch) rmSync(dir, { recursive: true, force: true })
})

/** A scratch folder laid out like `frontend/` (index.html, src/, public/) with these files. */
function tree(content: Record<string, string | Buffer>): string {
  const root = mkdtempSync(join(tmpdir(), 'ownvitals-scan-'))
  scratch.push(root)
  for (const [path, data] of Object.entries({ 'index.html': '<!doctype html>', ...content })) {
    const file = join(root, path)
    mkdirSync(dirname(file), { recursive: true })
    writeFileSync(file, data)
  }
  return root
}

/** Everything the three rules object to in the sources of `root`, as `path: what`. */
function problems(root: string): string[] {
  return shippedSources(root).flatMap(({ path, text }) => [
    ...remoteUrls(text).map((url) => `${path}: remote URL ${url}`),
    ...colourLiterals(path, text).map((colour) => `${path}: colour ${colour}`),
    ...cspViolations(path, text).map((what) => `${path}: ${what}`),
  ])
}

describe('the scan itself', () => {
  it('finds the sources it is meant to scan', () => {
    const paths = sources.map((source) => source.path)
    expect(paths).toContain('index.html')
    expect(paths).toContain(TOKENS)
    expect(paths).toContain('src/styles/app.css')
    expect(paths).toContain('src/main.tsx')
    expect(paths).toContain('src/api/client.ts')
    for (const path of ['index.html', TOKENS, 'src/main.tsx']) {
      expect(sources.find((source) => source.path === path)?.text.length).toBeGreaterThan(0)
    }
  })

  it('leaves tests and test helpers out', () => {
    expect(sources.filter((source) => /\.test\.|src\/test\//.test(source.path))).toEqual([])
  })

  it('reads every text file under src/ and public/, not only css, ts and tsx', () => {
    const root = tree({
      'src/main.tsx': 'export {}',
      'src/styles/app.css': 'a { margin: 0 }',
      'src/assets/logo.svg': '<svg/>',
      'src/data/links.json': '{}',
      'src/pages/about.html': '<p>x</p>',
      'src/notes.txt': 'x',
      'src/empty.js': '',
      'src/api/client.test.ts': 'planted test',
      'src/components/Card.test.tsx': 'planted test',
      'src/test/helper.ts': 'planted helper',
      'public/robots.txt': 'User-agent: *',
      'public/site.webmanifest': '{}',
      'public/icons/mark.svg': '<svg/>',
      'public/.well-known/security.txt': 'Contact: x',
      'src/.DS_Store': Buffer.from([0, 1, 2]),
    })
    expect(
      shippedSources(root)
        .map((source) => source.path)
        .sort(),
    ).toEqual(
      [
        'index.html',
        'public/.well-known/security.txt',
        'public/icons/mark.svg',
        'public/robots.txt',
        'public/site.webmanifest',
        'src/assets/logo.svg',
        'src/data/links.json',
        'src/empty.js',
        'src/main.tsx',
        'src/notes.txt',
        'src/pages/about.html',
        'src/styles/app.css',
      ].sort(),
    )
  })

  it('does not stop at a missing public/ folder', () => {
    expect(shippedSources(tree({ 'src/main.tsx': 'export {}' })).map((source) => source.path).sort()).toEqual([
      'index.html',
      'src/main.tsx',
    ])
  })

  it('lists a binary file instead of skipping it without a word', () => {
    const root = tree({
      'src/assets/mark.png': Buffer.from([0x89, 0x50, 0x4e, 0x47, 0, 1, 2]),
      'public/font.woff2': Buffer.from([0x77, 0x4f, 0x46, 0x32, 0, 0]),
      'src/assets/utf16.svg': Buffer.from('<\0s\0v\0g\0/\0>\0'),
      'src/assets/data.bin': Buffer.from([1, 0, 2]),
    })
    expect(shippedSources(root).map((source) => source.path)).toEqual(['index.html'])
    expect(binaryAssets(root).sort()).toEqual([
      'public/font.woff2',
      'src/assets/data.bin',
      'src/assets/mark.png',
      'src/assets/utf16.svg',
    ])
    // Images and fonts hold nothing the scan looks for; anything else binary needs a decision.
    expect(unreadableAssets(root).sort()).toEqual(['src/assets/data.bin', 'src/assets/utf16.svg'])
  })

  it('finds no shipped file it cannot read as text, other than images and fonts', () => {
    expect(unreadableAssets()).toEqual([])
  })

  it('passes a tree that is clean, with a svg, a manifest and a JSON file in it', () => {
    const root = tree({
      'index.html': '<!doctype html><script type="module" src="/src/main.tsx"></script>',
      'src/main.tsx': "export const api = '/api/auth/session'",
      [TOKENS]: ':root { --ov-bg: #fff; }',
      'src/assets/logo.svg':
        '<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink"><rect fill="none"/><path fill="currentColor" stroke="currentColor" d="M0 0h8v8H0z"/></svg>',
      'src/data/links.json': '{"home": "/", "api": "http://localhost:8000/api"}',
      'public/site.webmanifest': '{"name": "ownvitals", "start_url": "/"}',
    })
    expect(problems(root)).toEqual([])
  })

  it('catches what is planted in an svg, a json file, a manifest, a text file and an html page', () => {
    const root = tree({
      'src/assets/logo.svg':
        '<svg xmlns="http://www.w3.org/2000/svg"><rect fill="#123456"/><image href="https://cdn.example.com/logo.png"/></svg>',
      'src/assets/spin.svg': '<svg xmlns="http://www.w3.org/2000/svg"><script>x()</script><rect onclick="x()"/></svg>',
      'src/assets/pulse.svg': '<svg xmlns="http://www.w3.org/2000/svg"><style>.a { fill: tomato }</style></svg>',
      'src/data/links.json': '{"script": "//cdn.example.org/lib.js"}',
      'public/site.webmanifest': '{"theme_color": "#1f6f78", "start_url": "https://203.0.113.5/"}',
      'public/humans.txt': 'Team: https://localhost.evil.example/team',
      'src/pages/about.html': '<p style="margin: 0">x</p><img src="data:image/png;base64,AAAA">',
    })
    expect(problems(root)).toEqual(
      expect.arrayContaining([
        'src/assets/logo.svg: remote URL https://cdn.example.com',
        'src/assets/logo.svg: colour #123456',
        'src/assets/spin.svg: a script element',
        'src/assets/spin.svg: an inline event handler',
        'src/assets/pulse.svg: colour tomato',
        'src/data/links.json: remote URL //cdn.example.org',
        'public/site.webmanifest: remote URL https://203.0.113.5',
        'public/site.webmanifest: colour #1f6f78',
        'public/humans.txt: remote URL https://localhost.evil.example',
        'src/pages/about.html: a data: URL',
        'src/pages/about.html: a style attribute',
      ]),
    )
    expect(problems(root)).toHaveLength(11)
  })
})

describe('no third-party hosts', () => {
  it.each(sources.map((source) => [source.path, source.text] as const))('%s has no remote URL', (_path, text) => {
    expect(remoteUrls(text)).toEqual([])
  })

  it.each([
    ['url(https://fonts.example.com/a.woff2)', ['https://fonts.example.com']],
    ["@import '//cdn.example.org/x.css'", ['//cdn.example.org']],
    ['fetch("http://analytics.example.net/p")', ['http://analytics.example.net']],
    // A host that starts like localhost, or hides behind it.
    ['https://localhost.evil.example/x', ['https://localhost.evil.example']],
    ['http://localhost.evil.example:8000/', ['http://localhost.evil.example:8000']],
    ['https://localhostevil.example/x', ['https://localhostevil.example']],
    ['http://localhost@evil.example/x', ['http://localhost@evil.example']],
    ['http://127.0.0.1@evil.example/x', ['http://127.0.0.1@evil.example']],
    ['https://127.0.0.1.nip.io/x', ['https://127.0.0.1.nip.io']],
    ['https://127.0.0.10/x', ['https://127.0.0.10']],
    ['http://localhost:80@evil.example/', ['http://localhost:80@evil.example']],
    // IP literals, a host with no dot, other schemes.
    ['http://203.0.113.5/x', ['http://203.0.113.5']],
    ['http://203.0.113.5:8080/x', ['http://203.0.113.5:8080']],
    ['http://[2001:db8::1]/x', ['http://[2001:db8::1]']],
    ['http://3232235777/x', ['http://3232235777']],
    ['http://intranet/x', ['http://intranet']],
    ['wss://evil.example/socket', ['wss://evil.example']],
    ['ftp://files.example.net/a', ['ftp://files.example.net']],
    ['HTTPS://EVIL.EXAMPLE/x', ['HTTPS://EVIL.EXAMPLE']],
    // Protocol-relative URLs, with a dot, a bracket, or a quote in front.
    ['//203.0.113.5/x', ['//203.0.113.5']],
    ['//[2001:db8::1]/x', ['//[2001:db8::1]']],
    ['<script src=//cdn.example.org/a.js>', ['//cdn.example.org']],
    ['url(//cdn.example.org/a.css)', ['//cdn.example.org']],
    ['"//intranet/x"', ['//intranet']],
    ['src=//cdn/x', ['//cdn']],
    ['"///evil.example/x"', ['///evil.example']],
    // Slashes a browser does not mind, and a JSON-escaped URL.
    ['https:///evil.example/x', ['https:///evil.example']],
    ['https:\\\\evil.example', ['https:\\\\evil.example']],
    [String.raw`https:/\evil.example/x`, [String.raw`https:/\evil.example`]],
    [String.raw`http:\/evil.example/x`, [String.raw`http:\/evil.example`]],
    [String.raw`{"u": "https:\/\/cdn.example.com\/a.js"}`, [String.raw`https:\/\/cdn.example.com`]],
    [String.raw`{"u": "\/\/cdn.example.com\/a.js"}`, [String.raw`\/\/cdn.example.com`]],
    ['http://www.w3.org/2000/svg.evil.example/x', ['http://www.w3.org']],
    // A backslash is a slash to a browser: /\host and \\host act like //host.
    [String.raw`/\evil.example/x`, [String.raw`/\evil.example`]],
    [String.raw`\/evil.example/x`, [String.raw`\/evil.example`]],
    [String.raw`\\evil.example\share`, [String.raw`\\evil.example`]],
    [String.raw`\\203.0.113.5\x`, [String.raw`\\203.0.113.5`]],
    [String.raw`/\[2001:db8::1]/x`, [String.raw`/\[2001:db8::1]`]],
    [String.raw`<a href="/\evil.example/x">`, [String.raw`/\evil.example`]],
    [String.raw`<script src=\\cdn.example.org\a.js>`, [String.raw`\\cdn.example.org`]],
    [String.raw`@import url(\\cdn.example.org\a.css);`, [String.raw`\\cdn.example.org`]],
    [String.raw`location.href = '/\evil.example'`, [String.raw`/\evil.example`]],
    // The same in the source of a string, where each backslash is written twice.
    [String.raw`const u = '/\\evil.example'`, [String.raw`/\\evil.example`]],
    [String.raw`const u = '\\\\evil.example\\x'`, [String.raw`\\\\evil.example`]],
    // Running text.
    ['// loaded from https://cdn.example.com.', ['https://cdn.example.com']],
  ])('catches a remote host in %s', (text, expected) => {
    expect(remoteUrls(text)).toEqual(expected)
  })

  it.each([
    'http://localhost',
    'http://localhost/x',
    'http://localhost:8000',
    'http://127.0.0.1/x',
    'http://127.0.0.1:5173/x',
    'http://[::1]:8000/x',
    'HTTP://LOCALHOST:8000/x',
    'http://evil.example@localhost/x',
    'http://localhost.:8000/',
    '<svg xmlns="http://www.w3.org/2000/svg">',
    '<svg xmlns:xlink="http://www.w3.org/1999/xlink">',
    "xmlns = 'http://www.w3.org/1999/xhtml'",
    '// a comment',
    '//TODO: something',
    '//eslint-disable-next-line',
    '/* https:// is the scheme */',
    'a message that says http:// or https://',
    '/api/auth/session',
    'const p = a + "//" + b',
    'a//b.c',
    'pathname.replace(/\\/+$/, "")',
    'if (/^https?:\\/\\//.test(url)) return',
    'const dir = "C:\\\\Users\\\\x"',
    '`//host` in a comment',
    // Backslashes that are not a URL: escapes in strings and regular expressions, Windows paths.
    String.raw`const s = '\\n'`,
    String.raw`const dir = 'C:\\Users\\x.y'`,
    String.raw`const re = new RegExp('\\d+\\.\\d+')`,
    String.raw`const re = new RegExp('^\\w+@example\\.com$')`,
    String.raw`const re = /^\/\/[a-z.]+/`,
    String.raw`const re = /\/\/cdn\.example/`,
    String.raw`const re = /\\.example\.com$/`,
    String.raw`a { content: "\\201C" }`,
    String.raw`/\/+$/`,
    String.raw`x = a / \b.c`,
  ])('lets %s through', (text) => {
    expect(remoteUrls(text)).toEqual([])
  })
})

describe('no colour literal outside the tokens', () => {
  it.each(sources.map((source) => [source.path, source.text] as const))('%s has none', (path, text) => {
    expect(colourLiterals(path, text)).toEqual([])
  })

  it('defines the colours in tokens.css, so the scan has something to keep out of the rest', () => {
    const tokens = sources.find((source) => source.path === TOKENS)?.text ?? ''
    expect(tokens).toMatch(/--ov-bg:\s*#/)
    expect(colourLiterals('src/styles/other.css', tokens).length).toBeGreaterThan(10)
  })

  it.each([
    ['src/styles/a.css', 'a { color: #fff; }'],
    ['src/styles/a.css', 'a { background: #1f6f78cc; }'],
    ['src/styles/a.css', 'a { border: 1px solid #abc; }'],
    ['src/styles/a.css', 'a { color: rgb(0 0 0 / 0.5); }'],
    ['src/styles/a.css', 'a { color: hsl(10 20% 30%); }'],
    ['src/styles/a.css', 'a { color: oklch(60% 0.1 200); }'],
    ['src/styles/a.css', 'a { background: color-mix(in srgb, var(--ov-bg) 50%, white); }'],
    ['src/styles/a.css', 'a { border: 1px solid red; }'],
    ['src/styles/a.css', 'a { box-shadow: 0 0 4px Black; }'],
    ['src/styles/a.css', ':root { --ov-extra: tomato; }'],
    ['src/components/A.tsx', '<svg><rect fill="#000" /></svg>'],
    ['src/components/A.tsx', '<svg><rect fill="red" /></svg>'],
    ['src/components/A.tsx', "const a = { color: 'tan' }"],
    ['src/components/A.tsx', "const a = 'rgba(0, 0, 0, .5)'"],
    ['index.html', '<meta name="theme-color" content="#1f6f78" />'],
    ['src/assets/logo.svg', '<svg><rect fill="#123456"/></svg>'],
    ['src/assets/logo.svg', '<svg><rect fill="red"/></svg>'],
    ['src/assets/logo.svg', '<svg><stop stop-color="navy"/></svg>'],
    ['src/assets/logo.svg', '<svg><style>.a { fill: tomato }</style></svg>'],
    ['src/assets/logo.svg', '<svg><rect style="stroke: #fff; fill: Teal"/></svg>'],
    ['src/assets/logo.svg', '<svg><rect fill="rgb(1 2 3)"/></svg>'],
    ['public/site.webmanifest', '{"theme_color": "#1f6f78"}'],
    ['src/data/palette.json', '{"accent": "hsl(10 20% 30%)"}'],
  ])('catches a colour literal in %s: %s', (path, text) => {
    expect(colourLiterals(path, text).length).toBeGreaterThan(0)
  })

  it.each([
    ['src/styles/a.css', 'a { color: var(--ov-text); background: var(--ov-bg); }'],
    ['src/styles/a.css', 'a { color: currentColor; background: transparent; border: 1px solid var(--ov-line); }'],
    ['src/styles/a.css', 'a { border: 0; box-shadow: none; font-family: system-ui, Arial; }'],
    ['src/styles/a.css', '/* #fff and red in a comment */ a { margin: 0 }'],
    ['src/components/A.tsx', '<svg><rect fill="none" /><path fill="currentColor" /></svg>'],
    ['src/components/A.tsx', "const path = '/reset#token=abc'"],
    [TOKENS, ':root { --ov-bg: #fff; }'],
    ['src/assets/logo.svg', '<svg xmlns="http://www.w3.org/2000/svg"><rect fill="none"/><path fill="currentColor"/></svg>'],
    ['src/assets/logo.svg', '<svg><style>.a { fill: var(--ov-text); stroke: currentColor }</style></svg>'],
    ['public/site.webmanifest', '{"name": "ownvitals", "display": "standalone", "start_url": "/"}'],
  ])('lets %s through: %s', (path, text) => {
    expect(colourLiterals(path, text)).toEqual([])
  })
})

describe('nothing the content security policy would block', () => {
  it.each(sources.map((source) => [source.path, source.text] as const))('%s has none', (path, text) => {
    expect(cspViolations(path, text)).toEqual([])
  })

  it('index.html loads its one script as a module from our own origin', () => {
    const html = sources.find((source) => source.path === 'index.html')?.text ?? ''
    expect(html.match(/<script\b[^>]*>/g)).toEqual(['<script type="module" src="/src/main.tsx">'])
    expect(html).not.toMatch(/<link\b[^>]*rel=["']?(?:stylesheet|preload)/i)
  })

  it.each([
    ['index.html', '<script>window.x = 1</script>', 'an inline script'],
    ['index.html', '<script src="https://cdn.example.com/a.js"></script>', 'not from our own origin'],
    ['index.html', '<script src="//cdn.example.com/a.js"></script>', 'not from our own origin'],
    ['index.html', '<style>a{}</style>', 'an inline style element'],
    ['index.html', '<p style="color: var(--ov-text)">x</p>', 'a style attribute'],
    ['index.html', '<body onload="x()">', 'an inline event handler'],
    ['index.html', '<base href="/">', 'a base element'],
    ['index.html', '<meta http-equiv="Content-Security-Policy" content="default-src \'self\'">', 'a meta policy'],
    ['index.html', '<a href="javascript:x()">', 'a javascript: URL'],
    ['index.html', '<img src="data:image/png;base64,AAAA">', 'a data: URL'],
    ['src/styles/a.css', 'a { background: url(data:image/svg+xml;utf8,<svg/>) }', 'a data: URL'],
    ['src/components/A.tsx', '<p style={{ margin: 0 }}>x</p>', 'an inline style attribute'],
    ['src/components/A.tsx', '<p dangerouslySetInnerHTML={{ __html: x }} />', 'markup inserted as a string'],
    ['src/components/A.tsx', 'el.innerHTML = x', 'markup inserted as a string'],
    ['src/components/A.tsx', 'const f = new Function("return 1")', 'eval or new Function'],
    ['src/components/A.tsx', 'eval(x)', 'eval or new Function'],
    ['src/assets/logo.svg', '<svg><script>x()</script></svg>', 'a script element'],
    ['src/assets/logo.svg', '<svg onload="x()"></svg>', 'an inline event handler'],
    ['src/assets/logo.svg', '<svg><image href="data:image/png;base64,AAAA"/></svg>', 'a data: URL'],
    ['src/assets/logo.svg', '<svg><a href="javascript:x()"/></svg>', 'a javascript: URL'],
    ['src/pages/about.html', '<script>x()</script>', 'an inline script'],
    ['src/pages/about.htm', '<p style="margin: 0">x</p>', 'a style attribute'],
    ['src/data/links.json', '{"u": "data:text/html,<p>x</p>"}', 'a data: URL'],
  ])('catches %s: %s', (path, text, what) => {
    expect(cspViolations(path, text).join(' ')).toContain(what)
  })

  it('finds data: URLs but not a property called data', () => {
    expect(dataUrls('x = "data:image/png;base64,AAAA"')).not.toEqual([])
    expect(dataUrls('x = "data:,hello"')).not.toEqual([])
    expect(dataUrls('{data:e,method:t} data: null')).toEqual([])
  })
})
