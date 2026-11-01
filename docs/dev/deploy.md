# The deploy mechanism

`deploy/ownvitals-deploy` is the forced command of the home server's deploy key
(PLAN-operations §9.2). It is the release trust boundary (threat model T62): neither the `v*` tag
nor `release.yml`'s own checks are, because anyone who can push can create a `v*` tag and that
commit's `release.yml` runs. An image runs on the server only after the script has checked the
tag, `main`, the highest release deployed so far and the build-provenance attestation, and it is
pulled by digest only.

This page is the single source of the verification recipe: [release.md](release.md) says how an
image is built and attested, this page says how it is checked before it runs.

**Status: IMPLEMENTED, tested with fakes only.** Nothing has been installed or deployed. Installing
it on the home server is the owner's step, and agents use the deploy key only after the owner's
go-ahead (AGENTS.md, safety envelope).

## Commands

The key runs nothing else. The script reads only `SSH_ORIGINAL_COMMAND`, which must be exactly one
of:

```bash
ssh ownvitals-deploy@192.168.1.222 deploy vX.Y.Z sha256:<64 hex>   # the index digest from the release notes
ssh ownvitals-deploy@192.168.1.222 rollback
```

Anything else is refused before anything runs: extra or missing words, two spaces, a tab, a
newline, another command. The raw command is never echoed back. Each step prints one line,
`ok    <check>: ...` or `FAIL  <check>: ...`, and the script never prints a secret, an environment
file or a container's output.

| Exit | Means |
|---|---|
| 0 | deployed (or rolled back) |
| 1 | refused: a check failed or the command is not allowed; nothing was recorded and the running release is unchanged. A refusal at the state file (step 13) comes after the migrations: the running release then works on the migrated schema (expand/contract) |
| 2 | the new pair is recorded (a deploy's migrations ran), but its services were not started: the override could not be put in place, or `up -d` failed. After a deploy, `rollback` returns to the release before; after a rollback, `deploy` of the highest release with its digest goes forward again |
| 75 | another deploy or rollback holds the lock; nothing was done |

## The checks, in order

`deploy <tag> <digest>`:

1. **Arguments.** The tag is `vMAJOR.MINOR.PATCH` with `release.yml`'s numeric rule (no leading
   zeros) and **no pre-release or build metadata**: the deploy takes releases only. The digest is
   `sha256:` and 64 lower-case hex digits. Both are checked before either is used in an API path,
   an identity or an image reference. `LC_ALL=C` comes first, so a locale sent over SSH changes
   no character range.
2. **One at a time.** `flock --nonblock` on `/var/lib/ownvitals-deploy/lock`; a second deploy or
   rollback exits 75 without calling anything.
3. **Never below the highest release.** The state records the highest release ever deployed
   (tag, commit and digest), and a rollback never lowers it. The tag must be a **higher version**
   than the highest's, compared as numbers field by field (`v1.10.0` is newer than `v1.9.0`). The
   highest version itself passes only with its recorded digest: an idempotent redeploy, or
   forward again after a rollback; with another digest it is refused, since a release is never
   replaced. With nothing recorded (the first deploy), any release that passes the other checks
   may be the first. *Why:* a stolen key must not install an older, attested release with a known
   vulnerability, also not one between the release a rollback went back to and the one it left
   (v1.0.0, v1.2.0, `rollback`, then v1.1.0 is refused). Going back is `rollback` only: one
   step, through the other checks but 8 (below). Check 8 adds the same rule for history.
4. **gh.** The read-only token file exists, and `gh --version` is at least **2.102.0**.
   `--source-ref` and `--source-digest` exist since gh 2.68.0; 2.93.0 stops sending the token to
   TUF mirrors, and 2.102.0 makes `--source-ref` match case-sensitively. gh runs with only the
   token, its own config directory (`/var/lib/ownvitals-deploy/gh`), no prompts and a 120 s
   timeout per call.
5. **The tag's commit**, through the full ref name, so a branch of the same name is never read:
   `gh api repos/magpern/ownvitals/commits/refs/tags/<tag> --jq .sha` (40 hex digits).
