"""Every refusal of ``deploy/ownvitals-deploy``: malformed commands, tags and digests, and the
variants each check must refuse (or accept). Synthetic data, fake tools, no network."""

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from test_deploy_fakes import IMAGE, RULESETS_PATH
from test_deploy_harness import COMMITS, TOKEN, Host, digest_of, tag_ruleset
from test_deploy_script import deploy, refused, started, state

DIGEST = digest_of("v1.2.0")
LONG = "deploy v1.2.0 " + DIGEST + " " + "x" * 200
COMMANDS = [
    None,
    "",
    " ",
    "rollback ",
    " rollback",
    "rollback\n",
    "ROLLBACK",
    "rollback now",
    "rollback;id",
    "status",
    "deploy",
    "deploy v1.2.0",
    f"deploy v1.2.0 {DIGEST} extra",
    f"deploy  v1.2.0 {DIGEST}",
    f"deploy\tv1.2.0\t{DIGEST}",
    f"deploy v1.2.0 {DIGEST}\n",
    f"\ndeploy v1.2.0 {DIGEST}",
    f"Deploy v1.2.0 {DIGEST}",
    f"deploy v1.2.0 {DIGEST}; id",
    f"deploy v1.2.0 {DIGEST}\nrollback",
    "sh -c id",
    "bash",
    "scp -t /tmp",
    "rsync --server . /",
    "$(id)",
    LONG,
]
TAGS = [
    "1.2.0",
    "V1.2.0",
    "v1.2",
    "v1",
    "v1.2.0.0",
    "v01.2.0",
    "v1.02.0",
    "v1.2.00",
    "v1.2.0-rc.1",
    "v1.2.0-0",
    "v1.2.0+build.1",
    "v1.2.0-",
    "vv1.2.0",
    "release-1.2.0",
    "refs/tags/v1.2.0",
    "v1.2.0/../x",
    "v1.2.x",
    "v-1.2.0",
    "v1..2",
    "v1.2.0;id",
    "v1.2.0$(id)",
    "v\N{FULLWIDTH DIGIT ONE}.2.0",
    "v1.2.0\N{ZERO WIDTH SPACE}",
    "*",
    "v*",
]
DIGESTS = [
    DIGEST.upper(),
    "SHA256:" + DIGEST[7:],
    DIGEST[:-1],
    DIGEST + "0",
    DIGEST[7:],
    "sha512:" + DIGEST[7:],
    "sha256:" + "g" * 64,
    "sha256:" + "0" * 63 + "G",
    "sha256:",
    "sha256: " + DIGEST[7:],
    "sha256",
    "v1.2.0",
    "latest",
    "sha256:" + "0" * 64 + "\N{LATIN SMALL LETTER E WITH ACUTE}",
]


@pytest.fixture
def host(tmp_path: Path) -> Host:
    return Host(tmp_path)


@pytest.mark.parametrize("command", COMMANDS, ids=repr)
def test_any_other_command_is_refused_before_anything_runs(host: Host, command: str | None) -> None:
    run = host.run(command)
    # "deploy <tag> <digest>\n" splits into the right words; the newline then fails the digest.
    check = "digest:" if command and command.startswith(f"deploy v1.2.0 {DIGEST}\n") else "command:"
    refused(run, check)
    assert host.calls() == []  # not even the lock
    if command is not None and len(command.strip()) > 8:
        assert command.strip() not in run.out + run.err  # never echoed


@pytest.mark.parametrize("tag", TAGS, ids=repr)
def test_a_malformed_tag_is_refused_before_anything_runs(host: Host, tag: str) -> None:
    run = host.run(f"deploy {tag} {DIGEST}")
    assert run.code == 1, run.out
    assert "FAIL  tag: not vMAJOR.MINOR.PATCH" in run.err or "FAIL  command:" in run.err
    assert host.calls() == []


@pytest.mark.parametrize("digest", DIGESTS, ids=repr)
def test_a_malformed_digest_is_refused_before_anything_runs(host: Host, digest: str) -> None:
    run = host.run(f"deploy v1.2.0 {digest}")
    assert run.code == 1, run.out
    assert "FAIL  digest: not sha256:" in run.err or "FAIL  command:" in run.err
    assert host.calls() == []


def test_arguments_are_refused(host: Host) -> None:
    process = host.start(f"deploy v1.2.0 {DIGEST}", args=("rollback",))
    out, err = process.communicate(timeout=60)
    assert process.returncode == 1, out
    assert "FAIL  arguments: none are accepted" in err
    assert host.calls() == []


