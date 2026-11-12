# The owner's local real-account stack

Issue #450. Owner decision of 2026-11-12 (AGENTS.md "Health data", "The one exception"): a local
Claude Code session **on the owner's own computer**, started by the owner, may work with **the
owner's own Withings account** in a local stack on that machine. It settles the documented
assumptions of [docs/status.md](../status.md) faster than the home-server probe alone.

It is not for cloud or remote sessions (claude.ai, Claude Code on the web, any container), CI,
subagents, another computer, the home server, or anyone else's account, the household's other
members included. **An agent that is not that local session stops here.** An agent that cannot
tell whether it runs on the owner's own computer assumes it does not.

Nothing in this file changes a status level: results are shape-only evidence for the owner, who
settles each assumption and sets INTEGRATION_VERIFIED. Agents never write it.

## Do not (hard rules)

- No real value outside the local stack and the git-ignored `local-real/` directory: no
  measurement, ECG sample, timestamp, date, time zone, device or Withings user id, connection
  id, comment, token, client secret, authorization code, callback URL or webhook path in a
  commit, branch name, PR, issue, comment, CI log, committed file or fixture.
- What leaves the machine is either **shape-only** (as the probe and the assumption checker
  print: codes from fixed sets, counts, field names from fixed lists, fixed verdicts) or a
  **generated synthetic fixture** (below). Read every line before you paste it; when in doubt,
  leave it out and say which line kind it was.
- The real-account worktree **never commits and never pushes**: `scripts/local-real-guard.sh`
  refuses both while `local-real/REAL-ACCOUNT` exists (also when the script itself is missing),
  and `seal` breaks the worktree's push URL so `git push --no-verify` fails too. Code and
  fixtures are committed from a normal issue worktree.
