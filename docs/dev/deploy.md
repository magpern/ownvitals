# The deploy mechanism

`deploy/ownvitals-deploy` is the forced command of the home server's deploy key
(PLAN-operations §9.2). It is the release trust boundary (threat model T62): neither the `v*` tag
nor `release.yml`'s own checks are, because anyone who can push can create a `v*` tag and that
commit's `release.yml` runs. An image runs on the server only after the script has checked the
tag, `main` and the build-provenance attestation, and it is pulled by digest only.

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
| 1 | refused: a check failed or the command is not allowed; nothing was recorded and the running release is unchanged |
| 2 | `up -d` failed after the new pair was recorded (its migrations ran); `rollback` returns to the release before |
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
3. **Never back.** With a deployment recorded, the tag must be a **higher version** than the
   recorded current tag, compared as numbers field by field (`v1.10.0` is newer than `v1.9.0`).
   The deployed tag itself passes only with its recorded digest (an idempotent redeploy); with
   another digest it is refused, since a release is never replaced. With nothing recorded (the
   first deploy), any release that passes the other checks may be the first. *Why:* a stolen key
   must not install an older, attested release with a known vulnerability. Going back is
   `rollback` only, one step and through every check (below).
4. **gh.** The read-only token file exists, and `gh --version` is at least **2.102.0**.
   `--source-ref` and `--source-digest` exist since gh 2.68.0; 2.93.0 stops sending the token to
   TUF mirrors, and 2.102.0 makes `--source-ref` match case-sensitively. gh runs with only the
   token, its own config directory (`/var/lib/ownvitals-deploy/gh`) and no prompts.
5. **The tag's commit**, through the full ref name, so a branch of the same name is never read:
   `gh api repos/magpern/ownvitals/commits/refs/tags/<tag> --jq .sha` (40 hex digits).
