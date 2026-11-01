"""A throwaway installation of ``deploy/ownvitals-deploy`` for its tests (``Host``).

``Host`` copies the script into a temporary directory and rewrites its seven installation
constants (and nothing else) to point there. The script's PATH is then the fakes for gh, docker,
sudo and flock (``test_deploy_fakes``, plus a bash flock below), followed on Linux by a directory
holding only the core utilities the script runs, so no real gh, docker, sudo or flock is ever
reachable. Nothing touches the network, a registry or a server. Every commit, tag, digest and
token is synthetic.

The tests in this file check the harness and the script as a file: its constants, its shebang,
``bash -n`` and shellcheck.
"""

import hashlib
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from test_deploy_fakes import GITHUB_ISSUER, IMAGE, REPO, RULESET_JQ, SLSA

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "deploy" / "ownvitals-deploy"
FAKES = Path(__file__).with_name("test_deploy_fakes.py")
# The seven installation lines and their production values (docs/dev/deploy.md, "Install").
INSTALL = {
    "SELF": "/usr/local/bin/ownvitals-deploy",
    "ROOT_UID": "0",
    "SAFE_PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
    "CONF_DIR": "/etc/ownvitals-deploy",
    "STATE_DIR": "/var/lib/ownvitals-deploy",
    "APP_DIR": "/opt/ownvitals",
    "DOCKER_HOME": "/root",
}
# What the script (and the bash flock fake) may run besides the fakes, on the Linux test PATH.
CORE_UTILITIES = ("mkdir", "mv", "timeout", "readlink", "cat", "rm")
TOKEN = "synthetic_read_only_token_for_tests_0001"  # not a credential
SENTINEL = "synthetic-sentinel-must-not-reach-a-tool"

FLOCK = """#!/bin/bash
# Fake flock: flock --nonblock --conflict-exit-code <code> <fd>. The lock is a directory beside
# the file the descriptor names, held while the process that took it (the script) is alive.
set -u
log='{log}'
if [[ $# -ne 4 || $1 != --nonblock || $2 != --conflict-exit-code ]]; then
  printf '{{"tool":"flock","argv":["unexpected"],"status":64}}\\n' >> "$log"
  exit 64
fi
path=$(readlink "/proc/self/fd/$4") || exit 66
held=$path.held
status=0
if ! mkdir "$held" 2> /dev/null; then
  holder=$(cat "$held/pid" 2> /dev/null) || holder=''
  if [[ -n $holder ]] && kill -0 "$holder" 2> /dev/null; then
    status=$3
  else
    rm -rf "$held" && mkdir "$held"
  fi
fi
if [[ $status == 0 ]]; then printf '%s\\n' "$PPID" > "$held/pid"; fi
leaked=''
for name in DEPLOY_TEST_SENTINEL DOCKER_HOST COMPOSE_FILE; do
  if [[ -n ${{!name+set}} ]]; then leaked+="${{leaked:+,}}\\"$name\\""; fi
done
printf '{{"tool":"flock","argv":["%s","%s","%s","%s"],"status":%s,"leaked_env":[%s]}}\\n' \\
  "$@" "$status" "$leaked" >> "$log"
exit "$status"
"""


def write(path: Path, text: str) -> None:
    """LF line endings on every platform: bash reads a CR as part of the line."""
    path.write_bytes(text.encode("utf-8"))


def synthetic_commit(name: str) -> str:
    return hashlib.sha1(f"synthetic commit {name}".encode()).hexdigest()  # noqa: S324


def digest_of(tag: str) -> str:
    return "sha256:" + hashlib.sha256(f"synthetic image {tag}".encode()).hexdigest()


def attestation(tag: str, commit: str, **changes: str) -> dict[str, str]:
    """What release.yml would attest for ``tag`` built from ``commit`` (gh's view of it)."""
    return {
        "digest": digest_of(tag),
        "repo": REPO,
        "workflow": ".github/workflows/release.yml",
        "ref": f"refs/tags/{tag}",
        "source_ref": f"refs/tags/{tag}",
        "sha": commit,
        "runner": "github-hosted",
        "issuer": GITHUB_ISSUER,
        "predicate": SLSA,
    } | changes


