"""Fake gh, docker and sudo for the deploy script tests (``deploy/ownvitals-deploy``).

``test_deploy_harness.Host`` puts small wrappers named ``gh``, ``docker`` and ``sudo`` first on the
script's PATH; each runs ``python -I test_deploy_fakes.py <tool> <world.json> <arguments...>``.
Standard library only. The fakes answer from ``world.json`` (GitHub's tags, main, rulesets and
attestations, GHCR's digests, the host's compose file), never from the network, and log every
call to ``calls.jsonl`` beside it. Everything in a world is synthetic: made-up commits, tags and
digests, and no health data at all.

They are strict on purpose: an API path, jq program, flag or compose command the script was not
written to use fails the call, so a test notices when the script asks for something else (a bare
tag name, a bare ``main``, a pull by tag). The tests below check the fakes themselves: each jq
program gives what ``jq -r`` gives (gh's ``--jq`` prints strings raw), and the attestation policy
follows gh's flags.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

REPO = "magpern/ownvitals"
IMAGE = "ghcr.io/magpern/ownvitals"
SLSA = "https://slsa.dev/provenance/v1"
GITHUB_ISSUER = "https://token.actions.githubusercontent.com"
RULESETS_PATH = f"repos/{REPO}/rulesets?includes_parents=true&targets=tag&per_page=100"
RULESET_JQ = (
    '"target\\t\\(.target | tojson)", "enforcement\\t\\(.enforcement | tojson)",\n'
    '(.conditions.ref_name.include[]? | "include\\t\\(tojson)"),\n'
    '(.conditions.ref_name.exclude[]? | "exclude\\t\\(tojson)"), '
    '(.rules[]? | "rule\\t\\(.type | tojson)")'
)
DIGEST = r"sha256:[0-9a-f]{64}"
SUBJECT = re.compile(rf"oci://{re.escape(IMAGE)}@({DIGEST})")
PULL_REF = re.compile(rf"{re.escape(IMAGE)}@({DIGEST})")
RULESET_PATH = re.compile(rf"repos/{REPO}/rulesets/(\d+)")
TAG_PATH = re.compile(rf"repos/{REPO}/commits/refs/tags/(.+)")
COMPARE_PATH = re.compile(
    rf"repos/{REPO}/compare/([0-9a-f]{{40}})\.\.\.refs/heads/main\?per_page=1"
)
VALUE_FLAGS = {
    "--repo", "--owner", "--cert-identity", "--cert-identity-regex", "--cert-oidc-issuer",
    "--source-ref", "--source-digest", "--signer-digest", "--signer-repo", "--signer-workflow",
    "--predicate-type", "--bundle",
}  # fmt: skip
BOOLEAN_FLAGS = {"--deny-self-hosted-runners", "--bundle-from-oci"}


def _tojson(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _items(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def ruleset_lines(ruleset: dict[str, Any]) -> list[str]:
    """``RULESET_JQ``: one ``field<TAB>JSON`` line per fact, in jq's order."""
    ref_name = (ruleset.get("conditions") or {}).get("ref_name") or {}
    return [
        f"target\t{_tojson(ruleset.get('target'))}",
        f"enforcement\t{_tojson(ruleset.get('enforcement'))}",
        *(f"include\t{_tojson(x)}" for x in _items(ref_name.get("include"))),
        *(f"exclude\t{_tojson(x)}" for x in _items(ref_name.get("exclude"))),
        *(f"rule\t{_tojson(rule.get('type'))}" for rule in _items(ruleset.get("rules"))),
    ]


def _raw(value: Any) -> str:
    return value if isinstance(value, str) else _tojson(value)


# The jq programs the script uses, and what each prints for a JSON document.
JQ: dict[str, Any] = {
    ".[].id": lambda doc: [_raw(item["id"]) for item in doc],
    ".sha": lambda doc: [_raw(doc.get("sha"))],
    ".status": lambda doc: [_raw(doc.get("status"))],
    RULESET_JQ: ruleset_lines,
}


def _log(world_path: Path, entry: dict[str, Any]) -> None:
    leaked = [n for n in ("DEPLOY_TEST_SENTINEL", "DOCKER_HOST", "COMPOSE_FILE") if n in os.environ]
    entry["leaked_env"] = leaked
    with (world_path.parent / "calls.jsonl").open("a", encoding="utf-8") as log:
        log.write(json.dumps(entry) + "\n")