6. **The tag is protected** (PLAN-operations §9.2 (1)). Of the repository's rulesets
   (`rulesets?includes_parents=true&targets=tag`), one must have, read through
   `rulesets/<id>`: target `tag`; enforcement `active` (not `evaluate` or `disabled`); an include
   pattern `refs/tags/v*` (or `~ALL`); no exclude pattern that may match `refs/tags/<tag>` (bash's
   `*` also crosses `/`, so this over-matches, and a pattern with escapes counts as matching:
   both refuse); and the rules `update` and `deletion`. Today that is the `release tags` ruleset
   (`refs/tags/v*`; `update`, `deletion`, `non_fast_forward`; no bypass actors).
   *Why, and what it does not do:* it keeps a recorded `(tag, digest)` pair from changing under
   the record: the tag cannot move to another commit or be deleted and re-created. It does not
   stop a tag being **created** on any commit (there is no `creation` rule, an Architect decision
   while the owner's token is the only pusher), and it cannot see bypass actors, which a
   read-only token is not shown. Checks 7 and 8 are what stop a tag outside `main` (T62).
7. **`main` contains the commit:**
   `gh api "repos/magpern/ownvitals/compare/<commit>...refs/heads/main?per_page=1" --jq .status`
   is `identical` or `ahead`. Always `refs/heads/main`: a bare `main` could resolve to a tag of
   that name, which the `v*` ruleset does not cover.
8. **The attestation**, with the policy `release.yml` verifies its own attestation with:

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
   attestation to the commit check 7 found on `main`.
9. **Pull by digest:** `docker pull --quiet ghcr.io/magpern/ownvitals@<digest>`, never a tag.
10. **Compose.** `/opt/ownvitals/compose.yaml` has the services `migrate`, `web` and `worker`. The
    script writes an override that gives each of them `image: ghcr.io/magpern/ownvitals@<digest>`
    and `pull_policy: never`, and with it `docker compose config --images` may name the image only
    by that digest: a service that names it by a tag (or another digest) is refused, since running
    it would be a pull by tag.
11. **Migrations:** `docker compose run --rm -T --pull never migrate` with the new override.
    If it fails, nothing is recorded and the running containers are untouched.
12. **State:** the new pair is recorded as current and the pair it replaces as previous, and the
    override becomes `/var/lib/ownvitals-deploy/compose.image.yaml`. This happens after the
    migrations and before `up -d`, so a failed start still leaves `rollback` pointing at the
    release that ran before. Redeploying the current pair keeps the previous one.
13. **Start:** `docker compose up -d --pull never --no-build`.

Every compose call is
`docker compose --project-name ownvitals --project-directory /opt/ownvitals --file /opt/ownvitals/compose.yaml [--file <override>]`.

`rollback` takes the recorded previous pair and runs checks 2 and 4–10 on it, the same as a
deploy (it skips check 3: going back one step is what it is for), then records it as current
with no previous pair and runs `up -d`. **It runs no migrations**: the
previous release runs on the newer schema (expand/contract, PLAN-operations §9.2), and its
`alembic upgrade head` would not know the newer revision. It goes back one step only; a second
`rollback` is refused until the next deploy. If the previous pair no longer passes a check (its
attestation, its tag ruleset, its commit on `main`), the rollback is refused and nothing changes.

### Checking a release by hand

The same checks, for a release you want to look at before asking for a deploy:

```bash
tag=vX.Y.Z digest=sha256:<64 hex>
[[ $tag =~ ^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$ && $digest =~ ^sha256:[0-9a-f]{64}$ ]]
sha=$(gh api "repos/magpern/ownvitals/commits/refs/tags/$tag" --jq .sha)
gh api "repos/magpern/ownvitals/rulesets?includes_parents=true&targets=tag" --jq '.[].id' |
  while read -r id; do
    gh api "repos/magpern/ownvitals/rulesets/$id" \
      --jq '{target, enforcement, ref_name: .conditions.ref_name, rules: [.rules[].type]}'
  done   # one: tag, active, include refs/tags/v*, no matching exclude, update and deletion
gh api "repos/magpern/ownvitals/compare/$sha...refs/heads/main?per_page=1" --jq .status   # identical or ahead
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
| `state` | four lines, in this order: `current_tag=`, `current_digest=`, `previous_tag=`, `previous_digest=` (previous empty when there is none). Anything else makes every command refuse until it is fixed by hand |
| `compose.image.yaml` | the override for the recorded current digest; use it for manual compose commands: `docker compose -f compose.yaml -f /var/lib/ownvitals-deploy/compose.image.yaml ps` |
| `lock` | the `flock` file |
| `gh/` | gh's own config directory (nothing in it is needed) |

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

**5. The compose project** in `/opt/ownvitals`: `compose.yaml` (the production stack, with the
services `migrate`, `web` and `worker`; their `image:` is replaced by the override, so it can be
`ghcr.io/magpern/ownvitals`), `.env` and `secrets/` (root, `0700`). Images other than ours (the
database's) must already be pulled: the deploy runs compose with `--pull never`.

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
`/etc/ssh/authorized_keys/ownvitals-deploy` (`root:root 0644`, directory `0755`), one line:

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

Check it with `sudo visudo -cf /etc/sudoers.d/ownvitals-deploy`.

**10. Try it** from the agent machine: `ssh ownvitals-deploy@192.168.1.222 rollback` answers
`FAIL  rollback: no previous deployment is recorded`; `ssh ... id` and `ssh -t ...` are refused.
The first real deploy waits for your go-ahead.

### What the deploy user can and cannot do

| | |
|---|---|
| Shell | none: every session runs the forced command (both `authorized_keys` and `ForceCommand`), with no PTY and no forwarding |
| Root | only `/usr/local/bin/ownvitals-deploy` with no arguments, which accepts only the two commands and checks them again as root |
| Database | none: no Docker group, no database password, no published Postgres port; the script runs only `migrate`, which ships in the attested image |
| Keys and secrets | none: `/etc/ownvitals-deploy` and `/var/lib/ownvitals-deploy` are root `0700`, the token file `0600`, `/opt/ownvitals/secrets` root `0700`; the token reaches gh through the environment, never an argument or the output |
| Logs | none: not in `adm`, `systemd-journal` or `docker`; the script discards gh's and docker's output and prints only its own check lines |

What a key holder **can** do: deploy a release that passes every check and is newer than the
deployed one (or redeploy the deployed one with its digest), and roll back one step. It cannot
install an older release (check 3), and it cannot roll back twice in a row.

## Residual risks

- The script, gh and docker run as root (Docker access is root-equivalent anyway). gh parses
  GitHub's and the registry's answers as root.
- The token is a credential on the server; it is read-only and limited to this repository.
- Bypass actors of the tag ruleset are not visible to the token (check 6).
- The `release tags` ruleset has no `creation` rule (T62): checks 7 and 8 carry that.

## Tests

`backend/tests/test_deploy_*.py`, in the backend job (a change under `deploy/` runs it):

| File | Covers |
|---|---|
| `test_deploy_harness.py` | `Host`: a copy of the script whose seven installation lines point into a temporary directory, fakes first on its PATH and, on Linux, nothing else but a few core utilities; checks of the script as a file (`#!/bin/bash -p`, LF, mode 100755, `bash -n`, shellcheck) |
| `test_deploy_fakes.py` | the fake gh, docker and sudo (synthetic GitHub, GHCR and host) and their fidelity: each jq program against the real `jq -r`, the attestation flags against gh's semantics |
| `test_deploy_script.py` | scenarios: the full deploy, the version rule of check 3 (an older release, the deployed version with another digest, the same pair again, a newer release, numeric not textual order, the first deploy), every refusal of checks 6–10, rollback through every check (and refused when the previous pair no longer verifies), the state file, a failed migration and a failed `up -d`, two concurrent deploys, the sudo hop, the environment and tool output |
| `test_deploy_refusals.py` | every malformed command, tag and digest, and the variants of each check |
| `test_deploy_mutations.py` | each check removed or loosened in a copy of the script, and the scenario that catches it |

Run them with `uv run pytest -q tests/test_deploy_*.py` from `backend/`. Under Windows they need
Git Bash; the fakes run with the test's own Python.
