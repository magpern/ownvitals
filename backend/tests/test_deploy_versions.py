"""The version rule of ``deploy/ownvitals-deploy``: a deploy never goes below the highest release
ever deployed (rollback does not lower it), and its commit descends from that release's commit.

Each ``scenario_*`` passes on the script as it is; ``test_deploy_mutations`` runs them on copies
with the rule loosened. Synthetic data, fake tools, no network.
"""

from collections.abc import Callable
from pathlib import Path

import pytest

from test_deploy_fakes import REPO
from test_deploy_harness import COMMITS, Host, Run, add_release, commit_of, digest_of
from test_deploy_script import deploy, gh_calls, refused, started, state


def over(
    host: Host,
    current: str,
    tag: str,
    digest: str | None = None,
    *,
    previous: str = "",
    highest: str | None = None,
) -> Run:
    """``deploy <tag>`` over a recorded state. gh is too old here, so a version that is let
    through stops at the next check and nothing else runs."""
    (host.state_dir / "state").write_bytes(state(current, previous, highest).encode())
    host.world["gh_version"] = "2.0.0"
    host.log.unlink(missing_ok=True)
    return deploy(host, tag, digest)


def goes_ahead(run: Run) -> None:
    assert "ok    version: " in run.out, (run.out, run.err)
    assert "FAIL  gh: 2.0.0 is older" in run.err, run.err


def held_back(host: Host, run: Run) -> None:
    refused(run, "version: ")
    assert host.calls("gh") == []


BIG, BIGGER = "v999999999999999.0.0", "v1000000000000000.0.0"  # as long as a command allows


def scenario_refuses_an_older_release(host: Host) -> None:
    for current, tag in [
        ("v1.2.0", "v1.1.0"),
        ("v1.2.0", "v1.1.99"),
        ("v1.2.0", "v0.9.9"),
        ("v2.0.0", "v1.99.99"),
        ("v1.2.1", "v1.2.0"),
    ]:
        held_back(host, over(host, current, tag))


def scenario_refuses_a_release_below_the_highest(host: Host) -> None:
    """Measured against the highest release, not the current or the previous pair: v1.1.5 is
    above the previous v1.1.0, and above the current v1.1.0 after a rollback from v1.2.0."""
    for current, previous in [("v1.2.0", "v1.1.0"), ("v1.1.0", ""), ("v1.0.0", "")]:
        held_back(host, over(host, current, "v1.1.5", previous=previous, highest="v1.2.0"))
    held_back(host, over(host, "v1.1.0", "v1.2.0", digest_of("v1.1.5"), highest="v1.2.0"))
    goes_ahead(over(host, "v1.1.0", "v1.2.0", highest="v1.2.0"))  # forward again, its digest
    goes_ahead(over(host, "v1.1.0", "v1.2.1", highest="v1.2.0"))


def scenario_refuses_the_same_release_with_another_digest(host: Host) -> None:
    held_back(host, over(host, "v1.2.0", "v1.2.0", digest_of("v1.1.0")))


def scenario_accepts_the_same_release_again(host: Host) -> None:
    goes_ahead(over(host, "v1.2.0", "v1.2.0"))


def scenario_accepts_a_newer_release(host: Host) -> None:
    for current, tag in [
        ("v1.2.0", "v1.2.1"),
        ("v1.2.0", "v1.3.0"),
        ("v1.2.0", "v2.0.0"),
        ("v0.0.0", "v0.0.1"),
    ]:
        goes_ahead(over(host, current, tag))


def scenario_compares_versions_as_numbers(host: Host) -> None:
    for current, tag, newer in [
        ("v1.9.0", "v1.10.0", True),
        ("v1.10.0", "v1.9.0", False),
        ("v1.2.9", "v1.2.10", True),
        ("v9.0.0", "v10.0.0", True),
        ("v10.0.0", "v9.9.9", False),
        (BIG, BIGGER, True),
        (BIGGER, BIG, False),
    ]:
        run = over(host, current, tag)
        if newer:
            goes_ahead(run)
        else:
            held_back(host, run)