def _same_path(a: str | None, b: str) -> bool:
    return a is not None and os.path.normcase(os.path.normpath(a)) == os.path.normcase(
        os.path.normpath(b)
    )


def _noise(world: dict[str, Any]) -> None:
    """Output the script must never pass on (tool logs; the marker is checked by the tests)."""
    if world.get("noise"):
        print(f"tool output {world['noise']}")
        print(f"tool error {world['noise']}", file=sys.stderr)


# --- gh -------------------------------------------------------------------------------------


def _api(world: dict[str, Any], path: str) -> Any:
    """The JSON GitHub would answer, or None for a 404."""
    if path == RULESETS_PATH:  # the target filter is not modelled: the script checks .target
        return [
            {k: r[k] for k in ("id", "name", "target", "enforcement") if k in r}
            for r in world["rulesets"]
        ]
    if m := RULESET_PATH.fullmatch(path):
        return next((r for r in world["rulesets"] if str(r["id"]) == m[1]), None)
    if m := TAG_PATH.fullmatch(path):
        sha = world["tags"].get(m[1])
        return None if sha is None else {"sha": sha, "commit": {"message": "synthetic"}}
    if m := COMPARE_PATH.fullmatch(path):
        base, main = m[1], world["main"]
        if base == main[-1]:
            return {"status": "identical"}
        if base in main:
            return {"status": "ahead"}
        if base in world["behind"]:
            return {"status": "behind"}
        return {"status": "diverged"} if base in world["off_main"] else None
    return None


def _attestation_matches(a: dict[str, Any], flags: dict[str, str]) -> bool:
    identity = f"https://github.com/{a['repo']}/{a['workflow']}@{a['ref']}"
    checks = [
        a["repo"] == flags["--repo"] if "--repo" in flags else True,
        a["repo"].split("/")[0] == flags["--owner"] if "--owner" in flags else True,
        identity.startswith(f"https://github.com/{flags.get('--repo', a['repo'])}/"),
        identity == flags["--cert-identity"] if "--cert-identity" in flags else True,
        re.search(flags["--cert-identity-regex"], identity) is not None
        if "--cert-identity-regex" in flags
        else True,
        a["issuer"] == flags.get("--cert-oidc-issuer", GITHUB_ISSUER),
        a["source_ref"] == flags["--source-ref"] if "--source-ref" in flags else True,
        a["sha"] == flags["--source-digest"] if "--source-digest" in flags else True,
        a["sha"] == flags["--signer-digest"] if "--signer-digest" in flags else True,
        a["runner"] == "github-hosted" if "--deny-self-hosted-runners" in flags else True,
        a["predicate"] == flags.get("--predicate-type", SLSA),
    ]
    return all(checks)


def _verify(world: dict[str, Any], args: list[str]) -> int:
    """``gh attestation verify``: passes when one attestation of the digest meets every flag."""
    if not args or not (m := SUBJECT.fullmatch(args[0])):
        print("gh: unsupported subject", file=sys.stderr)
        return 1
    flags: dict[str, str] = {}
    rest = args[1:]
    while rest:
        flag = rest.pop(0)
        if flag in BOOLEAN_FLAGS:
            flags[flag] = ""
        elif flag in VALUE_FLAGS and rest:
            flags[flag] = rest.pop(0)
        else:
            print(f"gh: unknown flag {flag}", file=sys.stderr)
            return 64
    if "--repo" not in flags and "--owner" not in flags:
        print("gh: one of --repo or --owner is required", file=sys.stderr)
        return 64
    _noise(world)
    if m[1] not in world["registry"]:
        return 1
    found = [a for a in world["attestations"] if a["digest"] == m[1]]
    return 0 if any(_attestation_matches(a, flags) for a in found) else 1


def gh(world: dict[str, Any], args: list[str]) -> int:
    if args == ["--version"]:
        version = world["gh_version"]
        print(f"gh version {version} (2026-10-26)")
        print(f"https://github.com/cli/cli/releases/tag/v{version}")
        return 0
    if os.environ.get("GH_TOKEN") != world["token"] or not _same_path(
        os.environ.get("GH_CONFIG_DIR"), world["gh_dir"]
    ):
        print("gh: authentication required", file=sys.stderr)
        return 4
    if args[:2] == ["attestation", "verify"]:
        return _verify(world, args[2:])
    if len(args) != 6 or args[:3] != ["api", "--method", "GET"] or args[4] != "--jq":
        print("gh: unexpected arguments", file=sys.stderr)
        return 64
    path, program = args[3], args[5]
    if program not in JQ:
        print("gh: the fake does not know this jq program", file=sys.stderr)
        return 65
    if path in world["api_errors"]:
        print("gh: HTTP 500", file=sys.stderr)
        return 1
    doc = _api(world, path)
    if doc is None:
        print("gh: Not Found (HTTP 404)", file=sys.stderr)
        return 1
    if lines := JQ[program](doc):
        print("\n".join(lines))
    return 0


