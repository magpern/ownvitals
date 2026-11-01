"""``deploy/ownvitals-deploy`` under bash with fake gh, docker, sudo and flock (no network).

Each ``scenario_*`` function asserts one behaviour against a ``Host``; the tests here run them on
the script as it is, and ``test_deploy_mutations`` runs them on copies with one check removed, to
show each check is what makes its scenario pass. All data is synthetic.
"""

import stat
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from test_deploy_fakes import (
    GITHUB_ISSUER,
    IMAGE,
    REPO,
    RULESET_JQ,
    RULESETS_PATH,
    SLSA,
    parse_override,
)
from test_deploy_harness import (
    COMMITS,
    SENTINEL,
    TOKEN,
    Host,
    Run,
    commit_of,
    digest_of,
    tag_ruleset,
)

REFUSED = "refused: nothing was recorded and the running release is unchanged"
APP_SERVICES = ("migrate", "db-functions", "web", "worker")
# Every service but migrate, which the override scales to 0. db-functions runs the release's
# image, so up -d installs that release's database functions (docs/dev/deploy.md).
UP_STARTS = ["db", "db-functions", "web", "worker"]
COMPARE_C = f"repos/{REPO}/compare/{COMMITS['C']}...refs/heads/main?per_page=1"
CHECKS = [
    "lock:",
    "state:",
    "command:",
    "version: no deployment recorded",
    "gh:",
    "tag: refs/tags/{tag} is commit",
    "tag: refs/tags/{tag} is protected",
    "main:",
    "attestation:",
    "pull:",
    "compose:",
]


def deploy(host: Host, tag: str, digest: str | None = None, **kw: Any) -> Run:
    return host.run(f"deploy {tag} {digest or digest_of(tag)}", **kw)


def refused(run: Run, check: str) -> None:
    assert run.code == 1, (run.code, run.out, run.err)
    assert f"FAIL  {check}" in run.err, run.err
    assert REFUSED in run.err, run.err


def compose_rest(argv: list[str]) -> list[str]:
    """The compose command after the global flags and the files (other commands: as they are)."""
    files = [i for i, a in enumerate(argv) if a == "--file"]
    return argv[files[-1] + 2 :] if argv[:1] == ["compose"] and files else argv


def started(host: Host) -> list[list[str]]:
    """Every pull, migrate run and up the fakes saw."""
    docker = [c["argv"] for c in host.calls("docker")]
    return [a for a in docker if a[0] == "pull" or compose_rest(a)[:1] in (["run"], ["up"])]


def migrations(host: Host) -> list[str]:
    """The image of every migrate container the fakes started: by ``run``, or by an ``up -d``
    that started every service whose scale isn't 0, as compose does."""
    return [
        c["override"]["migrate"]["image"]
        for c in host.calls("docker")
        if compose_rest(c["argv"])[:1] == ["run"] or "migrate" in c.get("started", [])
    ]


def state(current: str, previous: str = "", highest: str | None = None) -> str:
    """The state file the script writes; the highest release defaults to the current one."""
    p_digest, highest = (digest_of(previous) if previous else ""), highest or current
    return (
        f"current_tag={current}\ncurrent_digest={digest_of(current)}\n"
        f"previous_tag={previous}\nprevious_digest={p_digest}\n"
        f"highest_tag={highest}\nhighest_commit={commit_of(highest)}\n"
        f"highest_digest={digest_of(highest)}\n"
    )


def gh_calls(tag: str, commit: str, highest: str | None = None) -> list[list[str]]:
    """A deploy's gh calls; ``highest`` (a commit) adds the history check of a later deploy."""
    api = ["api", "--method", "GET"]
    history = f"repos/{REPO}/compare/{highest}...{commit}?per_page=1"
    return [
        ["--version"],
        [*api, f"repos/{REPO}/commits/refs/tags/{tag}", "--jq", ".sha"],
        [*api, RULESETS_PATH, "--jq", ".[].id"],
        [*api, f"repos/{REPO}/rulesets/900001", "--jq", RULESET_JQ],
        [*api, f"repos/{REPO}/rulesets/900002", "--jq", RULESET_JQ],
        [*api, f"repos/{REPO}/compare/{commit}...refs/heads/main?per_page=1", "--jq", ".status"],
        *([[*api, history, "--jq", ".status"]] if highest else []),
        [
            "attestation",
            "verify",
            f"oci://{IMAGE}@{digest_of(tag)}",
            "--repo",
            REPO,
            "--cert-identity",
            f"https://github.com/{REPO}/.github/workflows/release.yml@refs/tags/{tag}",
            "--cert-oidc-issuer",
            GITHUB_ISSUER,
            "--source-ref",
            f"refs/tags/{tag}",
            "--source-digest",
            commit,
            "--deny-self-hosted-runners",
            "--predicate-type",
            SLSA,
        ],
    ]