def scenario_first_deploy_takes_any_release(host: Host) -> None:
    run = deploy(host, "v1.0.0")
    assert run.code == 0, run.err
    assert "ok    version: no deployment recorded" in run.out


def scenario_no_deploy_below_the_highest_after_a_rollback(host: Host) -> None:
    """v1.0.0, v1.2.0, rollback: v1.1.0, never deployed but below v1.2.0, is refused. v1.2.0 with
    its digest goes forward again, and so does v1.3.0, whose commit descends from v1.2.0's."""
    assert deploy(host, "v1.0.0").code == 0
    assert deploy(host, "v1.2.0").code == 0
    assert host.run("rollback").code == 0
    assert host.state() == state("v1.0.0", highest="v1.2.0")
    refused(deploy(host, "v1.1.0"), "version: v1.1.0 is below the highest deployed v1.2.0")
    refused(deploy(host, "v1.2.0", digest_of("v1.1.0")), "version: v1.2.0 was deployed with")
    assert host.state() == state("v1.0.0", highest="v1.2.0")
    run = deploy(host, "v1.2.0")
    assert run.code == 0, run.err
    assert f"ok    history: {COMMITS['C']} descends from v1.2.0's commit (identical)" in run.out
    assert host.state() == state("v1.2.0", "v1.0.0")
    commit = add_release(host.world, "v1.3.0", parent=COMMITS["C"], main=True)
    host.log.unlink()
    run = deploy(host, "v1.3.0")
    assert run.code == 0, run.err
    assert f"ok    history: {commit} descends from v1.2.0's commit (ahead)" in run.out
    assert [c["argv"] for c in host.calls("gh")] == gh_calls("v1.3.0", commit, COMMITS["C"])
    assert host.state() == state("v1.3.0", "v1.2.0")


def scenario_refuses_a_higher_tag_on_an_older_commit(host: Host) -> None:
    """v1.5.0 tagged on B, an older commit of main than v1.2.0's C: protected, on main and
    attested by its own release.yml run, but it does not descend from the highest release."""
    assert deploy(host, "v1.2.0").code == 0
    add_release(host.world, "v1.5.0", commit=COMMITS["B"])
    host.log.unlink()
    refused(deploy(host, "v1.5.0"), f"history: {COMMITS['B']} does not descend from v1.2.0's")
    assert not any(c["argv"][0] == "attestation" for c in host.calls("gh"))
    assert started(host) == []
    assert host.state() == state("v1.2.0")


def scenario_rollback_is_not_held_to_the_highest(host: Host) -> None:
    """Rollback goes back one step without the version and history rules, and keeps the highest."""
    assert deploy(host, "v1.1.0").code == 0
    assert deploy(host, "v1.2.0").code == 0
    host.log.unlink()
    assert host.run("rollback").code == 0
    assert [c["argv"] for c in host.calls("gh")] == gh_calls("v1.1.0", COMMITS["B"])
    assert host.state() == state("v1.1.0", highest="v1.2.0")


# --- tests ----------------------------------------------------------------------------------

SCENARIOS: list[Callable[[Host], None]] = [
    value for name, value in dict(globals()).items() if name.startswith("scenario_")
]


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.__name__.removeprefix("scenario_"))
def test_scenario(tmp_path: Path, scenario: Callable[[Host], None]) -> None:
    scenario(Host(tmp_path))


def test_an_unreadable_history_is_refused(tmp_path: Path) -> None:
    host = Host(tmp_path)
    assert deploy(host, "v1.1.0").code == 0
    path = f"repos/{REPO}/compare/{COMMITS['B']}...{commit_of('v1.2.0')}?per_page=1"
    host.world["api_errors"] = [path]
    refused(deploy(host, "v1.2.0"), "history: ")
    assert host.state() == state("v1.1.0")