def tag_ruleset(**changes: Any) -> dict[str, Any]:
    """The shape of the repository's ``release tags`` ruleset (synthetic id)."""
    return {
        "id": 900002,
        "name": "release tags",
        "target": "tag",
        "source_type": "Repository",
        "source": REPO,
        "enforcement": "active",
        "conditions": {"ref_name": {"exclude": [], "include": ["refs/tags/v*"]}},
        "rules": [{"type": "update"}, {"type": "deletion"}, {"type": "non_fast_forward"}],
        "bypass_actors": [],
        "current_user_can_bypass": "never",
    } | changes


# main is A-B-C; D is off main (a side branch from B); E descends from C but is not on main yet.
COMMITS = {name: synthetic_commit(name) for name in "ABCDE"}
PARENTS = {"A": None, "B": "A", "C": "B", "D": "B", "E": "C"}
TAGS = {"v1.0.0": "A", "v1.1.0": "B", "v1.2.0": "C", "v2.0.0": "D", "v3.0.0": "E"}


def commit_of(tag: str) -> str:
    """The commit of one of ``TAGS``; any other tag gets its own (``add_release``)."""
    return COMMITS[TAGS[tag]] if tag in TAGS else synthetic_commit(tag)


def add_release(
    world: dict[str, Any],
    tag: str,
    *,
    parent: str | None = None,
    commit: str | None = None,
    main: bool = False,
) -> str:
    """Release ``tag``, attested by release.yml and in GHCR: on ``commit``, or on a new commit
    whose parent is ``parent``; ``main`` moves main to it. Returns the commit."""
    if commit is None:
        commit = commit_of(tag)
        world["parents"][commit] = parent
    world["tags"][tag] = commit
    world["attestations"].append(attestation(tag, commit))
    world["registry"].append(digest_of(tag))
    if main:
        world["main"] = commit
    return commit


def default_world() -> dict[str, Any]:
    return {
        "synthetic": True,
        "token": TOKEN,
        "gh_version": "2.103.3",
        "parents": {COMMITS[c]: p and COMMITS[p] for c, p in PARENTS.items()},
        "main": COMMITS["C"],
        "tags": {tag: COMMITS[c] for tag, c in TAGS.items()},
        "rulesets": [
            {
                "id": 900001,
                "name": "main",
                "target": "branch",
                "enforcement": "active",
                "conditions": {"ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": []}},
                "rules": [{"type": "deletion"}, {"type": "non_fast_forward"}],
            },
            tag_ruleset(),
        ],
        "attestations": [attestation(tag, COMMITS[c]) for tag, c in TAGS.items()],
        "registry": [digest_of(tag) for tag in TAGS],
        "compose": {
            "db": "postgres:17.11",
            "migrate": IMAGE,
            "db-functions": IMAGE,
            "web": IMAGE,
            "worker": IMAGE,
        },
        # Per service, the lines `docker compose config` prints under it besides its image, as
        # compose v5 normalises them (every volume spelling becomes the long form).
        "mounts": {
            "db": [
                "    volumes:",
                "      - source: db-data",
                "        target: /var/lib/postgresql/data",
                "        type: volume",
                "        volume: {}",
            ],
            "db-functions": [
                "    tmpfs:",
                "      - /run/pgpass:mode=0700,uid=10001,gid=10001",
            ],
        },
        "compose_valid": True,
        "pull_exit": 0,
        "migrate_exit": 0,
        "up_exit": 0,
        "api_errors": [],
        "noise": None,
        "block": None,
    }


def _windows_bash() -> str | None:
    found = shutil.which("bash")
    system = os.environ.get("SYSTEMROOT", r"C:\Windows").lower()
    if found and not found.lower().startswith(system):  # not WSL's bash.exe
        return found
    for candidate in (
        r"C:\Program Files\Git\usr\bin\bash.exe",
        r"C:\Program Files\Git\bin\bash.exe",
    ):
        if Path(candidate).is_file():
            return candidate
    return None


def bash() -> str:
    """Git Bash on Windows, bash elsewhere; missing is a failure in CI and a skip anywhere else."""
    found = _windows_bash() if sys.platform == "win32" else shutil.which("bash")
    if found is None:
        if os.environ.get("CI") == "true":
            pytest.fail("bash is missing; the deploy script tests must run in CI")
        pytest.skip("bash is not installed")
    return found


