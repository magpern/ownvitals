# Releases and dependency updates

`.github/workflows/release.yml` turns a protected `v*` tag into an image in GHCR,
`ghcr.io/magpern/ownvitals`, with a build-provenance attestation. The deploy (PLAN-operations
§9.2) accepts only an image pulled by digest whose attestation that workflow signed for that tag.
Deploying is not part of a release and needs the owner's go-ahead. The procedure is AGENTS.md
"Releasing"; this page says what the workflow checks and how to check its output.

## Cutting a release

1. `main` is green and holds what the release should contain. Pick the next SemVer version
   `vX.Y.Z`; a pre-release is `vX.Y.Z-rc.1`.
2. Tag the tip of `main` and push the tag:

   ```bash
   git fetch origin main
   git tag -a vX.Y.Z -m "vX.Y.Z" origin/main
   git push origin vX.Y.Z
   ```

3. Follow the run: `gh run list -R magpern/ownvitals --workflow release.yml`, then
   `gh run watch <id> -R magpern/ownvitals`. The GitHub Release appears only after the image is
   pushed and its attestation verifies; its notes start with the image digest and the command
   that checks it.

The `release tags` ruleset stops a `v*` tag from being moved or deleted. A release that fails or
is wrong is fixed forward with a new version. Re-running the failed jobs of a run is fine: a
re-run reuses the outputs of the jobs that passed, so `attest` and `release` keep the image
job's digest. Never push a tag to try the workflow out; it has no manual trigger.

## What the workflow does

Workflow-level `permissions: {}`; each job asks for what it needs. The only credential is the
job's `GITHUB_TOKEN`.

| Job | Permissions | Does |
|---|---|---|
| `verify` | `contents: read` | Runs only in `magpern/ownvitals` (a fork's tags start nothing). Refuses unless the ref is `refs/tags/<tag>`, the tag is `vMAJOR.MINOR.PATCH` with an optional SemVer pre-release (no leading zeros, no empty identifiers, no `+build`, ASCII only), and the tagged commit is an ancestor of `main` |
| `image` | `contents: read`, `packages: write` | Builds the `Dockerfile` for `linux/amd64` and `linux/arm64` (QEMU) and pushes tags `X.Y.Z`, `X.Y` (not for a pre-release) and `sha-<7 hex>`, never `latest`. BuildKit's provenance (`mode=max`) and SBOM go into the image index. No build cache is read or written |
| `attest` | `id-token: write`, `attestations: write`, `packages: write` | `actions/attest-build-provenance` signs a SLSA provenance for the index digest, stores it in the repository's attestations and pushes it next to the image. Then `gh attestation verify` checks it, from the API and from the registry, with the deploy's policy (below) plus the tagged commit. Kept apart from `image` so the OIDC token is never available while the build runs |
| `release` | `contents: write` | `gh release create <tag> --verify-tag --generate-notes`, marked pre-release when the tag has a hyphen; the notes start with the digest and the verify command |

## Checking an image (what the deploy checks)

The digest to deploy is the multi-platform index digest from the release notes (also in the
`attest` job's summary). Pull it by digest, never by tag. A platform manifest's digest has no
attestation of its own.

```bash
tag=vX.Y.Z digest=sha256:<64 hex>
gh attestation verify "oci://ghcr.io/magpern/ownvitals@$digest" \
  --repo magpern/ownvitals \
  --cert-identity "https://github.com/magpern/ownvitals/.github/workflows/release.yml@refs/tags/$tag" \
  --source-ref "refs/tags/$tag" \
  --deny-self-hosted-runners
docker pull "ghcr.io/magpern/ownvitals@$digest"
```

`--cert-identity` pins the signer to `release.yml` run for exactly that tag; `--bundle-from-oci`
reads the attestation from the registry instead of the GitHub API.

**The `verify` job is a guard against mistakes, not a security boundary.** A tag pushed on a
commit outside `main` runs *that commit's* `release.yml`, which may not contain the check, and its
attestation still names `release.yml` at a `v*` tag. So the deploy must also check that the
attested commit is on `main`:

```bash
sha=$(gh api "repos/magpern/ownvitals/commits/$tag" --jq .sha)        # the protected tag's commit
gh api "repos/magpern/ownvitals/compare/$sha...main" --jq .status      # must be identical or ahead
# and add --source-digest "$sha" to the gh attestation verify above
```

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

## Changing `release.yml`

The `checks` CI job runs actionlint (with shellcheck) on every workflow. To build what the
`image` job builds, without pushing:

```bash
docker buildx create --name release-test --driver docker-container \
  --driver-opt image=moby/buildkit:<tag>@sha256:<digest>      # the pin in release.yml
docker buildx build --builder release-test --platform linux/amd64,linux/arm64 \
  --attest type=provenance,mode=max --attest type=sbom \
  --output type=oci,dest=release-test.oci.tar .
docker buildx rm release-test
```

## Dependabot

`.github/dependabot.yml`: one grouped PR per ecosystem per week for minor and patch updates and
one for majors. Dependabot PRs go through the review gate like any other.

| Ecosystem | Directory | Prefix | Not offered |
|---|---|---|---|
| `uv` | `/backend` | `build(deps)`, dev group `build(deps-dev)` | — |
| `npm` | `/frontend` (the e2e issue adds `/e2e`) | `build(deps)`, dev `build(deps-dev)` | `@types/node` majors (it follows Node 24) |
| `github-actions` | `/` | `ci(deps)` | — |
| `docker` | `/` (`Dockerfile`) | `build(deps)` | Python minors and majors (3.12), Node majors (24), `astral-sh/uv` |
| `docker-compose` | `/` (`compose.yaml`) | `build(deps)` | Postgres majors (17) |

Runtime versions (Python 3.12, Postgres 17, Node 24) change only through their own issue. The uv
image in the `Dockerfile` must equal the uv version CI installs (`setup-uv` `version:` in
`ci.yml`), so both move together by hand.

Not tracked by Dependabot, bumped by hand: the shellcheck and actionlint images and the Postgres
service image in `ci.yml`, the images in `release.yml` above, the uv and Node versions given to
`setup-uv` and `setup-node`, and the gitleaks release in `ci.yml`.
