// A stand-in for Google's back channel, for the e2e stack only (PLAN §3, ADR 0003 §11).
//
// Web talks to the real Google host names over https (the egress allowlist is code and cannot
// be pointed elsewhere). In the e2e compose network this container answers to
// oauth2.googleapis.com and www.googleapis.com with a certificate from the throwaway CA that
// e2e/scripts/stack-prepare.sh generates and adds to web's trust bundle:
//
//   POST /token               the code exchange; returns an RS256 ID token
//   GET  /oauth2/v3/certs     the JWKS
//
// The browser's visit to accounts.google.com never reaches this container: the Playwright test
// intercepts it and asks the control port (plain http, published on 127.0.0.1 only) for the
// redirect back to the callback:
//
//   POST /__test/authorize    { authorizeUrl, identity: { sub, email, email_verified, auth_time } }
//
// Like the live Google (ADR 0003 §1, amendment of 2026-11-08), the ID token carries no auth_time,
// whatever the request asked for (max_age=0, the claims parameter): Google ignores both. An
// identity with `auth_time: 'now'` gets the current time, as after a real sign-in at Google.
//
// It refuses to start unless OWNVITALS_ENVIRONMENT=test. Every identity is synthetic. It logs
// method and path only: no code, token, secret or address.
import { createHash, generateKeyPairSync, randomBytes, sign, timingSafeEqual } from 'node:crypto'
import { readFileSync } from 'node:fs'
import http from 'node:http'
import https from 'node:https'

if (process.env.OWNVITALS_ENVIRONMENT !== 'test') {
  console.error('fake-oidc: refusing to start unless OWNVITALS_ENVIRONMENT=test')
  process.exit(1)
}

const clientId = required('FAKE_OIDC_CLIENT_ID')
const clientSecret = readFileSync(process.env.FAKE_OIDC_SECRET_FILE ?? '/run/secrets/oauth_google_client_secret', 'utf8').trim()
const redirectBase = required('FAKE_OIDC_REDIRECT_BASE') // web's public base URL
const tlsPort = Number(process.env.FAKE_OIDC_TLS_PORT ?? 443)
const controlPort = Number(process.env.FAKE_OIDC_CONTROL_PORT ?? 9100)
const ISSUER = 'https://accounts.google.com'

function required(name) {
  const value = process.env[name]
  if (!value) {
    console.error(`fake-oidc: ${name} is required`)
    process.exit(1)
  }
  return value
}

const { privateKey, publicKey } = generateKeyPairSync('rsa', { modulusLength: 2048 })
const kid = randomBytes(8).toString('hex')
const jwks = { keys: [{ ...publicKey.export({ format: 'jwk' }), kid, alg: 'RS256', use: 'sig' }] }

const b64url = (input) => Buffer.from(input).toString('base64url')
const codes = new Map() // code -> { identity, nonce, challenge, redirectUri, authTime? }

function idToken(entry) {
  const now = Math.floor(Date.now() / 1000)
  const header = { alg: 'RS256', typ: 'JWT', kid }
  const claims = {
    iss: ISSUER,
    aud: clientId,
    azp: clientId,
    sub: entry.identity.sub,
    email: entry.identity.email,
    email_verified: entry.identity.email_verified ?? true,
    nonce: entry.nonce,
    ...(entry.authTime === undefined ? {} : { auth_time: entry.authTime }),
    iat: now,
    exp: now + 600,
  }
  const body = `${b64url(JSON.stringify(header))}.${b64url(JSON.stringify(claims))}`
  const signature = sign('RSA-SHA256', Buffer.from(body), privateKey)
  return `${body}.${signature.toString('base64url')}`
}

function safeEqual(a, b) {
  const left = Buffer.from(a)
  const right = Buffer.from(b)
  return left.length === right.length && timingSafeEqual(left, right)
}

function send(res, status, body, headers = {}) {
  const text = typeof body === 'string' ? body : JSON.stringify(body)
  res.writeHead(status, { 'content-type': 'application/json', 'cache-control': 'no-store', ...headers })
  res.end(text)
}

async function readBody(req) {
  const chunks = []
  let size = 0
  for await (const chunk of req) {
    size += chunk.length
    if (size > 16 * 1024) throw new Error('body too large')
    chunks.push(chunk)
  }
  return Buffer.concat(chunks).toString('utf8')
}