def pinned(tag: str) -> dict[str, dict[str, str]]:
    """The override for ``tag``: the app services on its digest, migrate at scale 0."""
    image = f"{IMAGE}@{digest_of(tag)}"
    services = {s: {"image": image, "pull_policy": "never"} for s in APP_SERVICES}
    services["migrate"]["scale"] = "0"
    return services


# --- scenarios ------------------------------------------------------------------------------


def scenario_deploy(host: Host) -> None:
    """Every check passes in order, the image is pulled by digest only, migrations run, the pair
    is recorded, and nothing leaks: no token, no tool output, no inherited environment."""
    run = deploy(host, "v1.2.0")
    assert run.code == 0, run.err
    digest, commit = digest_of("v1.2.0"), COMMITS["C"]
    oks = [line.removeprefix("ok    ") for line in run.out.splitlines() if line.startswith("ok")]
    expected = [c.format(tag="v1.2.0") for c in CHECKS] + [
        "migrations:",
        "state: recorded",
        "up -d:",
    ]
    assert len(oks) == len(expected), oks
    assert all(line.startswith(want) for line, want in zip(oks, expected, strict=True)), oks
    assert run.out.splitlines()[-1] == f"deployed v1.2.0 {digest}"
    assert [c["argv"] for c in host.calls("gh")] == gh_calls("v1.2.0", commit)
    docker = host.calls("docker")
    assert docker[0]["argv"] == ["pull", "--quiet", f"{IMAGE}@{digest}"]
    assert [compose_rest(c["argv"]) for c in docker[1:]] == [
        ["config", "--services"],
        ["config", "--images"],
        ["config"],
        ["run", "--rm", "-T", "--pull", "never", "migrate"],
        ["up", "-d", "--pull", "never", "--no-build"],
    ]
    assert all(c["override"] == pinned("v1.2.0") for c in docker[2:])
    assert docker[-1]["started"] == UP_STARTS
    assert migrations(host) == [f"{IMAGE}@{digest}"]  # the run step, once; up -d starts none
    assert [c["tool"] for c in host.calls()][:2] == ["flock", "gh"]
    assert all(c["status"] == 0 and c["leaked_env"] == [] for c in host.calls())
    assert all(c["gh_token_ok"] for c in host.calls("gh"))
    assert all(TOKEN not in " ".join(c["argv"]) for c in host.calls())
    assert host.state() == state("v1.2.0")
    assert parse_override(host.override() or "") == pinned("v1.2.0")
    assert not any(p.name.endswith((".next", ".next.yaml")) for p in host.state_dir.iterdir())
    for text in (run.out, run.err):
        assert TOKEN not in text
        assert SENTINEL not in text


def scenario_refuses_unprotected_tag(host: Host) -> None:
    host.world["rulesets"] = [tag_ruleset(rules=[{"type": "update"}])]
    refused(deploy(host, "v1.2.0"), "tag: no active tag ruleset")
    assert started(host) == []


def scenario_refuses_evaluate_mode_ruleset(host: Host) -> None:
    host.world["rulesets"] = [tag_ruleset(enforcement="evaluate")]
    refused(deploy(host, "v1.2.0"), "tag: no active tag ruleset")


def scenario_refuses_a_ruleset_the_owner_may_bypass(host: Host) -> None:
    """current_user_can_bypass: the token owner's role is on the bypass list."""
    host.world["rulesets"] = [tag_ruleset(current_user_can_bypass="always")]
    refused(deploy(host, "v1.2.0"), "tag: no active tag ruleset")


def scenario_refuses_an_unexpected_ruleset_id(host: Host) -> None:
    """An id that is not 1 to 19 digits is never put into an API path."""
    for bad in ["abc", "-1", "1.5", "9" * 20, "900002/../900002"]:
        host.world["rulesets"] = [tag_ruleset(id=bad)]
        host.log.unlink(missing_ok=True)
        refused(deploy(host, "v1.2.0"), "tag: the API returned an unexpected ruleset id")
        assert not any(f"repos/{REPO}/rulesets/" in " ".join(c["argv"]) for c in host.calls("gh"))