def _bash_path(path: Path) -> str:
    """``path`` as bash sees it in a PATH list: /c/... under Git Bash."""
    text = path.as_posix()
    if sys.platform == "win32" and len(text) > 1 and text[1] == ":":
        return f"/{text[0].lower()}{text[2:]}"
    return text


_EUID: list[str] = []


def euid() -> str:
    if not _EUID:
        out = subprocess.run(  # noqa: S603
            [bash(), "-c", 'printf %s "$EUID"'], capture_output=True, text=True, check=True
        )
        _EUID.append(out.stdout)
    return _EUID[0]


@dataclass
class Run:
    code: int
    out: str
    err: str


class Host:
    """One installation: ``conf/``, ``state/``, ``app/``, ``root/`` and the fakes, in ``tmp``."""

    def __init__(self, tmp: Path, *, script: str | None = None, root_uid: str | None = None):
        self.tmp = tmp
        self.world = default_world()
        self.conf, self.state_dir, self.app = tmp / "conf", tmp / "state", tmp / "app"
        self.fakebin, self.sysbin = tmp / "fakebin", tmp / "sysbin"
        for directory in (self.conf, self.state_dir, self.app, tmp / "root", self.fakebin):
            directory.mkdir()
        write(self.conf / "github-token", TOKEN + "\n")
        write(self.app / "compose.yaml", "# synthetic: the fake docker answers\n")
        self.world_file, self.log = tmp / "world.json", tmp / "calls.jsonl"
        self.world |= {
            "gh_dir": str(self.state_dir / "gh"),
            "app_dir": str(self.app),
            "compose_file": str(self.app / "compose.yaml"),
        }
        self._write_fakes()
        self.script = tmp / "ownvitals-deploy"
        values = {
            "SELF": self.script.as_posix(),
            "ROOT_UID": root_uid if root_uid is not None else euid(),
            "SAFE_PATH": f"{_bash_path(self.fakebin)}:{self._system_path()}",
            "CONF_DIR": self.conf.as_posix(),
            "STATE_DIR": self.state_dir.as_posix(),
            "APP_DIR": self.app.as_posix(),
            "DOCKER_HOME": (tmp / "root").as_posix(),
        }
        write(self.script, install(script or SCRIPT.read_text("utf-8"), values))

    def _write_fakes(self) -> None:
        # The fakes need only the standard library: the base interpreter starts faster than a
        # virtual environment's launcher on Windows.
        interpreter = getattr(sys, "_base_executable", sys.executable)
        python, fakes, world = (Path(p).as_posix() for p in (interpreter, FAKES, self.world_file))
        for tool in ("gh", "docker", "sudo"):
            write(
                self.fakebin / tool,
                "#!/bin/sh\nexport MSYS_NO_PATHCONV=1 MSYS2_ARG_CONV_EXCL='*'\n"
                f"exec '{python}' -I '{fakes}' {tool} '{world}' \"$@\"\n",
            )
        write(self.fakebin / "flock", FLOCK.format(log=_bash_path(self.log)))
        for fake in self.fakebin.iterdir():
            fake.chmod(0o755)

    def _system_path(self) -> str:
        if sys.platform == "win32":  # Git's /usr/bin has no gh, docker, sudo or flock
            for tool in ("gh", "docker", "sudo", "flock"):
                assert not Path(rf"C:\Program Files\Git\usr\bin\{tool}.exe").exists(), tool
            return "/usr/bin"
        self.sysbin.mkdir()
        for tool in CORE_UTILITIES:
            real = shutil.which(tool, path="/usr/bin:/bin")
            assert real is not None, tool
            (self.sysbin / tool).symlink_to(real)
        return self.sysbin.as_posix()

    def run(self, command: str | None, *, env: dict[str, str] | None = None) -> Run:
        process = self.start(command, env=env)
        out, err = process.communicate(timeout=120)
        return Run(process.returncode, out, err)

    def start(
        self, command: str | None, *, env: dict[str, str] | None = None, args: tuple[str, ...] = ()
    ) -> subprocess.Popen[str]:
        """Runs the script as sshd would: its shebang's bash options, SSH_ORIGINAL_COMMAND set."""
        write(self.world_file, json.dumps(self.world))
        shebang = self.script.read_text("utf-8").splitlines()[0].split()
        assert shebang[0] == "#!/bin/bash", shebang
        environment = {"PATH": os.environ["PATH"], "DEPLOY_TEST_SENTINEL": SENTINEL}
        for name in ("SYSTEMROOT", "WINDIR"):
            if name in os.environ:
                environment[name] = os.environ[name]
        if command is not None:
            environment["SSH_ORIGINAL_COMMAND"] = command
        environment |= env or {}
        return subprocess.Popen(  # noqa: S603
            [bash(), *shebang[1:], str(self.script), *args],
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            encoding="utf-8",
            errors="replace",
            umask=-1 if sys.platform == "win32" else 0o022,  # sshd's usual: the script sets its own
        )

    def calls(self, tool: str | None = None) -> list[dict[str, Any]]:
        if not self.log.exists():
            return []
        entries = [json.loads(line) for line in self.log.read_text("utf-8").splitlines()]
        return [e for e in entries if tool is None or e["tool"] == tool]

    def state(self) -> str | None:
        path = self.state_dir / "state"
        return path.read_text("utf-8") if path.exists() else None

    def override(self) -> str | None:
        path = self.state_dir / "compose.image.yaml"
        return path.read_text("utf-8") if path.exists() else None


