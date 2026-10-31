"""``deploy/ownvitals-deploy`` when a step fails after the checks: the pull hangs, or the state
file or the override cannot be written. The exit codes are the table in docs/dev/deploy.md.

Each ``scenario_*`` passes on the script as it is; ``test_deploy_mutations`` runs them on copies
with the handling removed. Synthetic data, fake tools, no network.
"""

from collections.abc import Callable
from pathlib import Path

import pytest

from test_deploy_fakes import parse_override
from test_deploy_harness import Host, install, write
from test_deploy_script import compose_rest, deploy, pinned, refused, started, state


def scenario_a_hung_pull_is_stopped(host: Host) -> None:
    """docker pull runs under ``timeout PULL_TIMEOUT``: a pull that hangs is killed and the deploy
    refused, and the lock goes with the run."""
    write(host.script, install(host.script.read_text("utf-8"), {"PULL_TIMEOUT": "1"}))
    host.world["pull_seconds"] = 10
    refused(deploy(host, "v1.2.0"), "pull: ")
    assert host.calls("docker") == []  # killed before it finished, so the fake logged nothing
    assert host.state() is None
    host.world["pull_seconds"] = 0
    assert deploy(host, "v1.2.0").code == 0


def scenario_a_failed_state_write_records_nothing(host: Host) -> None:
    """The state file cannot be written (here its temporary name is a directory): exit 1, the
    recorded pair and the override are unchanged, and up -d does not run."""
    assert deploy(host, "v1.1.0").code == 0
    (host.state_dir / "state.next").mkdir()
    host.log.unlink()
    refused(deploy(host, "v1.2.0"), "state: ")
    assert host.state() == state("v1.1.0")
    assert parse_override(host.override() or "") == pinned("v1.1.0")
    assert not any(compose_rest(a)[:1] == ["up"] for a in started(host))


def scenario_an_override_not_put_in_place_exits_2(host: Host) -> None:
    """The pair is recorded, but the override cannot replace the old one (here a directory, which
    ``mv -T`` will not replace): exit 2 as the table says, no up -d, and rollback goes back."""
    assert deploy(host, "v1.1.0").code == 0
    override = host.state_dir / "compose.image.yaml"
    override.unlink()
    override.mkdir()
    host.log.unlink()
    run = deploy(host, "v1.2.0")
    assert run.code == 2, (run.out, run.err)
    assert (
        "FAIL  state: the override could not be put in place, up -d did not run; "
        "v1.2.0 is recorded (its migrations ran); rollback: v1.1.0"
    ) in run.err, run.err
    assert host.state() == state("v1.2.0", "v1.1.0")
    assert not any(compose_rest(a)[:1] == ["up"] for a in started(host))
    override.rmdir()
    assert host.run("rollback").code == 0
    assert host.state() == state("v1.1.0", highest="v1.2.0")
    assert parse_override(host.override() or "") == pinned("v1.1.0")


# --- tests ----------------------------------------------------------------------------------

SCENARIOS: list[Callable[[Host], None]] = [
    value for name, value in dict(globals()).items() if name.startswith("scenario_")
]


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.__name__.removeprefix("scenario_"))
def test_scenario(tmp_path: Path, scenario: Callable[[Host], None]) -> None:
    scenario(Host(tmp_path))