def scenario_refuses_excluded_tag(host: Host) -> None:
    host.world["rulesets"] = [
        tag_ruleset(conditions={"ref_name": {"include": ["refs/tags/v*"], "exclude": ["*/v1.2*"]}})
    ]
    refused(deploy(host, "v1.2.0"), "tag: no active tag ruleset")


def scenario_refuses_a_branch_ruleset(host: Host) -> None:
    host.world["rulesets"] = [tag_ruleset(target="branch")]
    refused(deploy(host, "v1.2.0"), "tag: no active tag ruleset")


def scenario_refuses_a_ruleset_for_other_tags(host: Host) -> None:
    host.world["rulesets"] = [
        tag_ruleset(conditions={"ref_name": {"include": ["refs/tags/release-*"], "exclude": []}})
    ]
    refused(deploy(host, "v1.2.0"), "tag: no active tag ruleset")


def scenario_refuses_malformed_commands(host: Host) -> None:
    """Before anything runs: no lock, no gh, no docker."""
    digest = digest_of("v1.2.0")
    for command in ["rollback now", f"deploy v1.2.0 {digest} extra"]:
        refused(host.run(command), "command:")
    refused(host.run(f"deploy v1.2.0-rc.1 {digest}"), "tag:")
    refused(host.run(f"deploy v1.2.0 sha256:{digest[7:].upper()}"), "digest:")
    process = host.start(f"deploy v1.2.0 {digest}", args=("rollback",))
    _, err = process.communicate(timeout=60)
    assert process.returncode == 1
    assert "FAIL  arguments:" in err
    assert host.calls() == []


def scenario_refuses_a_state_file_it_did_not_write(host: Host) -> None:
    good = state("v1.2.0", "v1.1.0")
    for text in [
        good + "x=1\n",
        good.replace("current_tag=v1.2.0", "current_tag=v1.2"),
        good.replace(f"previous_digest={digest_of('v1.1.0')}", "previous_digest="),
        good.replace(f"highest_commit={COMMITS['C']}", "highest_commit=C"),
    ]:
        (host.state_dir / "state").write_bytes(text.encode())
        refused(host.run("rollback"), "state: ")
    assert host.calls("gh") == []


def scenario_refuses_a_malformed_token(host: Host) -> None:
    (host.conf / "github-token").write_bytes(f"{TOKEN} trailing words\n".encode())
    refused(deploy(host, "v1.2.0"), "gh: ")
    assert host.calls("gh") == []


def scenario_refuses_commit_off_main(host: Host) -> None:
    """v2.0.0 is attested and protected, but its commit is on a side branch."""
    refused(deploy(host, "v2.0.0"), "main: refs/heads/main does not contain")
    assert not any(c["argv"][0] == "attestation" for c in host.calls("gh"))
    assert started(host) == []


def scenario_refuses_without_cert_identity(host: Host) -> None:
    host.world["attestations"][2]["workflow"] = ".github/workflows/other.yml"
    refused(deploy(host, "v1.2.0"), "attestation:")
    assert started(host) == []


def scenario_refuses_other_source_ref(host: Host) -> None:
    host.world["attestations"][2]["source_ref"] = "refs/heads/main"
    refused(deploy(host, "v1.2.0"), "attestation:")


def scenario_refuses_other_source_commit(host: Host) -> None:
    host.world["attestations"][2]["sha"] = COMMITS["B"]
    refused(deploy(host, "v1.2.0"), "attestation:")


def scenario_refuses_self_hosted_runner(host: Host) -> None:
    host.world["attestations"][2]["runner"] = "self-hosted"
    refused(deploy(host, "v1.2.0"), "attestation:")


def scenario_refuses_old_gh(host: Host) -> None:
    host.world["gh_version"] = "2.101.9"
    refused(deploy(host, "v1.2.0"), "gh: 2.101.9 is older than 2.102.0")
    assert [c["argv"] for c in host.calls("gh")] == [["--version"]]


def scenario_refuses_a_service_by_tag(host: Host) -> None:
    host.world["compose"]["scheduler"] = f"{IMAGE}:1.2.0"
    refused(deploy(host, "v1.2.0"), "compose: a service names")
    assert [a[0] for a in started(host)] == ["pull"]


def scenario_refuses_a_missing_service(host: Host) -> None:
    del host.world["compose"]["worker"]
    refused(deploy(host, "v1.2.0"), "compose: ")
    assert [a[0] for a in started(host)] == ["pull"]


