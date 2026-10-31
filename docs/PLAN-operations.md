# ownvitals: plan, operations part

Sections 9 and 10 of [PLAN.md](PLAN.md) (Revision 2), split out to keep each file under 500 lines.
Section numbers match the references in PLAN.md.

---

## 9. Environments, hosting and legal

### 9.1 Local development (Windows, Docker Desktop)
- `compose.yaml`: web, worker, postgres, mailpit, fake-oidc, fake-withings.
- Worktrees in `../ownvitals-wt/`; LF-only scripts.
- **No real health data on the development machine**, which is where agents run. The local dev
  app only ever uses the demo account and fakes.

### 9.2 Home server (private phase)
| Piece | Setting |
|---|---|
| App host | `new-home-server` 192.168.1.222 (Ubuntu 26.04, 8 cores, 14 GB, Docker 29). Compose project `ownvitals`, image from GHCR (tag `v*`) |
| Ports | web on **`192.168.1.222:8096`**. Postgres not published |
| Reverse proxy | SWAG on 192.168.1.151: `site-confs/ownvitals.conf` for `withings.pernemark.se` → `http://192.168.1.222:8096`; the wildcard cert covers it. The webhook location has its own `access_log` format without the URI |
| DNS / Cloudflare | `withings` CNAME (you, manually). If proxied: SSL Full (strict), WAF skip rule for `/api/webhooks/withings/*`, trust `CF-Connecting-IP` only via SWAG |
| **Deploy mechanism** | A dedicated `ownvitals-deploy` user whose SSH key has a **forced command** (`command="/usr/local/bin/ownvitals-deploy",no-pty,no-port-forwarding`) that only accepts a release tag. It pulls that image, runs migrations and `up -d`, and records the previous tag. **No shell, no database, key or log access.** Agents get only this key, and use it only after your go-ahead |
| Rollback | `ownvitals-deploy <previous-tag>`. Migrations follow expand/contract, so the previous release runs on the new schema |
| Backups / DR | Nightly encrypted `pg_dump` + KEK backup (separately) to another machine. Targets: RPO 24 h, RTO 4 h. Quarterly restore drill |
| Hardening | Host firewall: 8096 only from .151; own Docker network; KEK files read-only, outside the DB |
| Shared-host risk | The host also runs a torrent client, flaresolverr, Portainer (Docker socket) and more. Acceptable for household use only |

**Real data gate (M2)**: no real Withings account connects on the home server until mandatory
2FA (M0), backups with a passing restore drill, rollback, log redaction and the deploy
mechanism are in place.

### 9.3 Legal (not legal advice)
- **Household phase**: GDPR doesn't apply to processing in purely personal or household activity
  (Art. 2(2)(c)). That depends on the **nature and scope of the processing** (whose data, for
  whom), not on where the server sits.
- **Before any non-household user** (even one friend), pass the M8 gate:
  - explicit consent (Art. 9(2)(a)) and a privacy notice
  - a record of processing
  - a **DPIA** (Art. 35)
  - Art. 32 security assessment
  - a breach procedure (IMY within 72 h) and data-subject rights
  - **processor agreements** (hosting, email, backups, Anthropic) and an international-transfer
    check
  - privileged-access controls
  - **medical-device classification review** of the intended purpose (Läkemedelsverket
    guidance), the experimental tier and AI features included
- **An EU VPS alone doesn't solve** processor agreements, transfers, backup security or admin
  access; those are separate items in the gate.
- Also check before M8: Withings developer terms (including the BYO and user-cap assumptions,
  D2) and a host name without the Withings trademark (e.g. `ownvitals.pernemark.se`).

---

## 10. Agentic development (classbeat's AGENTS.md model)

### 10.1 Roles
| Role | Model | Does |
|---|---|---|
| Owner (you) | — | Product decisions, external approvals (Withings apps, Cloudflare/SWAG, deploys), the probe run, the M8 gate |
| Architect / Orchestrator | Opus (main session) | Milestone → issues (one per PR, acceptance criteria, dependencies), ADRs, merge order, `docs/status.md` |
| Developer | Sonnet; **Opus for security-critical** | One issue → one PR in its own worktree; code, tests, CI fixes, all findings |
| Explorer | Haiku | Fixtures, `fake-withings` payloads from the public spec, docs research |
| Reviewer | **Fresh context per review**: Opus for security-critical, Sonnet otherwise | `Agent review (<model>): PASS/FAIL <sha>` on the PR |

**Security-critical**:
- token vault and rotation, OAuth state
- webhook receiver
- RLS, roles and `SECURITY DEFINER` functions
- jobs, leases and the rate limiter
- auth (M0 2FA, M3 passkeys and QR)
- export and delete
- deploy mechanism
- AI egress

### 10.2 Loop and merge gate (unchanged)
```
milestone → issues → Developer (worktree) → PR → CI → fresh Reviewer
   PASS <sha> → gh pr merge --squash --match-head-commit <sha>
   FAIL       → Developer fixes all blocking findings → CI → delta review
```
- Every push after a PASS needs a new (delta) review.
- Agents never `--admin`, `--auto`, approve, or override a FAIL.
- You can always merge on GitHub yourself.
- The gate is enforced by the agents (AGENTS.md/CLAUDE.md), not by GitHub checks.
- Health-specific review checks:
  - no real health data in tests, fixtures or logs
  - units and time zones correct
  - each rule has a tier and cites its source
  - experimental logic is behind the flag

### 10.3 Independent verification
A fresh model context is useful, but it shares failure modes with the model that wrote the code.
So:

- **Deterministic security tests run in CI on every PR**:
  - RLS and AST guards in every execution context
  - crypto and egress scans (from classbeat)
  - token-rotation crash tests
  - webhook fuzzing (forged, oversized, malformed, replayed)
  - auth-flow e2e
  - log-redaction tests
- **A human security review** (you, or someone you trust) is part of the M8 gate.

### 10.4 Status levels and real data
| Level | Means | Set by |
|---|---|---|
| IMPLEMENTED | Code and synthetic tests complete (fakes, demo account) | Agent, after the PASS merge |
| INTEGRATION_VERIFIED | Provider behavior confirmed with a real account (OAuth, webhook, data shapes) | **Owner only**, from the probe output |
| PRODUCTION_READY | Security, operations and deployment acceptance complete | Owner, at the milestone gate |

- Agents keep working past blocked external steps using fakes and **documented assumptions**
  (listed in `docs/status.md`). They **never** mark anything INTEGRATION_VERIFIED.
- Probe: `ownvitals probe --connection <id>` runs **on the home server**, run by you. It prints
  only shapes: meastypes present and their counts, ECG array count and length, sampling rate,
  attrib and position values, notification fields, capability states. **No values or
  timestamps.** You paste the output into the issue.

### 10.5 GitHub
- Public repo, so GitHub-hosted runners are free. No self-hosted runner on the home server: fork
  PRs could run code on it.
- `main` requires a PR plus green CI, squash only, linear history; `v*` tags protected.
- CODEOWNERS, Dependabot, commitlint, secret scanning and push protection.
- Release: tag → GHCR image → deploy via §9.2.