def _ref_name(include: list[str], exclude: list[str]) -> dict[str, Any]:
    return {"ref_name": {"include": include, "exclude": exclude}}


Variant = Callable[[dict[str, Any]], None]
UNPROTECTED: dict[str, Variant] = {
    "no rulesets": lambda w: w.update(rulesets=[]),
    "only the branch ruleset": lambda w: w.update(rulesets=w["rulesets"][:1]),
    "evaluate mode": lambda w: w.update(rulesets=[tag_ruleset(enforcement="evaluate")]),
    "disabled": lambda w: w.update(rulesets=[tag_ruleset(enforcement="disabled")]),
    "a branch target": lambda w: w.update(rulesets=[tag_ruleset(target="branch")]),
    "a push target": lambda w: w.update(rulesets=[tag_ruleset(target="push")]),
    "another include": lambda w: w.update(
        rulesets=[tag_ruleset(conditions=_ref_name(["refs/tags/release-*"], []))]
    ),
    "a near include": lambda w: w.update(
        rulesets=[tag_ruleset(conditions=_ref_name(["refs/tags/v1*"], []))]
    ),
    "no conditions": lambda w: w.update(rulesets=[tag_ruleset(conditions=None)]),
    "an exclude that matches": lambda w: w.update(
        rulesets=[tag_ruleset(conditions=_ref_name(["refs/tags/v*"], ["refs/tags/v1.*"]))]
    ),
    "an exclude of everything": lambda w: w.update(
        rulesets=[tag_ruleset(conditions=_ref_name(["~ALL"], ["~ALL"]))]
    ),
    "an exclude with escapes": lambda w: w.update(
        rulesets=[tag_ruleset(conditions=_ref_name(["refs/tags/v*"], ["refs/tags/\\x"]))]
    ),
    "no deletion rule": lambda w: w.update(rulesets=[tag_ruleset(rules=[{"type": "update"}])]),
    "no update rule": lambda w: w.update(rulesets=[tag_ruleset(rules=[{"type": "deletion"}])]),
    "no rules": lambda w: w.update(rulesets=[tag_ruleset(rules=[])]),
    "the list unreadable": lambda w: w.update(api_errors=[RULESETS_PATH]),
    "the ruleset unreadable": lambda w: w.update(
        api_errors=["repos/magpern/ownvitals/rulesets/900002"]
    ),
}
PROTECTED: dict[str, Variant] = {
    "~ALL included": lambda w: w.update(rulesets=[tag_ruleset(conditions=_ref_name(["~ALL"], []))]),
    "an unrelated exclude": lambda w: w.update(
        rulesets=[tag_ruleset(conditions=_ref_name(["refs/tags/v*"], ["refs/tags/nightly-*"]))]
    ),
    "the tag ruleset listed first": lambda w: w.update(rulesets=w["rulesets"][::-1]),
    "extra rules": lambda w: w.update(
        rulesets=[
            tag_ruleset(rules=[{"type": "creation"}, {"type": "update"}, {"type": "deletion"}])
        ]
    ),
}


@pytest.mark.parametrize("variant", UNPROTECTED.values(), ids=UNPROTECTED.keys())
def test_a_tag_no_active_ruleset_protects_is_refused(host: Host, variant: Variant) -> None:
    variant(host.world)
    refused(deploy(host, "v1.2.0"), "tag: ")
    assert not any(c["argv"][0] == "attestation" for c in host.calls("gh"))
    assert host.calls("docker") == []


@pytest.mark.parametrize("variant", PROTECTED.values(), ids=PROTECTED.keys())
def test_a_ruleset_that_protects_the_tag_is_accepted(host: Host, variant: Variant) -> None:
    variant(host.world)
    run = deploy(host, "v1.2.0")
    assert run.code == 0, run.err


COMPARE_C = f"repos/magpern/ownvitals/compare/{COMMITS['C']}...refs/heads/main?per_page=1"