# --- docker ---------------------------------------------------------------------------------


def parse_override(text: str) -> dict[str, dict[str, str]]:
    """The override the script writes: ``services:``, then per service its image and pull policy."""
    services: dict[str, dict[str, str]] = {}
    lines = [ln for ln in text.splitlines() if ln and not ln.startswith("#")]
    if not lines or lines[0] != "services:":
        raise ValueError("no services")
    current = None
    for line in lines[1:]:
        if m := re.fullmatch(r"  ([a-z0-9_-]+):", line):
            current = services.setdefault(m[1], {})
        elif (m := re.fullmatch(r"    (image|pull_policy): (\S+)", line)) and current is not None:
            current[m[1]] = m[2]
        else:
            raise ValueError(f"unexpected line {line!r}")
    return services


def _block(world: dict[str, Any], step: str) -> None:
    """Holds the first caller of ``step`` until the test creates the release file."""
    block = world.get("block") or {}
    if block.get("step") == step:
        try:
            with Path(block["waiting"]).open("x", encoding="utf-8") as waiting:
                waiting.write("waiting\n")
        except FileExistsError:
            return
        deadline = time.monotonic() + 60
        while not Path(block["release"]).exists() and time.monotonic() < deadline:
            time.sleep(0.05)


def _compose(world: dict[str, Any], args: list[str], entry: dict[str, Any]) -> int:
    if len(args) < 4 or args[:3] != ["--project-name", "ownvitals", "--project-directory"]:
        return 64
    if not _same_path(args[3], world["app_dir"]):
        return 64
    files, rest = [], args[4:]
    while rest[:1] == ["--file"] and len(rest) > 1:
        files.append(rest[1])
        rest = rest[2:]
    if not files or not _same_path(files[0], world["compose_file"]) or len(files) > 2:
        return 64
    base: dict[str, str] = world["compose"]
    if len(files) == 1:
        if rest != ["config", "--services"]:
            return 64
        if not world["compose_valid"]:
            return 1
        print("\n".join(base))
        return 0
    try:
        override = parse_override(Path(files[1]).read_text("utf-8"))
    except (OSError, ValueError):
        return 65
    entry["override"] = override
    images = {**base, **{name: s.get("image", base.get(name, "")) for name, s in override.items()}}
    if rest == ["config", "--images"]:
        print("\n".join(images.values()))
        return 0
    if rest == ["run", "--rm", "-T", "--pull", "never", "migrate"]:
        _noise(world)
        _block(world, "migrate")
        return world["migrate_exit"]
    if rest == ["up", "-d", "--pull", "never", "--no-build"]:
        _noise(world)
        _block(world, "up")
        return world["up_exit"]
    return 64


def docker(world: dict[str, Any], args: list[str], entry: dict[str, Any]) -> int:
    if len(args) == 3 and args[:2] == ["pull", "--quiet"]:
        _noise(world)
        m = PULL_REF.fullmatch(args[2])
        entry["by_digest"] = m is not None
        return world["pull_exit"] if m is not None and m[1] in world["registry"] else 1
    if args[:1] == ["compose"]:
        return _compose(world, args[1:], entry)
    return 64


def main(argv: list[str]) -> int:
    for stream in (sys.stdout, sys.stderr):  # LF on Windows too, as gh and docker print
        stream.reconfigure(newline="\n")  # type: ignore[union-attr]
    tool, world_path, args = argv[0], Path(argv[1]), argv[2:]
    world = json.loads(world_path.read_text("utf-8"))
    entry: dict[str, Any] = {"tool": tool, "argv": args}
    if tool == "sudo":
        entry["ssh_original_command"] = os.environ.get("SSH_ORIGINAL_COMMAND")
        status = 0
    elif tool == "gh":
        entry["gh_token_ok"] = os.environ.get("GH_TOKEN") == world["token"]
        status = gh(world, args)
    else:
        status = docker(world, args, entry)
    entry["status"] = status
    _log(world_path, entry)
    return status


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))