6. **The tag is protected** (PLAN-operations §9.2 (1)). Of the repository's rulesets
   (`rulesets?includes_parents=true&targets=tag`), one must have, read through
   `rulesets/<id>` (an id of 1 to 19 digits; anything else is refused): target `tag`; enforcement
   `active` (not `evaluate` or `disabled`); an include pattern `refs/tags/v*` (or `~ALL`); no
   exclude pattern that may match `refs/tags/<tag>` (bash's `*` also crosses `/`, so this
   over-matches, and a pattern with escapes counts as matching: both refuse); the rules `update`
   and `deletion`; and `current_user_can_bypass` `never`. Today that is the `release tags` ruleset
   (`refs/tags/v*`; `update`, `deletion`, `non_fast_forward`; no bypass actors).
   *Why, and what it does not do:* it keeps a recorded `(tag, digest)` pair from changing under
   the record: the tag cannot move to another commit or be deleted and re-created. It does not
   stop a tag being **created** on any commit (there is no `creation` rule, an Architect decision
   while the owner's token is the only pusher). A read-only token is not shown the bypass list;
   `current_user_can_bypass` is GitHub's answer for the token's own user, so it catches **the
   owner's role on the bypass list** (on this personal repository, the repository admin role),
   not an app or deploy keys on it. If GitHub leaves the field out for the token, every deploy
   refuses: the owner's first deploy confirms it is there. Checks 7, 8 and
   9 are what stop a tag outside `main` or behind the highest release (T62).
7. **`main` contains the commit:**
   `gh api "repos/magpern/ownvitals/compare/<commit>...refs/heads/main?per_page=1" --jq .status`
   is `identical` or `ahead`. Always `refs/heads/main`: a bare `main` could resolve to a tag of
   that name, which the `v*` ruleset does not cover.
8. **History**, when a release is recorded: the commit is the highest release's commit or
   descends from it,
   `gh api "repos/magpern/ownvitals/compare/<highest commit>...<commit>?per_page=1" --jq .status`
   is `identical` or `ahead`. Anyone who can push a tag can put a higher-numbered `v*` tag on an
   older commit of `main`, and that commit's `release.yml` attests it: this refuses it. Rollback
   skips it, as it skips check 3.
9. **The attestation**, with the policy `release.yml` verifies its own attestation with:

   ```bash
   gh attestation verify "oci://ghcr.io/magpern/ownvitals@<digest>" \
     --repo magpern/ownvitals \
     --cert-identity "https://github.com/magpern/ownvitals/.github/workflows/release.yml@refs/tags/<tag>" \
     --cert-oidc-issuer https://token.actions.githubusercontent.com \
     --source-ref "refs/tags/<tag>" \
     --source-digest "<commit>" \
     --deny-self-hosted-runners \
     --predicate-type https://slsa.dev/provenance/v1
   ```

   The exact `--cert-identity` pins the signer to `release.yml` run for exactly that tag (stricter
   than `--signer-workflow`, which gh makes exclusive with it); `--source-digest` ties the
   attestation to the commit check 7 found on `main`. *Assumption, confirmed by the first real
   release (the owner's step):* `commits/refs/tags/<tag>` peels an annotated tag to its commit
   (checked on a public repository), and `release.yml` attests `GITHUB_SHA`, which for a tag push
   is that commit, so `--source-digest` matches for an annotated tag as for a lightweight one.
10. **Pull by digest:** `docker pull --quiet ghcr.io/magpern/ownvitals@<digest>`, never a tag,
    under `timeout 600` (`PULL_TIMEOUT`): a pull that hangs is stopped and refused, so it never
    holds the lock.
11. **Compose.** `/opt/ownvitals/compose.yaml` has the services `migrate`, `db-functions`, `web`
    and `worker`. The script writes an override that gives each of them
    `image: ghcr.io/magpern/ownvitals@<digest>`
    and `pull_policy: never`, and gives `migrate` **`scale: 0`**. With it,
    `docker compose config --images` may name the image only by that digest: a service that names
    it by a tag (or another digest) is refused, since running it would be a pull by tag. The script
    recognises the image only when it is spelled exactly `ghcr.io/magpern/ownvitals` (in any case);
    another spelling of it (`ghcr.io:443/…`, an image id) is not recognised, so the compose file
    must not use one (install step 5). Last, the resolved `docker compose config` (with the
    override) must show, under `db-functions`, no `volumes`, `volumes_from`, `configs`, `develop`
    (Compose resolves `extends` before it prints, so an extended service's volumes show as
    `volumes`), and `secrets` exactly `postgres_password` at its default target
    (`/run/secrets/postgres_password`). Compose normalises every volume spelling to `volumes:`, so a
    bind mount, a named volume or a tmpfs volume is refused, and so is a config or a secret placed
    at an SQL path (its PGPASSFILE tmpfs is the `tmpfs:` key). The functions then come from the
    release image only. Checked against Compose v5.5.1's output.
12. **Migrations:** `docker compose run --rm -T --pull never migrate` with the new override. This
    is the only step that runs migrations: `run` runs `migrate` whatever its scale, while `up -d`
    starts no service with scale 0. If it fails, nothing is recorded and the running containers
    are untouched.
13. **State:** the new pair is recorded as current, the pair it replaces as previous, and this
    release as the highest; then the override becomes
    `/var/lib/ownvitals-deploy/compose.image.yaml`. This happens after the migrations and before
    `up -d`, so a failed start still leaves `rollback` pointing at the release that ran before.
    Redeploying the current pair keeps the previous one. If the state file cannot be written, the
    deploy exits 1 with nothing recorded; if the override cannot be put in place after that, it
    exits 2.
14. **Start:** `docker compose up -d --pull never --no-build`. It starts every service but
    `migrate`, and does not wait for `migrate` even when `web` and `worker` depend on it
    (`service_completed_successfully`, directly or through `db-functions`). `db-functions` runs
    the release's image, so this installs the release's database functions before `web` and
    `worker` start (below, "Database functions").

Every compose call is
`docker compose --project-name ownvitals --project-directory /opt/ownvitals --file /opt/ownvitals/compose.yaml [--file <override>]`.

`rollback` takes the recorded previous pair and runs checks 2, 4–7 and 9–11 on it, the same as a
deploy (it skips checks 3 and 8: going back one step is what it is for), then records it as
current with no previous pair, keeps the highest release, and runs `up -d`. **It runs no
migrations:** it has no step 12, and `up -d` does not start `migrate` (`scale: 0`), so the
previous release's `alembic upgrade head`, which would not know the newer revision, never runs,
provided the compose file meets install step 5. The previous release runs on the newer schema
(expand/contract, PLAN-operations §9.2), **with its own database functions**: `up -d` runs
`db-functions` from the previous release's image, which re-installs that release's functions. It
goes back one step only; a second `rollback` is
refused until the next deploy. The way forward again is `deploy` of the highest release with its
digest, or of a newer one; the release rolled back to is below the highest, so `deploy` refuses
it. If the previous pair no longer passes a check (its attestation, its tag ruleset, its commit on
`main`), the rollback is refused and nothing changes.

### Database functions

The job queue's `SECURITY DEFINER` functions (`20-queue.sql`, ADR 0005 section 3) are installed by
the one-shot `db-functions` as the database superuser, on every `up -d`. It runs the application
image: the SQL and its entrypoint ship in the image (`/app/db-functions`), and nothing is read from
the server's disk ([stack.md](stack.md), "Where the database's SQL comes from"). The override pins
`db-functions` to the release digest like `migrate`, `web` and `worker`, and check 11 refuses a
compose file that lacks it. So:

| After | The functions installed are |
|---|---|
| `deploy vX.Y.Z` | those of vX.Y.Z (step 14 runs its `db-functions`) |
| `rollback` | those of the release rolled back to: its `db-functions` re-installs them, replacing the newer release's |
| `deploy` of the highest release again | those of the highest release |
| a failed `up -d` (exit 2) | those of whichever release's `db-functions` last completed; `ps -a` shows it |

A release whose image predates `/app/db-functions` (none exists: this mechanism came before the
first release) could not be rolled back to: its `db-functions` would fail and `up -d` exit 2.

The previous release's functions must work on the newer schema, as its code must
(expand/contract). The script replaces every function it manages, so a rollback leaves no newer
version of them behind; a function the newer release added and the older script does not know
makes it stop with a message naming it (its foreign-code check), and the rollback's `up -d` fails
(exit 2, below).

### A failed rollback

If a rollback's `up -d` fails (exit 2), the rollback is already recorded: current is the release
rolled back to, there is no previous pair, and `/var/lib/ownvitals-deploy/compose.image.yaml`
pins its digest. The key cannot retry it (`rollback` refuses without a previous pair); it can only
go forward with `deploy <highest tag> <its digest>`. To retry the rollback itself, on the server
as root (the owner's step), with the recorded override and nothing else:

```bash
o=/var/lib/ownvitals-deploy/compose.image.yaml
c=(docker compose --project-name ownvitals --project-directory /opt/ownvitals
   --file /opt/ownvitals/compose.yaml --file "$o")
"${c[@]}" ps -a
"${c[@]}" logs db-functions web worker   # may show data-free errors only; never paste them anywhere
# fix the cause, then start exactly what the record says:
"${c[@]}" up -d --pull never --no-build
```

Never edit the state file or the override by hand, and never run `migrate` here: the override
gives it scale 0, and `up -d` leaves it alone. If the older release cannot run on the schema (or
its functions script refuses it), go forward instead: `deploy` of the highest release with its
digest.

### Checking a release by hand

The same checks, for a release you want to look at before asking for a deploy:

```bash
tag=vX.Y.Z digest=sha256:<64 hex>
[[ $tag =~ ^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$ && $digest =~ ^sha256:[0-9a-f]{64}$ ]]
sha=$(gh api "repos/magpern/ownvitals/commits/refs/tags/$tag" --jq .sha)
gh api "repos/magpern/ownvitals/rulesets?includes_parents=true&targets=tag" --jq '.[].id' |
  while read -r id; do
    gh api "repos/magpern/ownvitals/rulesets/$id" \
      --jq '{target, enforcement, current_user_can_bypass, ref_name: .conditions.ref_name,
             rules: [.rules[].type]}'
  done   # one: tag, active, never, include refs/tags/v*, no matching exclude, update and deletion
gh api "repos/magpern/ownvitals/compare/$sha...refs/heads/main?per_page=1" --jq .status   # identical or ahead
highest=<the highest_commit line of the server's state file>
gh api "repos/magpern/ownvitals/compare/$highest...$sha?per_page=1" --jq .status   # identical or ahead
gh attestation verify "oci://ghcr.io/magpern/ownvitals@$digest" \
  --repo magpern/ownvitals \
  --cert-identity "https://github.com/magpern/ownvitals/.github/workflows/release.yml@refs/tags/$tag" \
  --cert-oidc-issuer https://token.actions.githubusercontent.com \
  --source-ref "refs/tags/$tag" --source-digest "$sha" \
  --deny-self-hosted-runners --predicate-type https://slsa.dev/provenance/v1
```

## State

`/var/lib/ownvitals-deploy/` (root, `0700`):

| File | Holds |
|---|---|
| `state` | seven lines, in this order: `current_tag=`, `current_digest=`, `previous_tag=`, `previous_digest=` (previous empty when there is none), `highest_tag=`, `highest_commit=`, `highest_digest=` (the highest release ever deployed; a rollback leaves it). Anything else makes every command refuse until it is fixed by hand |
| `compose.image.yaml` | the override for the recorded current digest; use it for manual compose commands: `docker compose -f compose.yaml -f /var/lib/ownvitals-deploy/compose.image.yaml ps` |
| `lock` | the `flock` file |
| `gh/` | gh's own config directory (nothing in it is needed) |

The script runs with `umask 077`: the files it creates are `0600` and `gh/` is `0700`.

## Install (the owner's step, on 192.168.1.222)

Do this from a checkout of `main` at the commit that merged the reviewed script. Nothing here is
done by an agent.

**1. gh.** Install gh **2.102.0 or later** from GitHub's apt repository
(<https://github.com/cli/cli/blob/trunk/docs/install_linux.md>); Ubuntu's own package is older.
Check `gh --version`.

**2. The script and its directories:**

```bash
sudo install -o root -g root -m 0755 deploy/ownvitals-deploy /usr/local/bin/ownvitals-deploy
sudo install -d -o root -g root -m 0700 /etc/ownvitals-deploy /var/lib/ownvitals-deploy
```

**3. The read-only GitHub token.** A fine-grained personal access token of `magpern`, repository
access *only* `magpern/ownvitals`, permissions *Contents: read*, *Metadata: read* and
*Attestations: read*, nothing else, with an expiry in your calendar (an expired token makes
every deploy refuse at the first API call). Store it without echoing it into the shell history:

```bash
sudo install -o root -g root -m 0600 /dev/null /etc/ownvitals-deploy/github-token
sudoedit /etc/ownvitals-deploy/github-token        # paste the token, one line
```

**4. GHCR.** If the package is private (ADR 0001 leaves that to you), log root's Docker in with
a token that has only `read:packages`: `sudo docker login ghcr.io -u magpern --password-stdin`.
gh reads the same credentials (`/root/.docker/config.json`) for the attestation's image. A public
package needs nothing.

**5. Docker Compose and the compose project.** `docker compose version` must be **v5.5.1** or
later: the version the override was verified with on Docker Desktop, with synthetic services in
the repository's `depends_on` chain (`config --services` lists a `migrate` at `scale: 0`,
`run --rm -T --pull never migrate` runs it, and `up -d` neither starts it nor waits for it while
`web` and `worker` depend on it through `db-functions`). Older versions are not verified.

In `/opt/ownvitals`: `compose.yaml` (the production stack), `.env` (root, `0600`) and
`secrets/` (root, `0700`). Images other than ours (the database's) must already be pulled: the
deploy runs compose with `--pull never`. `compose.yaml` **must**:

- have the services `migrate`, `db-functions`, `web` and `worker` (the script refuses without
  them); the override replaces their `image:`, so it can be `ghcr.io/magpern/ownvitals`;
- run `db-functions` as the repository's `compose.yaml` does: its entrypoint
  `["sh", "/app/db-functions/db-functions.sh"]` from the image, the `postgres_password` secret, the
  `/run/pgpass` tmpfs (uid 10001), and **no volumes**;
- run the database image by digest (`postgres:17.11@sha256:...`, the one in the repository's
  `compose.yaml`), pulled by that digest beforehand;
- run the migrations (`alembic upgrade head`) in `migrate` only, as a service the override can
  scale to 0: no `profiles` (the script needs it in `config --services`) and no
  `deploy.replicas` other than 0 (compose refuses it beside `scale: 0`, and so does the deploy;
  a `scale` of its own is overridden). `web` and
  `worker` may depend on it with `service_completed_successfully`, directly or through
  `db-functions`.

It **must not**:

- run `alembic`, or any other migration, on start anywhere else: not in the command or
  entrypoint of `web`, `worker` or any other service, and not in a second migrate-like service.
  `up -d` starts every service whose scale is not 0, so such a path would run on every `up -d`,
  and a rollback would run the older release's migrations against the newer schema;
- name our image other than as `ghcr.io/magpern/ownvitals` spelled exactly (`ghcr.io:443/…`, an
  image id): the script would not recognise it (check 11);
- mount anything into `db-functions` (above all `/app/db-functions`), or give any service a
  host path that holds SQL other than `db`'s first-start init directory: the functions would then
  come from the server's disk, and a deploy or rollback would not change them.

**6. The deploy user**, with no shell access, no groups and a home it can't write:

```bash
sudo install -d -o root -g root -m 0755 /home/ownvitals-deploy
sudo useradd --system --no-create-home --home-dir /home/ownvitals-deploy --shell /bin/sh ownvitals-deploy
sudo usermod -p '*' ownvitals-deploy   # no password can match; not "locked", so key login works
id ownvitals-deploy                    # only its own group: not docker, sudo, adm or systemd-journal
```

The login shell must be a real shell (sshd runs the forced command through it); `/bin/sh` reads
no start-up files for a command.

**7. The key**, root-owned where the user can't change it,
`/etc/ssh/authorized_keys/ownvitals-deploy` (`root:root 0644`, directory `0755`):

```bash
sudo install -d -o root -g root -m 0755 /etc/ssh/authorized_keys
sudo install -o root -g root -m 0644 /dev/null /etc/ssh/authorized_keys/ownvitals-deploy
sudoedit /etc/ssh/authorized_keys/ownvitals-deploy   # the line below, with the agent's public key
```

One line:

```
restrict,command="/usr/local/bin/ownvitals-deploy",no-pty,no-port-forwarding,no-agent-forwarding,no-X11-forwarding ssh-ed25519 AAAA... ownvitals-deploy
```

`restrict` also turns off `~/.ssh/rc` and any forwarding added to OpenSSH later; the explicit
options stay for older readers. Optionally add `from="<the agent machine's address>"`.

**8. sshd**, `/etc/ssh/sshd_config.d/ownvitals-deploy.conf`; it repeats the forced command, so
neither file alone grants more:

```
Match User ownvitals-deploy
    AuthorizedKeysFile /etc/ssh/authorized_keys/%u
    AuthenticationMethods publickey
    PasswordAuthentication no
    KbdInteractiveAuthentication no
    ForceCommand /usr/local/bin/ownvitals-deploy
    PermitTTY no
    DisableForwarding yes
    PermitTunnel no
    PermitUserRC no
```

Then `sudo sshd -t && sudo systemctl reload ssh`.

**9. sudo.** The script hops to root through exactly one rule: the deploy user may run exactly
this file, without arguments, keeping `SSH_ORIGINAL_COMMAND`. The root run cleans its
environment and parses the command again. `/etc/sudoers.d/ownvitals-deploy` (`root:root 0440`):

```
Defaults:ownvitals-deploy env_keep += "SSH_ORIGINAL_COMMAND"
ownvitals-deploy ALL=(root) NOPASSWD: /usr/local/bin/ownvitals-deploy ""
```

Check it with `sudo visudo -cf /etc/sudoers.d/ownvitals-deploy`. Ubuntu 26.04's `sudo` is
sudo-rs, while the smoke test of #100 ran classic sudo 1.9.15 on Ubuntu 24.04. The script fails
closed either way: it refuses any argument itself and parses `SSH_ORIGINAL_COMMAND` again as root,
so a rule sudo read more loosely grants nothing more, and one it read more strictly only refuses.
Run `visudo -cf` and step 10 with the host's own sudo.

**sudo-rs 0.2.2** rejects the per-user `Defaults:ownvitals-deploy` line, and without it drops
`SSH_ORIGINAL_COMMAND`: every command is then refused (`FAIL  command: ...`) and nothing runs. The
fallback is a global keep in the same file, in place of the per-user line:

```
Defaults env_keep += "SSH_ORIGINAL_COMMAND"
ownvitals-deploy ALL=(root) NOPASSWD: /usr/local/bin/ownvitals-deploy ""
```

*Trade-off:* the variable then survives **every** `sudo` on the host, for every user, not only
this rule: a command an administrator runs with `sudo` from an SSH session sees the SSH command
line that session was started with. It is not a secret, and only a program that reads it is
affected (this script, which parses it again as root); it grants nothing by itself. Use it only
if the host's sudo refuses the per-user line, and check the file again with
`sudo visudo -cf /etc/sudoers.d/ownvitals-deploy`.

**10. Try it** from the agent machine: `ssh ownvitals-deploy@192.168.1.222 rollback` answers
`FAIL  rollback: no previous deployment is recorded`; `ssh ... id` and `ssh -t ...` are refused.
The first real deploy waits for your go-ahead; it is also what confirms the two assumptions
about GitHub in checks 6 and 9 (`current_user_can_bypass` for the token, `--source-digest` for
an annotated tag).

### What the deploy user can and cannot do

| | |
|---|---|
| Shell | none: every session runs the forced command (both `authorized_keys` and `ForceCommand`), with no PTY and no forwarding |
| Root | only `/usr/local/bin/ownvitals-deploy` with no arguments, which accepts only the two commands and checks them again as root |
| Database | none: no Docker group, no database password, no published Postgres port; the script runs `migrate` only in a deploy's step 12 and `db-functions` only through `up -d`, both from the attested image |
| Keys and secrets | none: `/etc/ownvitals-deploy` and `/var/lib/ownvitals-deploy` are root `0700`, the token file `0600`, `/opt/ownvitals/secrets` root `0700`; the token reaches gh through the environment, never an argument or the output |
| Logs | none: not in `adm`, `systemd-journal` or `docker`; the script discards gh's and docker's output and prints only its own check lines |

What a key holder **can** do: deploy a release that passes every check, is newer than the highest
release ever deployed and descends from it (or deploy that highest release again with its
digest), and roll back one step. It cannot install a release below the highest (check 3) or on an
older commit (check 8), and it cannot roll back twice in a row.

## Residual risks

- The script, gh and docker run as root (Docker access is root-equivalent anyway). gh parses
  GitHub's and the registry's answers as root.
- The token is a credential on the server; it is read-only and limited to this repository.
- The token sees only whether its own user may bypass the tag ruleset (check 6): an app or
  deploy keys on the bypass list are not visible.
- The `release tags` ruleset has no `creation` rule (T62): checks 7, 8 and 9 carry that.
- The highest release is a high-water mark: a release found bad after it was deployed is left by
  `rollback` once, and the way forward is a newer release, never an older one.

## Tests

`backend/tests/test_deploy_*.py`, in the backend job (a change under `deploy/` runs it):

| File | Covers |
|---|---|
| `test_deploy_harness.py` | `Host`: a copy of the script whose seven installation lines point into a temporary directory, fakes first on its PATH and, on Linux, nothing else but a few core utilities; checks of the script as a file (`#!/bin/bash -p`, LF, mode 100755, `bash -n`, shellcheck) |
| `test_deploy_fakes.py` | the fake gh, docker and sudo (synthetic GitHub with a commit graph, GHCR and host) and their fidelity: each jq program against the real `jq -r`, the attestation flags against gh's semantics, `compare` against the graph. Like compose, the fake's `up -d` starts every service whose scale is not 0, and web waits for `migrate` |
| `test_deploy_script.py` | scenarios: the full deploy (`db-functions` pinned and started with the app services), every refusal of checks 6, 7 and 9–11 (a ruleset the owner may bypass, an unexpected ruleset id), rollback through every check (its `db-functions` on the previous digest), running no migration (with a `migrate` that would fail) and refused when the previous pair no longer verifies, the state file and its modes, a failed migration and a failed `up -d`, two concurrent deploys, the sudo hop, the environment and tool output (a marker on every tool's stderr, `config` and failing `gh api` calls included) |
| `test_deploy_versions.py` | checks 3 and 8: an older release, a release below the highest whatever the current and previous pairs, the highest version with another digest, the same pair again, a newer release, numeric not textual order, the first deploy; v1.0.0, v1.2.0, `rollback`, v1.1.0 refused and v1.2.0 (its digest) and a descending v1.3.0 accepted; a higher tag on an older commit refused; rollback not held to either rule |
| `test_deploy_failures.py` | a hung pull stopped by its timeout, a state file that cannot be written (exit 1), an override that cannot be put in place (exit 2) |
| `test_deploy_refusals.py` | every malformed command, tag, digest and state file, and the variants of each check |
| `test_deploy_db_functions.py` | check 11's `db-functions` rule: a bind mount, a named or tmpfs volume, `volumes_from`, a config (with or without a target), `develop`, another secret, `postgres_password` at an SQL path or with a mode, no secret; near misses (the same keys on another service, the words in a label) |
| `test_deploy_mutations.py` | each check removed or loosened in a copy of the script, and the scenario that catches it |

Run them with `uv run pytest -q tests/test_deploy_*.py` from `backend/`. Under Windows they need
Git Bash; the fakes run with the test's own Python.