@pytest.mark.parametrize(
    ("tag", "change", "check"),
    [
        ("v9.9.9", {}, "tag: refs/tags/v9.9.9 not found"),
        ("v1.2.0", {"tags": {"v1.2.0": "not-a-commit"}}, "tag: refs/tags/v1.2.0 did not resolve"),
        ("v2.0.0", {}, "main: refs/heads/main does not contain"),  # a side branch: diverged
        ("v3.0.0", {}, "main: refs/heads/main does not contain"),  # newer than main: behind
        ("v1.2.0", {"api_errors": [COMPARE_C]}, "main: "),
    ],
    ids=["unknown tag", "not a commit", "diverged", "behind", "compare unreadable"],
)
def test_the_tag_must_resolve_to_a_commit_on_main(
    host: Host, tag: str, change: dict[str, Any], check: str
) -> None:
    host.world |= change
    refused(deploy(host, tag), check)
    assert not any(c["argv"][0] == "attestation" for c in host.calls("gh"))
    assert host.calls("docker") == []


ATTESTATIONS: dict[str, dict[str, str]] = {
    "another workflow": {"workflow": ".github/workflows/other.yml"},
    "another tag's run": {"ref": "refs/tags/v1.1.0", "source_ref": "refs/tags/v1.1.0"},
    "a branch's run": {"ref": "refs/heads/main", "source_ref": "refs/heads/main"},
    "another source ref": {"source_ref": "refs/heads/main"},
    "a source ref differing in case": {"source_ref": "refs/tags/V1.2.0"},
    "another commit": {"sha": COMMITS["B"]},
    "a self-hosted runner": {"runner": "self-hosted"},
    "another repository": {"repo": "magpern/other"},
    "another issuer": {"issuer": "https://issuer.invalid"},
    "another predicate": {"predicate": "https://in-toto.io/attestation/release/v0.1"},
}


@pytest.mark.parametrize("change", ATTESTATIONS.values(), ids=ATTESTATIONS.keys())
def test_an_attestation_that_does_not_match_is_refused(host: Host, change: dict[str, str]) -> None:
    host.world["attestations"][2] |= change
    refused(deploy(host, "v1.2.0"), "attestation: ")
    assert host.calls("docker") == []


@pytest.mark.parametrize(
    ("digest", "change"),
    [
        (DIGEST, {"attestations": []}),
        (digest_of("v1.1.0"), {}),  # v1.1.0's image under the v1.2.0 tag
        ("sha256:" + "0" * 64, {"registry": ["sha256:" + "0" * 64]}),  # in GHCR, never attested
    ],
    ids=["none", "another release's image", "an unattested image"],
)
def test_an_image_without_its_attestation_is_refused(
    host: Host, digest: str, change: dict[str, Any]
) -> None:
    host.world |= change
    refused(deploy(host, "v1.2.0", digest), "attestation: ")
    assert host.calls("docker") == []


@pytest.mark.parametrize(
    ("version", "check"),
    [
        ("2.101.9", "gh: 2.101.9 is older than 2.102.0"),
        ("2.68.0", "gh: 2.68.0 is older"),
        ("1.999.999", "gh: 1.999.999 is older"),
        ("dev", "gh: unrecognised version"),
    ],
)
def test_an_old_or_unknown_gh_is_refused(host: Host, version: str, check: str) -> None:
    host.world["gh_version"] = version
    refused(deploy(host, "v1.2.0"), check)
    assert [c["argv"] for c in host.calls("gh")] == [["--version"]]


@pytest.mark.parametrize("version", ["2.102.0", "2.103.3", "3.0.0"])
def test_a_recent_gh_is_accepted(host: Host, version: str) -> None:
    host.world["gh_version"] = version
    assert deploy(host, "v1.2.0").code == 0


@pytest.mark.parametrize("token", [None, "", "short", TOKEN + " x", TOKEN + "\r", "a" * 256])
def test_a_missing_or_malformed_token_is_refused(host: Host, token: str | None) -> None:
    path = host.conf / "github-token"
    if token is None:
        path.unlink()
    else:
        path.write_bytes(token.encode())
    refused(deploy(host, "v1.2.0"), "gh: ")
    assert host.calls("gh") == []


def test_a_failed_pull_is_refused(host: Host) -> None:
    host.world["pull_exit"] = 1
    refused(deploy(host, "v1.2.0"), "pull: ")
    assert [c["argv"][0] for c in host.calls("docker")] == ["pull"]