# --- tests of the fakes ---------------------------------------------------------------------

RULESET_SAMPLES: list[dict[str, Any]] = [
    {
        "id": 1,
        "target": "tag",
        "enforcement": "active",
        "conditions": {"ref_name": {"include": ["refs/tags/v*"], "exclude": []}},
        "rules": [{"type": "update"}, {"type": "deletion"}, {"type": "non_fast_forward"}],
    },
    {"id": 2, "target": "tag", "enforcement": "evaluate", "conditions": None, "rules": []},
    {"id": 3, "target": "branch", "rules": [{"type": "deletion"}]},
    {
        "id": 4,
        "target": "tag",
        "enforcement": "active",
        "conditions": {
            "ref_name": {"include": ["~ALL", 'x\t"y\nz'], "exclude": ["a\\b", "\N{EM DASH}"]}
        },
        "rules": [{"type": "update"}],
    },
]
JQ_SAMPLES: list[tuple[str, Any]] = [
    (".[].id", [{"id": 26276892}, {"id": 7}]),
    (".[].id", []),
    (".sha", {"sha": "0" * 40, "commit": {}}),
    (".status", {"status": "ahead"}),
    *((RULESET_JQ, sample) for sample in RULESET_SAMPLES),
]


def test_each_jq_program_prints_what_jq_prints() -> None:
    """The fake's reading of every program equals ``jq -r`` on the same document."""
    import pytest

    jq = shutil.which("jq")
    if jq is None:
        if os.environ.get("CI") == "true":
            pytest.fail("jq is missing; the fake's jq programs must be checked in CI")
        pytest.skip("jq is not installed")
    assert set(JQ) == {program for program, _ in JQ_SAMPLES}
    for program, doc in JQ_SAMPLES:
        real = subprocess.run(  # noqa: S603
            [jq, "-r", program],
            input=json.dumps(doc),
            capture_output=True,
            encoding="utf-8",
            check=True,
        )
        assert real.stdout.replace("\r\n", "\n").splitlines() == JQ[program](doc), program


def _attestation(**changes: str) -> dict[str, str]:
    return {
        "digest": "sha256:" + "a" * 64,
        "repo": REPO,
        "workflow": ".github/workflows/release.yml",
        "ref": "refs/tags/v1.2.0",
        "source_ref": "refs/tags/v1.2.0",
        "sha": "c" * 40,
        "runner": "github-hosted",
        "issuer": GITHUB_ISSUER,
        "predicate": SLSA,
    } | changes


def test_the_attestation_policy_follows_gh_flags() -> None:
    policy = {
        "--repo": REPO,
        "--cert-identity": f"https://github.com/{REPO}/.github/workflows/release.yml@refs/tags/v1.2.0",
        "--source-ref": "refs/tags/v1.2.0",
        "--source-digest": "c" * 40,
        "--deny-self-hosted-runners": "",
    }
    assert _attestation_matches(_attestation(), policy)
    for field, value in [
        ("repo", "magpern/other"),
        ("workflow", ".github/workflows/other.yml"),
        ("ref", "refs/tags/v1.1.0"),
        ("source_ref", "refs/tags/V1.2.0"),  # case-sensitive since gh 2.102.0
        ("sha", "d" * 40),
        ("runner", "self-hosted"),
        ("issuer", "https://issuer.invalid"),
        ("predicate", "https://example.invalid/other"),
    ]:
        assert not _attestation_matches(_attestation(**{field: value}), policy), field
    # Without --cert-identity any workflow of the repository passes, as with gh.
    loose = {k: v for k, v in policy.items() if k != "--cert-identity"}
    assert _attestation_matches(_attestation(workflow=".github/workflows/other.yml"), loose)


def test_the_override_parser_is_strict() -> None:
    good = "# c\nservices:\n  web:\n    image: x@sha256:1\n    pull_policy: never\n"
    assert parse_override(good) == {"web": {"image": "x@sha256:1", "pull_policy": "never"}}
    for bad in ["", "services:\n  web:\n    build: .\n", "x: 1\n", "services:\n web:\n"]:
        try:
            parse_override(bad)
        except ValueError:
            continue
        raise AssertionError(f"accepted {bad!r}")