def scenario_rollback_runs_every_check(host: Host) -> None:
    """One step back through the same checks, the previous digest, and no migrations."""
    assert deploy(host, "v1.1.0").code == 0
    assert deploy(host, "v1.2.0").code == 0
    host.log.unlink()
    run = host.run("rollback")
    assert run.code == 0, run.err
    assert run.out.splitlines()[-1] == f"rolled back to v1.1.0 {digest_of('v1.1.0')}"
    assert [c["argv"] for c in host.calls("gh")] == gh_calls("v1.1.0", COMMITS["B"])
    docker = host.calls("docker")
    assert docker[0]["argv"] == ["pull", "--quiet", f"{IMAGE}@{digest_of('v1.1.0')}"]
    assert [compose_rest(c["argv"])[0] for c in docker[1:]] == ["config", "config", "config", "up"]
    assert docker[-1]["override"] == pinned("v1.1.0")
    # up -d re-runs db-functions from v1.1.0's image: the previous release's functions return.
    assert docker[-1]["override"]["db-functions"]["image"] == f"{IMAGE}@{digest_of('v1.1.0')}"
    assert "db-functions" in docker[-1]["started"]
    assert host.state() == state("v1.1.0", highest="v1.2.0")


def scenario_rollback_runs_no_migration(host: Host) -> None:
    """up -d starts every service whose scale isn't 0, and web waits for migrate: the override's
    scale 0 is what keeps the older release's migrate (which fails on the newer schema) from
    running and failing the rollback."""
    assert deploy(host, "v1.1.0").code == 0
    assert deploy(host, "v1.2.0").code == 0
    host.log.unlink()
    host.world["migrate_exit"] = 1  # v1.1.0's alembic does not know v1.2.0's revision
    run = host.run("rollback")
    assert run.code == 0, (run.out, run.err)
    assert migrations(host) == []
    assert [c.get("started") for c in host.calls("docker") if "started" in c] == [UP_STARTS]


def scenario_rollback_refused_when_previous_attestation_is_gone(host: Host) -> None:
    assert deploy(host, "v1.1.0").code == 0
    assert deploy(host, "v1.2.0").code == 0
    host.world["attestations"] = [a for a in host.world["attestations"] if a["sha"] != COMMITS["B"]]
    host.log.unlink()
    refused(host.run("rollback"), "attestation:")
    assert started(host) == []
    assert host.state() == state("v1.2.0", "v1.1.0")


def scenario_state_records_deployed_and_previous(host: Host) -> None:
    for tag, expected in [
        ("v1.0.0", state("v1.0.0")),
        ("v1.1.0", state("v1.1.0", "v1.0.0")),
        ("v1.1.0", state("v1.1.0", "v1.0.0")),  # a redeploy keeps the rollback target
        ("v1.2.0", state("v1.2.0", "v1.1.0")),
    ]:
        assert deploy(host, tag).code == 0
        assert host.state() == expected
    assert host.run("rollback").code == 0
    assert host.state() == state("v1.1.0", highest="v1.2.0")  # a rollback keeps the highest
    refused(host.run("rollback"), "rollback: no previous deployment is recorded")
    refused(deploy(host, "v1.0.0"), "version: v1.0.0 is below the highest deployed v1.2.0")
    assert host.state() == state("v1.1.0", highest="v1.2.0")
    assert deploy(host, "v1.2.0").code == 0  # forward again after a rollback
    assert host.state() == state("v1.2.0", "v1.1.0")


def scenario_up_failure_keeps_the_rollback_target(host: Host) -> None:
    """Recorded after the migrations, before up -d: a failed start still rolls back one step."""
    assert deploy(host, "v1.1.0").code == 0
    host.world["up_exit"] = 1
    run = deploy(host, "v1.2.0")
    assert run.code == 2, (run.out, run.err)
    assert "FAIL  up -d" in run.err
    assert host.state() == state("v1.2.0", "v1.1.0")
    host.world["up_exit"] = 0
    assert host.run("rollback").code == 0
    assert host.state() == state("v1.1.0", highest="v1.2.0")


def scenario_migration_failure_records_nothing(host: Host) -> None:
    host.world["migrate_exit"] = 1
    refused(deploy(host, "v1.2.0"), "migrations:")
    assert not any(compose_rest(a)[:1] == ["up"] for a in started(host))
    assert host.state() is None
    assert host.override() is None