COMPOSE: dict[str, tuple[dict[str, Any], str]] = {
    "no worker": (
        {"compose": {"db": "postgres:17.11", "migrate": IMAGE, "web": IMAGE}},
        "compose: ",
    ),
    "invalid": ({"compose_valid": False}, "compose: "),
    "by tag": ({"x": f"{IMAGE}:1.2.0"}, "compose: a service names"),
    "by another digest": ({"x": f"{IMAGE}@{digest_of('v1.1.0')}"}, "compose: a service names"),
    "tag and digest": ({"x": f"{IMAGE}:1.2.0@{DIGEST}"}, "compose: a service names"),
    "untagged": ({"x": IMAGE}, "compose: a service names"),
    "upper case": ({"x": "GHCR.IO/MAGPERN/OWNVITALS:latest"}, "compose: a service names"),
}


@pytest.mark.parametrize(("change", "check"), COMPOSE.values(), ids=COMPOSE.keys())
def test_compose_must_run_the_digest_only(host: Host, change: dict[str, Any], check: str) -> None:
    if "x" in change:
        host.world["compose"]["scheduler"] = change["x"]
    else:
        host.world |= change
    refused(deploy(host, "v1.2.0"), check)
    assert [a[0] for a in started(host)] == ["pull"]
    assert host.state() is None


def test_other_images_in_compose_are_left_alone(host: Host) -> None:
    host.world["compose"] |= {"cache": "redis:8", "tools": "ghcr.io/magpern/ownvitals-tools:1"}
    assert deploy(host, "v1.2.0").code == 0


STATES = {
    "an extra line": state("v1.2.0") + "x=1\n",
    "a missing line": "".join(state("v1.2.0").splitlines(keepends=True)[:3]),
    "another order": "".join(sorted(state("v1.2.0", "v1.1.0").splitlines(keepends=True))),
    "a bad tag": state("v1.2.0").replace("current_tag=v1.2.0", "current_tag=v1.2"),
    "a bad digest": state("v1.2.0").replace(DIGEST, DIGEST.upper()),
    "CRLF": state("v1.2.0", "v1.1.0").replace("\n", "\r\n"),
    "half a previous pair": state("v1.2.0").replace("previous_tag=", "previous_tag=v1.1.0"),
    "no current": "current_tag=\ncurrent_digest=\nprevious_tag=\nprevious_digest=\n",
    "empty": "",
}


@pytest.mark.parametrize("command", ["deploy", "rollback"])
@pytest.mark.parametrize("text", STATES.values(), ids=STATES.keys())
def test_a_state_file_the_script_did_not_write_is_refused(
    host: Host, text: str, command: str
) -> None:
    (host.state_dir / "state").write_bytes(text.encode())
    run = deploy(host, "v1.2.0") if command == "deploy" else host.run("rollback")
    refused(run, "state: ")
    assert host.calls("gh") == []
    assert (host.state_dir / "state").read_bytes() == text.encode()


def test_a_rollback_with_nothing_recorded_is_refused(host: Host) -> None:
    refused(host.run("rollback"), "rollback: no previous deployment is recorded")
    assert deploy(host, "v1.2.0").code == 0
    refused(host.run("rollback"), "rollback: no previous deployment is recorded")
    assert host.calls("gh")[-1]["argv"][0] == "attestation"  # the deploy's; rollback called none


ROLLBACK: dict[str, Variant] = {
    "attestation gone": lambda w: w.update(
        attestations=[a for a in w["attestations"] if a["sha"] != COMMITS["B"]]
    ),
    "tag ruleset gone": lambda w: w.update(rulesets=[]),
    "tag deleted": lambda w: w["tags"].pop("v1.1.0"),
    "commit no longer on main": lambda w: w.update(
        main=[COMMITS["A"], COMMITS["C"]], off_main=[*w["off_main"], COMMITS["B"]]
    ),
    "gh too old now": lambda w: w.update(gh_version="2.90.0"),
    "image gone from GHCR": lambda w: w.update(
        registry=[d for d in w["registry"] if d != digest_of("v1.1.0")]
    ),
}


@pytest.mark.parametrize("variant", ROLLBACK.values(), ids=ROLLBACK.keys())
def test_a_rollback_whose_previous_pair_no_longer_verifies_is_refused(
    host: Host, variant: Variant
) -> None:
    assert deploy(host, "v1.1.0").code == 0
    assert deploy(host, "v1.2.0").code == 0
    host.log.unlink()
    variant(host.world)
    run = host.run("rollback")
    assert run.code == 1, run.out
    assert "refused: nothing was recorded" in run.err
    assert started(host) == []
    assert host.state() == state("v1.2.0", "v1.1.0")
