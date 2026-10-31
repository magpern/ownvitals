// Renders the design canvas sources (src/*.dc.html) into plain HTML pages (html/*.html) that any
// browser opens without the canvas runtime. No dependencies: `node docs/design/mockups/build.mjs`.
//
// It understands exactly what the sources use: {{holes}} (dotted lookups into renderVals()),
// <sc-for list as>, <sc-if value>, <dc-import name> (the navigation rail) and onX="{{handler}}"
// attributes (dropped; the pages are static). Links between boards (X.dc.html) become X.html.
// Open a page with ?theme=dark for the dark theme.

import { copyFileSync, mkdirSync, readFileSync, readdirSync, writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));
const SRC = join(here, 'src');
const OUT = join(here, 'html');

class DCLogic {
  constructor(props) {
    this.props = props;
    this.state = {};
  }
  setState(s) {
    this.state = { ...this.state, ...s };
  }
  forceUpdate() {}
}

function decode(s) {
  return s.replace(/&#39;/g, "'").replace(/&quot;/g, '"').replace(/&amp;/g, '&');
}

function esc(v) {
  return String(v ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

function parse(file) {
  const text = readFileSync(join(SRC, file), 'utf8');
  const title = /<title>([\s\S]*?)<\/title>/.exec(text)[1];
  const xdc = /<x-dc>([\s\S]*?)<\/x-dc>/.exec(text)[1];
  const helmet = /<helmet>([\s\S]*?)<\/helmet>/.exec(xdc);
  const body = xdc.replace(/<helmet>[\s\S]*?<\/helmet>/, '').trim();
  const script = /<script type="text\/x-dc" data-dc-script data-props='([^']*)'>([\s\S]*?)<\/script>/.exec(text);
  const meta = JSON.parse(decode(script[1]));
  const defaults = {};
  for (const [k, v] of Object.entries(meta)) if (!k.startsWith('$')) defaults[k] = v.default;
  return { title, helmet: helmet ? helmet[1].trim() : '', body, defaults, code: script[2] };
}

function lookup(path, scope) {
  const p = path.trim();
  if (p === 'true') return true;
  if (p === 'false') return false;
  if (/^-?\d+(\.\d+)?$/.test(p)) return Number(p);
  return p.split('.').reduce((o, k) => (o == null ? undefined : o[k]), scope);
}

function attrsOf(text, scope) {
  const out = {};
  for (const m of text.matchAll(/([\w-]+)="([^"]*)"/g)) {
    if (m[1].startsWith('hint-')) continue;
    const key = m[1].replace(/-(\w)/g, (_, c) => c.toUpperCase());
    const hole = /^\{\{([^}]*)\}\}$/.exec(m[2]);
    out[key] = hole ? lookup(hole[1], scope) : m[2];
  }
  return out;
}

function render(tpl, scope) {
  let t = tpl.replace(/<sc-for\s+list="\{\{([^}]*)\}\}"\s+as="(\w+)"[^>]*>([\s\S]*?)<\/sc-for>/g, (_, list, as, inner) =>
    (lookup(list, scope) || []).map((item, i) => render(inner, { ...scope, [as]: item, $index: i })).join(''));
  t = t.replace(/<sc-if\s+value="\{\{([^}]*)\}\}"[^>]*>([\s\S]*?)<\/sc-if>/g, (_, cond, inner) =>
    (lookup(cond, scope) ? render(inner, scope) : ''));
  t = t.replace(/<dc-import\s+([^>]*)><\/dc-import>/g, (_, attrs) => {
    const a = attrsOf(attrs, scope);
    return renderBoard(`${a.name}.dc.html`, a).html;
  });
  t = t.replace(/\s+on[A-Z]\w*="\{\{[^}]*\}\}"/g, '');
  return t.replace(/\{\{([^}]*)\}\}/g, (_, p) => esc(lookup(p, scope)));
}

function renderBoard(file, passed = {}, state = {}) {
  const b = parse(file);
  const Component = new Function('DCLogic', `${b.code}\nreturn Component;`)(DCLogic);
  const inst = new Component({ ...b.defaults, ...passed });
  inst.state = { ...state };
  return { ...b, html: render(b.body, inst.renderVals()) };
}

// Boards with a second state worth its own page: <Board>.<variant>.html, rendered with that
// component state. Links on a variant page still point at the boards' default pages.
const VARIANTS = {
  'Findings.dc.html': [{ variant: 'history', state: { tab: 'history' } }],
};

const themeScript = `<script>
  // ?theme=dark switches every .ov root, the imported rail included.
  var t = new URLSearchParams(location.search).get('theme') || 'light';
  document.querySelectorAll('.ov').forEach(function (e) { e.setAttribute('data-theme', t); });
</script>`;

mkdirSync(OUT, { recursive: true });
copyFileSync(join(SRC, 'ov.css'), join(OUT, 'ov.css'));
const boards = readdirSync(SRC).filter((f) => f.endsWith('.dc.html')).sort();
const pages = boards.flatMap((file) => [
  { file, out: file.replace(/\.dc\.html$/, '.html'), state: {} },
  ...(VARIANTS[file] || []).map((v) => ({ file, out: file.replace(/\.dc\.html$/, `.${v.variant}.html`), state: v.state })),
]);
for (const { file, out, state } of pages) {
  const b = renderBoard(file, {}, state);
  const page = `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>${b.title} · ownvitals mock-up</title>
<link rel="stylesheet" href="ov.css">
${b.helmet}
</head>
<body>
${b.html}
${themeScript}
</body>
</html>
`;
  writeFileSync(join(OUT, out), page.replace(/([\w-]+)\.dc\.html/g, '$1.html'));
}
console.log(`rendered ${pages.length} pages from ${boards.length} boards into ${OUT}`);
