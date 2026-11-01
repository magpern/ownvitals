# Releases and dependency updates

`.github/workflows/release.yml` turns a `v*` tag into an image in GHCR,
`ghcr.io/magpern/ownvitals`, with a build-provenance attestation. The deploy (PLAN-operations
§9.2) accepts only an image pulled by digest whose attestation that workflow signed for that tag.
Deploying is not part of a release and needs the owner's go-ahead. The procedure is AGENTS.md
"Releasing"; this page says what the workflow checks and how to check its output.

## Cutting a release

1. `main` is green and holds what the release should contain. Pick the next SemVer version
   `vX.Y.Z`; a pre-release is `vX.Y.Z-rc.1`.
2. `version` in `backend/pyproject.toml` on `main` is that version without the `v`, spelled
   exactly as the tag (`X.Y.Z-rc.1` for a pre-release, not PEP 440's `X.Y.Zrc1`): the `verify`
   job refuses any other tag. If it isn't, first merge a PR that sets it and runs `uv lock` (the
   lockfile records the project's version). Only pre-release spellings that Python packaging
   also accepts as a version (`-rc.1`, `-a.1`, `-beta.2`) can be released: `verify` accepts any
   SemVer pre-release, but `pyproject.toml` and `uv lock` refuse a version such as `1.2.3-x-y.1`.
3. Tag the tip of `main` and push the tag:

   ```bash
   git fetch origin main
   git tag -a vX.Y.Z -m "vX.Y.Z" origin/main
   git push origin vX.Y.Z
   ```

4. Follow the run: `gh run list -R magpern/ownvitals --workflow release.yml`, then
   `gh run watch <id> -R magpern/ownvitals`. The GitHub Release appears only after the image is
   pushed and its attestation verifies; its notes start with the image digest and the command
   that checks it.

The `release tags` ruleset stops a `v*` tag from being moved or deleted. A release that fails or
is wrong is fixed forward with a new version. Re-running the **failed** jobs of a run is fine: a
re-run reuses the outputs of the jobs that passed, so `attest` and `release` keep the image
job's digest. **Never use "Re-run all jobs"** on a run whose `image` job passed: the build is
not reproducible, so it moves the `X.Y.Z` tag to a new digest and attests that one too, and the
version then has two attested digests. Never push a tag to try the workflow out; it has no
manual trigger.

## What the workflow does

Workflow-level `permissions: {}`; each job asks for what it needs. The only credential is the
job's `GITHUB_TOKEN`.

| Job | Permissions | Does |
|---|---|---|
| `verify` | `contents: read` | Runs only in `magpern/ownvitals` (a fork's tags start nothing). Refuses unless the ref is `refs/tags/<tag>`, the tag is `vMAJOR.MINOR.PATCH` with an optional SemVer pre-release (no leading zeros, no empty identifiers, no `+build`, ASCII only), the tagged commit is an ancestor of `main`, and `project.version` in `backend/pyproject.toml` at that commit is the tag without its `v` |
| `image` | `contents: read`, `packages: write` | Builds the `Dockerfile` for `linux/amd64` and `linux/arm64` (QEMU) and pushes tags `X.Y.Z`, `X.Y` (not for a pre-release) and `sha-<7 hex>`, never `latest`. BuildKit's provenance (`mode=max`) and SBOM go into the image index. No build cache is read or written |
| `attest` | `id-token: write`, `attestations: write`, `packages: write` | `actions/attest-build-provenance` signs a SLSA provenance for the index digest, stores it in the repository's attestations and pushes it next to the image. Then `gh attestation verify` checks it, from the API and from the registry, with the deploy's policy (cert identity, source ref, source digest, no self-hosted runners). Kept apart from `image` so the OIDC token is never available while the build runs |
| `release` | `contents: write` | `gh release create <tag> --verify-tag --generate-notes`, marked pre-release when the tag has a hyphen; the notes start with the digest and the verify command |

## Checking an image (what the deploy checks)

The digest to deploy is the multi-platform index digest from the release notes (also in the
`attest` job's summary). Pull it by digest, never by tag. A platform manifest's digest has no
attestation of its own.

**Neither the tag nor this workflow is the security boundary; the deploy is** (threat model
T62). The `release tags` ruleset stops a `v*` tag being moved or deleted, not created, and a tag
pushed on a commit outside `main` runs *that commit's* `release.yml`, which may lack the
`verify` checks, while its attestation still names `release.yml` at a `v*` tag. The deploy's
checks, in order, and the commands to run them by hand are in [deploy.md](deploy.md) (#84); this
page carries no copy. The verify command in the release notes is a convenience only.

The workflow never changes the package's visibility: whether it is public is the owner's decision
(ADR 0001), and with it how the home server pulls (anonymously or with a read-only token).

## Pinned actions and images

- Every action is pinned by full commit SHA with its version in a comment; Dependabot's
  `github-actions` entry bumps them.
- Images given as action inputs are pinned by tag and index digest, and Dependabot does not see
  them: `tonistiigi/binfmt` (the QEMU emulator that runs the arm64 build), `moby/buildkit` (the
  builder) and `docker/buildkit-syft-scanner` (the SBOM generator). Bump the tag and digest by
  hand; `docker buildx imagetools inspect <image>:<tag>` prints the digest.
- `gh` is the runner image's.
- The `Dockerfile`'s images are pinned as `name:tag@sha256:<index digest>`. Dependabot's
  `docker` entry bumps the `python` image's tag and digest. The `ghcr.io/astral-sh/uv` image
  moves by hand with `setup-uv` (Dependabot ignores it; `scripts/ci-uv-version.sh` checks the
  tags agree), and so does the `# syntax=docker/dockerfile:...` line, which Dependabot does not
  read.

## Changing `release.yml`

The `checks` CI job runs actionlint (with shellcheck) on every workflow, and
`scripts/test-release.sh`, which tests the workflow's guards without the network: the trigger,
each job's permissions and `needs`, the action and image pins, no `${{ }}` inside a `run:`
script, and the `run:` scripts themselves, run against a scratch git repository with a fake
`gh` (accepted and refused tags, refs, commits, versions and digests; a `tomllib.py` planted at
the repository root, which `python3 -I` never imports; the exact arguments of
`gh attestation verify` and `gh release create`). A change to a guard changes its test in the
same PR. Locally the script needs mikefarah yq v4 (it reads only stdin, so a `docker run -i`
wrapper works), jq, git and python3 3.11 or later.

To build what the `image` job builds, without pushing:

```bash
docker buildx create --name release-test --driver docker-container \
  --driver-opt image=moby/buildkit:<tag>@sha256:<digest>      # the pin in release.yml
docker buildx build --builder release-test --platform linux/amd64,linux/arm64 \
  --attest type=provenance,mode=max \
  --attest type=sbom,generator=docker/buildkit-syft-scanner:<tag>@sha256:<digest> \
  --output type=oci,dest=release-test.oci.tar .                # both pins as in release.yml
docker buildx rm release-test
```

The arm64 half runs under the QEMU emulator the machine already has (Docker Desktop's), not the
pinned `tonistiigi/binfmt` image the `image` job installs.

## Dependabot

`.github/dependabot.yml`: one grouped PR per ecosystem per week for minor and patch updates and
one for majors. Dependabot PRs go through the review gate like any other.

| Ecosystem | Directory | Prefix | Groups | Cooldown | Not offered |
|---|---|---|---|---|---|
| `uv` | `/backend` | `build(deps)`, dev group `build(deps-dev)` | `uv`, `major` | 5 days | — |
| `npm` | `/frontend` (the e2e issue adds `/e2e`) | `build(deps)`, dev `build(deps-dev)` | `npm`, `major` | 5 days | `@types/node` majors (it follows Node 24) |
| `github-actions` | `/` | `ci(deps)` | `actions`, `major` | — | — |
| `docker` | `/` (`Dockerfile`) | `build(deps)` | `docker`, `major` | — | Python minors and majors (3.12), Node majors (24), `astral-sh/uv` |
| `docker-compose` | `/` (`compose.yaml`) | `build(deps)` | `compose`, `major` | — | Postgres majors (17) |

**Cooldown:** a uv or npm version is offered only once it is 5 days old, so a hijacked release
is usually yanked before it reaches a PR. Security updates ignore the cooldown.

**Titles:** the PR title becomes the squash subject, and `pr-title` checks it, with ` (#n)`
appended, against commitlint's 100 characters; that is why the group names are short. A PR that
updates several dependencies fits (about 80 characters at most). A group PR for a single
dependency names it with both versions, the directory and the group, for example
`build(deps-dev): bump @testing-library/user-event from 14.6.8 to 14.7.0 in /frontend in the
major group across 1 directory`, and many npm ones still don't fit. The Architect retitles such
a PR to a shorter Conventional Commits subject before merging.

Runtime versions (Python 3.12, Postgres 17, Node 24) change only through their own issue. The uv
image in the `Dockerfile` must equal the uv version CI installs (`setup-uv` `version:` in
`ci.yml`), so both move together by hand.

Not tracked by Dependabot, bumped by hand: the shellcheck and actionlint images and the Postgres
service image in `ci.yml` (with `compose.yaml`'s `db` image, which must carry the same digest;
[jobs.md](jobs.md)), the images in `release.yml` above, the uv and Node versions given to
`setup-uv` and `setup-node`, the `Dockerfile`'s uv image and `# syntax=` line, and the gitleaks
and Trivy releases in `ci.yml` (each with its SHA-256 from the release's checksums file;
`scripts/test-ci-downloads.sh` tests that both steps check it).
