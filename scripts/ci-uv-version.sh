#!/usr/bin/env bash
# CI (checks job): the uv version is the same everywhere. The Dockerfile's uv tag is the reference
# (Dependabot's uv image bump is ignored, so both move together by hand):
#
#   - the `with.version` of every setup-uv step in any workflow or composite action must equal it
#     (a missing or computed (`${{ }}`) version fails, and so does finding no setup-uv step);
#   - every `uv@X.Y.Z` and `with uv X.Y.Z` in docs/dev/backend.md (the commands CI runs and
#     developers copy) must equal it, and the file must name the version at least once.
#
# Needs mikefarah yq v4. Run from the repository root. Ported from classbeat (d8060510), where it
# was moved out of ci.yml to keep that under 500 lines; the docs check is new.
set -euo pipefail
yqv="$(yq --version)"
echo "$yqv"
grep -Eq 'mikefarah/yq.* v4\.' <<< "$yqv" || { echo "::error::need mikefarah yq v4"; exit 1; }
# The tag, bare or pinned by digest (`uv:X.Y.Z@sha256:<64 hex>`); any other form has no tag.
want="$(sed -n 's|^FROM ghcr.io/astral-sh/uv:\([0-9][0-9.]*\)\(@sha256:[0-9a-f]\{64\}\)\{0,1\} .*|\1|p' Dockerfile | tr -d '\r')"
[ -n "$want" ] || { echo "::error::no uv tag found in the Dockerfile"; exit 1; }
[ "$(wc -l <<< "$want")" -eq 1 ] || { echo "::error::more than one uv tag in the Dockerfile"; exit 1; }
echo "Dockerfile uv tag: $want"
query="$(cat <<'Q'
[(.jobs // {} | to_entries | .[] | .key as $j | .value.steps[]? | [$j, .]),
 (.runs.steps[]? | ["(composite action)", .])]
| .[] | select(((.[1].uses // "") | downcase) | test("^astral-sh/setup-uv@"))
| .[0] + "\t" + (.[1].name // .[1].uses) + "\t" + ((.[1].with.version | select(tag == "!!str")) // "NONE") + "\t" + ((.[1].with // {} | has("version-file")) | tostring)
Q
)"
shopt -s nullglob globstar
files=(.github/workflows/*.yml .github/workflows/*.yaml .github/actions/**/action.yml .github/actions/**/action.yaml)
found=0
bad=0
for f in "${files[@]}"; do
  rows="$(yq -r "$query" "$f")"
  [ -n "$rows" ] || continue
  while IFS=$'\t' read -r job step v vfile; do
    vfile="${vfile%$'\r'}"
    found=$((found + 1))
    if [ "$vfile" = true ]; then
      echo "::error file=$f::job '$job', step '$step': setup-uv must not set version-file; use a literal version"
      bad=1
    elif [ "$v" != "$want" ]; then
      echo "::error file=$f::job '$job', step '$step': setup-uv version '$v' is not the Dockerfile uv tag $want (a literal string is required)"
      bad=1
    fi
  done <<< "$rows"
done
[ "$found" -gt 0 ] || { echo "::error::no setup-uv step found"; exit 1; }

doc=docs/dev/backend.md
[ -f "$doc" ] || { echo "::error::$doc is missing"; exit 1; }
docfound=0
while IFS= read -r ref; do
  docfound=$((docfound + 1))
  v="${ref##*[@ ]}"
  if [ "$v" != "$want" ]; then
    echo "::error file=$doc::'$ref' is not the Dockerfile uv tag $want"
    bad=1
  fi
done < <(grep -Eo 'uv@[0-9]+(\.[0-9]+)*|with uv [0-9]+(\.[0-9]+)*' "$doc" | tr -d '\r')
[ "$docfound" -gt 0 ] || { echo "::error file=$doc::no uv version found (expected uv@$want commands)"; exit 1; }

[ "$bad" -eq 0 ] && echo "uv $want everywhere ($found setup-uv step(s), $docfound reference(s) in $doc)"
exit "$bad"