- No database dump, `COPY ... TO`, export (the app's data export included), screenshot or copy
  of the real data anywhere, including `local-real/`. Query results that hold values stay in the
  session's terminal; files in `local-real/` hold shape-only output only.
- No public tunnel, no webhook from Withings, and no new or changed Withings developer app
  without the owner's explicit go-ahead for that action (AGENTS.md "Safety envelope").
- No real data to any other model call: AI stays off in this stack (`anthropic_api_key` empty).
- When the work is done: `docker compose ... down -v` (the volume holds the real data), and
  delete `local-real/` and the Withings client secret file.

## 1. The worktree

A detached worktree, so there is no branch to push, with its own compose project, database
volume and ports (`9001` is the slot used below: web `127.0.0.1:29001`, Postgres `39001`, Mailpit
`49001`; any free number from 1 to 9999 that no issue worktree uses works):

```bash
cd D:/DeveloperArea/Repos/withings
git fetch origin
git worktree add --detach ../ownvitals-wt/local-real origin/main
cd ../ownvitals-wt/local-real
scripts/worktree.sh init 9001          # .env and secrets/ (random values), both git-ignored
scripts/local-real-guard.sh install    # pre-commit and pre-push hooks (shared by every worktree)
mkdir -p local-real && touch local-real/REAL-ACCOUNT   # marks this worktree: no commit, no push
scripts/local-real-guard.sh seal       # this worktree's push URL goes nowhere (see below)
```

`.env`, `.env.*` (so `.env.local`), `secrets/`, `local-real/`, probe output and database dumps
are git-ignored, and the guard refuses them when staged (pre-commit), from this worktree at all
(pre-commit and pre-push), and anywhere in the tree (CI step "No local real-account files
tracked"). With `core.hooksPath` set, `install` writes there. The guard also refuses `.envrc`,
`.envrc.*`, `secret/`, plain `.sql` outside `docker/postgres/<area>/`, archives (`.tar`, `.tgz`, `.zip`, the
app's export included), `.har` and `.csv` other than `synthetic_*.csv`.

Hooks are skipped by `git commit --no-verify` and `git push --no-verify`. `seal` covers the push
side: it turns on `extensions.worktreeConfig` and sets `remote.<name>.pushurl` to
`local-real-no-push://this-worktree-never-pushes` for every remote **in this worktree's own
config only** (`git config --worktree`), while the other worktrees push as before. `pushurl` is
multi-valued and git pushes to every value in turn, so `seal` refuses while any
`remote.<name>.pushurl` is set in the shared, global or system config (remove it first; `git
config --show-origin --get-regexp 'remote\..*\.pushurl'` shows where), and checks afterwards
that the sealed URL is each remote's only push URL. Then any push from here fails before it
connects. It also refuses without the marker and when the shared config sets `core.worktree`.
Check it with `git config --get-all remote.origin.pushurl` (it must print only the
`local-real-no-push://` URL; a dry run is not enough, since it reports success for a real URL
listed first) and `git push --dry-run --no-verify origin HEAD:refs/heads/x` (it must fail with
"local-real-no-push"); seal again after adding a remote. It does not stop a push to a
URL typed on the command line, and nothing stops a `--no-verify` commit: the worktree is detached
and sealed, so such a commit cannot leave it (and CI refuses the paths in any PR). Removing
the worktree (section 7) removes its config with it.

Two limits of `seal`. A `url.<base>.insteadOf` (or `pushInsteadOf`) in any config scope that
matches the sealed URL (`local-real-no-push://...`) rewrites it to a real one at push time, so
never set one for that scheme; `git ls-remote --get-url origin` and `git remote get-url --push
origin` show the URL git will really use. And `seal` checks the config as it is when it runs:
**run it again after any git config change** (a new remote, a global or included file, a rewrite
rule), then repeat the two checks above. An included file's push URL is caught by the final check
(`still has another push URL`), but only when `seal` runs.

The base `compose.yaml` mounts `connection_kek` for web only. Write the overlay that also gives
it to `migrate` (to store the app's client secret) and to the worker (to open the grant), inside
the git-ignored directory:

```yaml
# local-real/compose.local.yaml (never committed)
services:
  migrate:
    secrets: [connection_kek]
  worker:
    secrets: [connection_kek]
```

```bash
dc() { docker compose -f compose.yaml -f local-real/compose.local.yaml "$@"; }
dc up -d --build --wait
```

Do not use the e2e overlay (`compose.e2e.yaml`): it points the worker's CA bundle at the fake.

## 2. The Withings app and the callback

Use a **separate Withings developer app** for local work, never the production app: the
production app's callback is the public site, so a local connect would hand the authorization
code to production. Registering it is the owner's action (AGENTS.md "Safety envelope").

`withings-apps create` stores an `https` callback origin (a port is allowed). The OAuth redirect
is a browser redirect, so it never needs a public address:

1. Register the dev app with the callback `https://localhost:29001/api/oauth/withings/callback`.
   **Unverified:** whether Withings accepts a `localhost` callback for a dev app. If it refuses,
   use a name the owner controls that resolves to `127.0.0.1` in the hosts file of this computer
   only (for example `local.<owner's domain>`), with the same port and path; no DNS change.
2. Put the client secret in `secrets/withings_client_secret` (git-ignored, one line) and store
   the app with the secret on stdin, as the e2e helper does:

   ```bash
   dc run --rm -T --no-deps migrate python -m ownvitals withings-apps create \
     --client-id "$WITHINGS_DEV_CLIENT_ID" --callback-base https://localhost:29001 \
     --secret-file /dev/stdin < secrets/withings_client_secret
   ```

   Set `WITHINGS_DEV_CLIENT_ID` in the shell only, not in a committed file.
3. Sign up in the local app (`http://127.0.0.1:29001`): an invite code from
   `dc exec web python -m ownvitals invite create`, the account mails in Mailpit at
   `http://127.0.0.1:49001`. Use a local address, not a real mailbox.
4. Connect Withings from the onboarding screen. After consent, Withings sends the browser to
   `https://localhost:29001/api/oauth/withings/callback?...`, which the plain-HTTP stack cannot
   answer. Change only the start of that address in the address bar to
   `http://127.0.0.1:29001` and keep the path and query (the e2e suite does the same,
   [e2e.md](e2e.md) "The connect journey"). The query holds a one-time code: never copy it
   anywhere else. The session cookie belongs to `127.0.0.1`, so sign in there, not on
   `localhost`.

**Watch the production connection afterwards.** The fake assumes a new authorization drops the
earlier tokens *of the same app*. If Withings drops them across apps, the home server's
connection goes to `needs_reconnect`; that is the `new_grant_drops_old` check below.

## 3. Webhooks: none locally

Withings cannot reach this stack, so the nightly reconciliation's subscribe calls fail (Withings
checks the callback URL) and no notification arrives. That is expected and harmless: the
connect job queues every backfill, and the scheduled `withings_poll` (every 6 hours,
`worker/kinds.py`) keeps syncing. A tunnel (any service that gives the stack a public URL) needs
the owner's explicit go-ahead first: it sends real notifications through a third party and
changes the dev app's callback.

## 4. Connect, backfill, sync and the probe

- **Connect** queues `withings_connect`, which reads devices, goals and capabilities and queues
  one `withings_backfill` per stream the grant allows ([connections.md](connections.md),
  [jobs.md](jobs.md)). Follow it with `dc exec worker python -m ownvitals jobs stats` (counts
  only) and the worker log (`dc logs worker`; the log carries no values by design, but it stays
  on this machine all the same).
- **Sync**: the 6-hourly poll; nothing else is needed.
- **Probe** (the same command as the home-server runbook, [m2-probe.md](../runbooks/m2-probe.md)):
  the connection id is the `id` of the `withings` row of `http://127.0.0.1:29001/api/connections`.

  ```bash
  dc exec -T worker python -m ownvitals probe --connection "$CONN" --json > local-real/probe.json
  dc exec -T worker python -m ownvitals.withings.probe_assumptions_cli < local-real/probe.json
  ```

  The second command prints one `assumption <id> <verdict> <note>` line per documented
  assumption (`pass`, `fail`, `no_data`, `manual`) and nothing read from the input, not even a
  count: it is the part to paste. Exit code 1 means at least one `fail`. The probe's own text
  output is shape-only too, but it carries the connection id: leave that line out of anything
  you paste.

## 5. Turning findings into something committable

**(a) Shape-only reports.** The assumption checker's output, and the probe's lines except
`connection`. A finding that the shapes do not cover is written as a rule, not as data: "meastype
X occurs without its partner", "the field Y is absent", "status Z answered a refused refresh",
never with a value, a date or a count that would identify a day.

**(b) Synthetic fixtures.** Never edit, scrub or trim a real payload into a fixture: a missed
field leaks, and a real value cannot be told from a synthetic one later. Instead **generate** the
fixture from the shape: extend the seeded generator (`backend/tests/withingsdata.py`,
`withingsstreams.py`, `withingsshapes.py`) so it produces the shape seen (the meastype, the
field, the array count and length, the sampling rate, the enum value), with values from its own
seeded ranges. Name the file `synthetic_*` or mark it `synthetic: true`, as AGENTS.md requires,
and write it in a normal issue worktree, not this one. The real payload stays in the local stack
only.

## 6. Assumption checklist

Each row of "Assumptions" in [docs/status.md](../status.md) and its check here. `checker` means
the id printed by `probe_assumptions_cli`; a `manual` check is done in this stack and reported as
a shape-only sentence. Queries run as the superuser (forced RLS hides the rows from the other
roles) through `dc exec db psql -U postgres ownvitals`, and print **counts only**: never
`SELECT *`, never a value column, never `GROUP BY` a value.

| Assumption (docs/status.md) | Check |
|---|---|
| API hosts and the token endpoint `POST /v2/oauth2` | checker `hosts_token_endpoint`; the connect itself exercises the code exchange |
| `attrib` codes are the documented ones | checker `attrib_documented` |
| `attrib = 1` is an ambiguous device reading | checker `attrib_1_ambiguous` (occurs?), then compare in the Withings app who took such a reading; report yes or no |
| `attrib = 15` marks a guided reading (N2) | checker `attrib_15_guided` (occurs?), then compare with a reading you took guided |
| Foot types 158/159 and 197/198 pair by `grpid` (N5) | checker `n5_foot_pairs` |
| ECG A2: lead unknown, `wearposition` | checker `ecg_a2_lead`; the label itself is OC2 |
| ECG A3: 300 or 500 Hz | checker `ecg_a3_rate` |
| ECG A5: one array per recording | checker `ecg_a5_one_array` |
| ECG A6: strips last about 30 s | checker `ecg_a6_strip_30s` |
| ECG A7: groups 135 to 138 and 130 belong to the ECG | checker `ecg_a7_groups` (counts); the device and timestamp link is OC5 (`ownvitals ecg validate`, [ecg.md](ecg.md)) |
| ECG A1, A4, A8, A9 and the OC thresholds | `dc exec worker python -m ownvitals ecg validate --user <uuid>` (counts, yes/no and percents only); OC1 compares with the Withings app by eye |
| Time zones are IANA names (`source_tz`) | checker `source_tz_iana` (manual): `SELECT count(*) FILTER (WHERE source_tz IN (SELECT name FROM pg_timezone_names)), count(*) FILTER (WHERE source_tz IS NOT NULL) FROM sync_cursors;` both numbers equal |
| Sleep summary field names (`SLEEP_FIELDS`), units | checker `sleep_summary_fields` (manual): `dc exec worker python -m ownvitals analysis check --user <uuid>` and the scores' `skipped`/`invalid` counts; report which field names were missing, never values |
| Device battery enum `high`/`medium`/`low` (Q1) | checker `battery_enum` (manual): `SELECT count(*) FILTER (WHERE battery IN ('high', 'medium', 'low')), count(*) FILTER (WHERE battery IS NOT NULL) FROM devices;` both numbers equal |
| Refused refresh token answers 2553 | checker `refresh_status_2553` (manual): only seen if a refresh is refused; the probe's `call` line shows the status |
| `blocked_by_provider_plan` signal | checker `provider_plan_signal` (manual): only an account without Withings+ shows it |
| A new authorization drops earlier tokens | checker `new_grant_drops_old` (manual): the home server's connection status after the local connect |
| Webhook form, retries, the 3-s answer | checker `webhook_form` (manual): not on this stack (no public URL); the home server's first webhook |
| `getmeas` reports no deletions | checker `getmeas_deletions` (manual): delete a test reading in the Withings app, sync, and say whether the stored count changed |
| Fake readings of #63, #66, #71, #156 to #161 (statuses, scopes, notify semantics, rate limit) | covered only where a row above sees them; the rest stay with the home server's first real connect, sync and webhook |
| Read API, metrics, scores, UI rows (#233, #290, #292, #295) | look at the local screens and `/metrics`; report which state each card showed, not its numbers |

Report the results on the issue the Architect names, as the checker's lines plus one shape-only
sentence per manual row. The owner decides what is settled.

## 7. Cleaning up

```bash
dc down -v                      # deletes the database volume with the real data
rm -rf local-real secrets/withings_client_secret
cd .. && git worktree remove --force local-real
```

Then, in the Withings developer portal, the owner may revoke the dev app's access or delete the
app.