function clientCredentials(req, form) {
  const header = req.headers.authorization
  if (header?.startsWith('Basic ')) {
    const [id, secret = ''] = Buffer.from(header.slice(6), 'base64').toString('utf8').split(':')
    return { id: decodeURIComponent(id ?? ''), secret: decodeURIComponent(secret) }
  }
  return { id: form.get('client_id') ?? '', secret: form.get('client_secret') ?? '' }
}

async function token(req, res) {
  const form = new URLSearchParams(await readBody(req))
  const creds = clientCredentials(req, form)
  if (!safeEqual(creds.id, clientId) || !safeEqual(creds.secret, clientSecret)) {
    return send(res, 401, { error: 'invalid_client' })
  }
  const entry = codes.get(form.get('code') ?? '')
  codes.delete(form.get('code') ?? '') // single use, as at Google
  if (form.get('grant_type') !== 'authorization_code' || !entry) return send(res, 400, { error: 'invalid_grant' })
  if (form.get('redirect_uri') !== entry.redirectUri) return send(res, 400, { error: 'redirect_uri_mismatch' })
  const verifier = form.get('code_verifier') ?? ''
  const challenge = createHash('sha256').update(verifier).digest('base64url')
  if (verifier.length < 43 || !safeEqual(challenge, entry.challenge)) return send(res, 400, { error: 'invalid_grant' })
  return send(res, 200, {
    access_token: randomBytes(16).toString('hex'),
    token_type: 'Bearer',
    expires_in: 3599,
    scope: 'openid https://www.googleapis.com/auth/userinfo.email',
    id_token: idToken(entry),
  })
}

// The control step that stands in for Google's consent page.
async function authorize(req, res) {
  const { authorizeUrl, identity } = JSON.parse(await readBody(req))
  const query = new URL(authorizeUrl).searchParams
  const redirectUri = query.get('redirect_uri') ?? ''
  const problems = []
  if (query.get('client_id') !== clientId) problems.push('client_id')
  if (!redirectUri.startsWith(`${redirectBase}/oauth/google/callback`)) problems.push('redirect_uri')
  if (query.get('response_type') !== 'code') problems.push('response_type')
  if (query.get('code_challenge_method') !== 'S256' || !query.get('code_challenge')) problems.push('pkce')
  if (!query.get('state') || !query.get('nonce')) problems.push('state/nonce')
  if (!identity?.sub || !identity?.email) problems.push('identity')
  if (problems.length > 0) return send(res, 400, { error: 'bad_authorize', problems })
  const code = randomBytes(16).toString('hex')
  codes.set(code, {
    identity,
    nonce: query.get('nonce'),
    challenge: query.get('code_challenge'),
    redirectUri,
    authTime: identity.auth_time === 'now' ? Math.floor(Date.now() / 1000) : undefined,
  })
  const location = new URL(redirectUri)
  location.searchParams.set('code', code)
  location.searchParams.set('state', query.get('state'))
  return send(res, 200, { location: location.href })
}

function handler(control) {
  return async (req, res) => {
    const path = new URL(req.url ?? '/', 'http://fake-oidc').pathname
    console.log(`${req.method} ${path}`)
    try {
      if (req.method === 'POST' && path === '/token') return await token(req, res)
      if (req.method === 'GET' && path === '/oauth2/v3/certs') {
        return send(res, 200, jwks, { 'cache-control': 'public, max-age=300' })
      }
      if (control && req.method === 'POST' && path === '/__test/authorize') return await authorize(req, res)
      if (control && req.method === 'GET' && path === '/__test/ready') return send(res, 200, { ready: true })
      return send(res, 404, { error: 'not_found' })
    } catch {
      return send(res, 400, { error: 'bad_request' })
    }
  }
}

https
  .createServer(
    { key: readFileSync(process.env.FAKE_OIDC_TLS_KEY ?? '/certs/oidc.key'), cert: readFileSync(process.env.FAKE_OIDC_TLS_CERT ?? '/certs/oidc.pem') },
    handler(false),
  )
  .listen(tlsPort, '0.0.0.0', () => console.log(`fake-oidc: https on ${tlsPort}`))

http.createServer(handler(true)).listen(controlPort, '0.0.0.0', () => console.log(`fake-oidc: control on ${controlPort}`))
