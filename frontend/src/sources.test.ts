import { describe, expect, it } from 'vitest'
import {
  TOKENS,
  colourLiterals,
  cspViolations,
  dataUrls,
  remoteUrls,
  shippedSources,
} from './test/sourceScan'

// Three rules about what the repository ships, checked on the sources themselves:
//   1. no third-party host: the browser talks to our own origin only (the CSP's `'self'`);
//   2. no colour literal outside src/styles/tokens.css;
//   3. nothing the CSP would block: no inline script or style, no `data:` URL.
// src/build.test.ts checks the third rule again on the production build.

const sources = shippedSources()

describe('the scan itself', () => {
  it('finds the sources it is meant to scan', () => {
    const paths = sources.map((source) => source.path)
    expect(paths).toContain('index.html')
    expect(paths).toContain(TOKENS)
    expect(paths).toContain('src/styles/app.css')
    expect(paths).toContain('src/main.tsx')
    expect(paths).toContain('src/api/client.ts')
    for (const source of sources) expect(source.text.length).toBeGreaterThan(0)
  })

  it('leaves tests and test helpers out', () => {
    expect(sources.filter((source) => /\.test\.|src\/test\//.test(source.path))).toEqual([])
  })
})

describe('no third-party hosts', () => {
  it.each(sources.map((source) => [source.path, source.text] as const))('%s has no remote URL', (_path, text) => {
    expect(remoteUrls(text)).toEqual([])
  })

  it('does catch a remote host, but not localhost or the SVG namespace', () => {
    expect(remoteUrls('url(https://fonts.example.com/a.woff2)')).toEqual(['https://fonts.example.com'])
    expect(remoteUrls("@import '//cdn.example.org/x.css'")).toEqual(['//cdn.example.org'])
    expect(remoteUrls('fetch("http://analytics.example.net/p")')).toEqual(['http://analytics.example.net'])
    expect(remoteUrls('http://localhost:8000 http://127.0.0.1/x')).toEqual([])
    expect(remoteUrls('<svg xmlns="http://www.w3.org/2000/svg">')).toEqual([])
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
  ])('catches %s: %s', (path, text, what) => {
    expect(cspViolations(path, text).join(' ')).toContain(what)
  })

  it('finds data: URLs but not a property called data', () => {
    expect(dataUrls('x = "data:image/png;base64,AAAA"')).not.toEqual([])
    expect(dataUrls('x = "data:,hello"')).not.toEqual([])
    expect(dataUrls('{data:e,method:t} data: null')).toEqual([])
  })
})
