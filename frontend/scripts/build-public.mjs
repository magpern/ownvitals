// Builds the prerendered public site into dist-public/ (ADR 0016 section 1). Run after the SPA
// build: `npm run build:public`. Settings, both from the environment:
//   OWNVITALS_PUBLIC_ORIGIN          required; the canonical https origin (a bad one fails the build)
//   OWNVITALS_PUBLIC_LEGAL_APPROVED  the legal gate; only the exact value `true` builds legal pages
// The output directory is the first argument (default dist-public). It is emptied first.
import { copyFileSync, mkdirSync, readFileSync, readdirSync, rmSync, writeFileSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { build, createServer } from 'vite'

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const out = resolve(root, process.argv[2] ?? 'dist-public')
const origin = process.env.OWNVITALS_PUBLIC_ORIGIN
if (!origin) throw new Error('OWNVITALS_PUBLIC_ORIGIN is required (an https origin, e.g. https://ownvitals.eu)')

const server = await createServer({ root, configFile: false, logLevel: 'error', appType: 'custom', server: { middlewareMode: true, hmr: false } })
let files
let images
try {
  const site = await server.ssrLoadModule('/src/public/site.ts')
  const imagesModule = await server.ssrLoadModule('/src/public/images.ts')
  files = site.siteFiles(origin, site.legalApproved(process.env.OWNVITALS_PUBLIC_LEGAL_APPROVED))
  images = imagesModule.imageFiles()
} finally {
  await server.close()
}

rmSync(out, { recursive: true, force: true })
for (const file of files) {
  const target = join(out, file.path)
  mkdirSync(dirname(target), { recursive: true })
  writeFileSync(target, file.content, 'utf8')
}

// One stylesheet: tokens.css, fonts.css, then public.css. The font files stay under /assets/,
// where the SPA build has the same content-hashed files, so none is copied here.
const scratch = join(out, '.css-build')
await build({
  root,
  configFile: false,
  logLevel: 'error',
  publicDir: false,
  build: { outDir: scratch, emptyOutDir: true, assetsInlineLimit: 0, rollupOptions: { input: { public: 'src/styles/public-entry.css' } } },
})
const css = readdirSync(join(scratch, 'assets')).filter((name) => /^public-.*\.css$/.test(name))
if (css.length !== 1) throw new Error(`expected one public stylesheet, found ${css.length}`)
writeFileSync(join(out, 'public.css'), readFileSync(join(scratch, 'assets', css[0])))
rmSync(scratch, { recursive: true, force: true })

mkdirSync(join(out, 'img'), { recursive: true })
for (const name of images) copyFileSync(join(root, '..', 'docs', 'design', 'mockups', 'png', name), join(out, 'img', name))