def install(text: str, values: dict[str, str]) -> str:
    """``text`` with each installation line ``readonly NAME=...`` set to ``values[NAME]``."""
    lines = text.splitlines(keepends=True)
    for name, value in values.items():
        hits = [i for i, line in enumerate(lines) if line.startswith(f"readonly {name}=")]
        assert len(hits) == 1, f"readonly {name}= must appear exactly once"
        lines[hits[0]] = f"readonly {name}={value}\n"
    return "".join(lines)


# --- tests of the script as a file and of the harness ---------------------------------------


def test_the_installation_block_is_exactly_the_documented_constants() -> None:
    text = SCRIPT.read_text("utf-8")
    start = text.index("# --- Installation.")
    block = text[start : text.index("# ---\n", start)].splitlines()
    readonly = [line for line in block if line.startswith("readonly ")]
    assert readonly == [f"readonly {name}={value}" for name, value in INSTALL.items()]


def test_the_script_is_bash_p_lf_and_executable_in_git() -> None:
    raw = SCRIPT.read_bytes()
    assert raw.startswith(b"#!/bin/bash -p\n")
    assert b"\r" not in raw
    mode = subprocess.run(  # noqa: S603
        ["git", "-C", str(ROOT), "ls-files", "-s", "deploy/ownvitals-deploy"],  # noqa: S607
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    assert mode, "deploy/ownvitals-deploy is not in the git index"
    assert mode[0] == "100755", mode


def test_the_script_uses_the_jq_program_the_fake_knows() -> None:
    text = SCRIPT.read_text("utf-8")
    start = text.index("readonly RULESET_JQ='") + len("readonly RULESET_JQ='")
    assert text[start : text.index("'\n", start)] == RULESET_JQ


def test_bash_parses_the_script() -> None:
    subprocess.run([bash(), "-n", str(SCRIPT)], check=True)  # noqa: S603


def test_shellcheck_is_clean() -> None:
    shellcheck = shutil.which("shellcheck")
    if shellcheck is None:
        if os.environ.get("CI") == "true":
            pytest.fail("shellcheck is missing; the deploy script must be checked in CI")
        pytest.skip("shellcheck is not installed (CI runs it)")
    result = subprocess.run(  # noqa: S603
        [shellcheck, "--shell=bash", str(SCRIPT)], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stdout


def test_the_harness_rewrites_only_the_installation_lines(tmp_path: Path) -> None:
    host = Host(tmp_path)
    original = SCRIPT.read_text("utf-8").splitlines()
    copy = host.script.read_text("utf-8").splitlines()
    changed = [(a, b) for a, b in zip(original, copy, strict=True) if a != b]
    names = [a.split("=")[0].removeprefix("readonly ") for a, _ in changed]
    assert set(names) <= set(INSTALL)
    assert all(
        b.startswith(f"readonly {name}=") for name, (_, b) in zip(names, changed, strict=True)
    )
    assert {"STATE_DIR", "SAFE_PATH", "APP_DIR"} <= set(names)