def scenario_one_deploy_at_a_time(host: Host) -> None:
    """A second deploy while one holds the lock is refused and calls nothing."""
    waiting, release = host.tmp / "waiting", host.tmp / "release"
    host.world["block"] = {"step": "up", "waiting": str(waiting), "release": str(release)}
    first = host.start(f"deploy v1.2.0 {digest_of('v1.2.0')}")
    try:
        deadline = time.monotonic() + 60
        while not waiting.exists():
            assert first.poll() is None, first.communicate()
            assert time.monotonic() < deadline, "the first deploy never reached up -d"
            time.sleep(0.05)
        calls = len(host.calls())
        second = deploy(host, "v1.1.0")
        assert second.code == 75, (second.out, second.err)
        assert "FAIL  lock: another deploy or rollback is running" in second.err
        assert [c["tool"] for c in host.calls()[calls:]] == ["flock"]
    finally:
        release.write_text("go\n", "utf-8")
        _, err = first.communicate(timeout=60)
    assert first.returncode == 0, err
    assert host.state() == state("v1.2.0")
    host.world["block"] = None
    assert deploy(host, "v1.2.0").code == 0  # the lock went with the first process


def scenario_hops_to_root_through_sudo(host: Host) -> None:
    """As the deploy user: exactly ``sudo -n -- <script>``, the command kept in the environment."""
    command = f"deploy v1.2.0 {digest_of('v1.2.0')}"
    run = host.run(command)
    assert run.code == 0, run.err
    assert [(c["tool"], c["argv"], c.get("ssh_original_command")) for c in host.calls()] == [
        ("sudo", ["-n", "--", host.script.as_posix()], command)
    ]


def scenario_environment_reaches_no_tool(host: Host) -> None:
    marker = host.tmp / "bash_env_ran"
    hook = host.tmp / "hook.sh"
    hook.write_bytes(f"echo ran > '{marker.as_posix()}'\n".encode())
    env = {
        "BASH_ENV": hook.as_posix(),
        "GH_TOKEN": "ambient-token-that-must-not-be-used",
        "DOCKER_HOST": "tcp://ambient.invalid:2375",
        "COMPOSE_FILE": "/elsewhere/compose.yaml",
        "LC_ALL": "tr_TR.UTF-8",
        "LANG": "tr_TR.UTF-8",
    }
    run = deploy(host, "v1.2.0", env=env)
    assert run.code == 0, run.err
    assert not marker.exists()
    assert all(c["leaked_env"] == [] for c in host.calls())
    assert all(c["gh_token_ok"] for c in host.calls("gh"))


def scenario_tool_output_is_never_shown(host: Host) -> None:
    """The fakes print a marker on every gh api, compose config, pull, verify, run and up call
    (on stderr only where the script reads stdout): a compose error can quote .env lines."""
    host.world["noise"] = "TOOL-LOG-MARKER-0193"
    runs: list[Run] = []
    for change, check in [
        ({"attestations": []}, "attestation:"),
        ({"api_errors": [COMPARE_C]}, "main:"),  # a failing gh api call
        ({"compose_valid": False}, "compose:"),  # a failing config --services
    ]:
        world = dict(host.world)
        host.world |= change
        runs.append(deploy(host, "v1.2.0"))
        refused(runs[-1], check)
        host.world = world
    runs.append(deploy(host, "v1.2.0"))
    assert runs[-1].code == 0, runs[-1].err
    for run in runs:
        assert "TOOL-LOG-MARKER-0193" not in run.out + run.err


def scenario_files_are_root_only(host: Host) -> None:
    """umask 077: the state file, the override and the lock are 0600, gh's directory 0700, even
    when the script starts with umask 022."""
    if sys.platform == "win32":
        pytest.skip("POSIX file modes; CI runs this on Linux")
    assert deploy(host, "v1.1.0").code == 0
    assert deploy(host, "v1.2.0").code == 0
    modes = {
        p.name: stat.S_IMODE(p.stat().st_mode)
        for p in host.state_dir.iterdir()
        if not p.name.endswith(".held")  # the fake flock's own
    }
    assert modes == {"state": 0o600, "compose.image.yaml": 0o600, "lock": 0o600, "gh": 0o700}


# --- tests ----------------------------------------------------------------------------------

SCENARIOS: list[Callable[[Host], None]] = [
    value for name, value in dict(globals()).items() if name.startswith("scenario_")
]


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.__name__.removeprefix("scenario_"))
def test_scenario(tmp_path: Path, scenario: Callable[[Host], None]) -> None:
    root_uid = "-1" if scenario is scenario_hops_to_root_through_sudo else None
    scenario(Host(tmp_path, root_uid=root_uid))
