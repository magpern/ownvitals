"""The load harness of the rate limiters (#462): profiles, the fake Withings answer, the report.

Two profiles drive the same tests. ``CI`` is small and fixed (at least 50 concurrent callers,
a few seconds of wall time) and runs in the normal backend shards. ``FULL`` is marked ``load``,
excluded from the default run (``pyproject.toml`` ``addopts``) and sized by environment
variables, so the owner can run it on a throwaway stack (``docs/dev/load.md``).

Everything is synthetic: generated account ids, documentation IP ranges (RFC 5737), example.test
addresses, and a fake Withings answer built in memory. Nothing here opens a socket beyond the
loopback Postgres; the suite's network guard (``tests/conftest.py``) covers these tests too.

Reports are counts only (admitted, refused, p95 wait): no value, no id, no timestamp.
"""

import math
import os
import threading
from collections.abc import Sequence
from dataclasses import dataclass, field

import pytest

# The CI variant's size. The acceptance criteria ask for at least 50 concurrent callers.
CI_CALLERS = 50
# One Postgres connection per caller: the default max_connections is 100, and the test's own
# superuser and app engines need a few.
MAX_CALLERS = 90


@dataclass(frozen=True)
class Profile:
    """How hard one run pushes. ``workers`` concurrent callers, ``users`` synthetic accounts."""

    name: str
    workers: int
    users: int
    budget: int  # the Withings bucket's budget a minute (the schema allows 1 to 100)
    rounds: int  # how many times each auth scenario is repeated, each on fresh keys
    deadline: float  # seconds: a run that has not finished by then fails instead of hanging


CI = Profile("ci", workers=CI_CALLERS, users=10, budget=20, rounds=1, deadline=45.0)


# Read once, when the harness is imported (collection): the suite's autouse fixture removes
# every OWNVITALS_* variable but the test database URL before each test runs.
_SETTINGS = {k: v for k, v in os.environ.items() if k.startswith("OWNVITALS_LOAD_")}


def _env_int(name: str, default: int, low: int, high: int) -> int:
    raw = _SETTINGS.get(name)
    if raw is None or raw == "":
        return default
    value = int(raw)
    if not low <= value <= high:
        raise pytest.UsageError(f"{name} must be {low} to {high}")
    return value


def full_profile() -> Profile:
    """The owner's run: ``OWNVITALS_LOAD_WORKERS``, ``_USERS``, ``_BUDGET``, ``_ROUNDS``."""
    workers = _env_int("OWNVITALS_LOAD_WORKERS", MAX_CALLERS, CI_CALLERS, MAX_CALLERS)
    users = _env_int("OWNVITALS_LOAD_USERS", 30, 2, workers)
    budget = _env_int("OWNVITALS_LOAD_BUDGET", 100, 4, 100)
    rounds = _env_int("OWNVITALS_LOAD_ROUNDS", 5, 1, 100)
    return Profile("full", workers, users, budget, rounds, deadline=600.0)


def profiles() -> list[object]:
    """The parametrisation every load test takes: ``ci`` always, ``full`` only with ``-m load``."""
    return [
        pytest.param(CI, id="ci"),
        pytest.param(None, id="full", marks=pytest.mark.load),
    ]


def resolve(profile: Profile | None) -> Profile:
    return profile if profile is not None else full_profile()


def p95(samples: Sequence[float]) -> float:
    """The nearest-rank 95th percentile, 0 for no samples."""
    if not samples:
        return 0.0
    ordered = sorted(samples)
    return ordered[max(0, math.ceil(0.95 * len(ordered)) - 1)]


@dataclass
class Report:
    """One scenario's counts. ``waits`` are seconds from a caller's first ask to its grant."""

    scenario: str
    profile: str
    callers: int
    admitted: int = 0
    refused: int = 0
    waits: list[float] = field(default_factory=lambda: list[float]())

    def line(self) -> str:
        return (
            f"{self.scenario} [{self.profile}]: callers={self.callers} admitted={self.admitted} "
            f"refused={self.refused} p95_wait_ms={p95(self.waits) * 1000:.1f}"
        )


REPORTS = pytest.StashKey[list[Report]]()


class _Summary:
    """Prints the reports at the end of the run. A plugin registered on the first report, not a
    ``conftest.py``: a second module named ``conftest`` would shadow ``tests/conftest.py`` for
    the tests that import from it."""

    def __init__(self, config: pytest.Config) -> None:
        self._config = config

    @pytest.hookimpl
    def pytest_terminal_summary(self, terminalreporter: pytest.TerminalReporter) -> None:
        reports = self._config.stash.get(REPORTS, [])
        if reports:
            terminalreporter.section("rate limiter load (counts only)")
            for report in reports:
                terminalreporter.write_line(report.line())


SUMMARY = "ownvitals-load-summary"


def record(config: pytest.Config, report: Report) -> None:
    """Keep the report for the terminal summary."""
    config.stash.setdefault(REPORTS, []).append(report)
    if not config.pluginmanager.has_plugin(SUMMARY):
        config.pluginmanager.register(_Summary(config), SUMMARY)


class FakeWithings:
    """A fake Withings API answer, in memory. Call ``n`` (1-based) of the run answers as
    ``answers`` says (``601`` or ``429``); every other call answers an empty success. The
    bodies have the public API's shape; nothing is sent anywhere.

    With ``hold_after``, every call numbered above it waits until ``open_gate`` (a slow
    provider while the 601 is handled): those calls keep their slots, so once the four slots
    are held no claim can be granted, and at most three calls follow call ``hold_after``
    before the gate opens, however long the pause waits for the bucket lock."""

    OK = b'{"status": 0, "body": {}}'
    RATE_601 = b'{"status": 601, "body": {"wait_seconds": 30}}'

    def __init__(
        self,
        answers: dict[int, int] | None = None,
        *,
        hold_after: int | None = None,
        timeout: float = 60.0,
    ) -> None:
        self._answers = answers or {}
        self._lock = threading.Lock()
        self._gate = threading.Event()
        self._hold_after = hold_after
        self._timeout = timeout
        self.calls = 0
        if hold_after is None:
            self._gate.set()

    def open_gate(self) -> None:
        self._gate.set()

    def call(self) -> tuple[int, bytes]:
        """One call: the HTTP status and body."""
        with self._lock:
            self.calls += 1
            number = self.calls
            answer = self._answers.get(number)
        if (
            self._hold_after is not None
            and number > self._hold_after
            and not self._gate.wait(self._timeout)
        ):
            raise TimeoutError("the fake Withings gate never opened")
        if answer == 601:
            return 200, self.RATE_601
        if answer == 429:
            return 429, b""
        return 200, self.OK


class InFlight:
    """Counts the calls between grant and release; keeps the highest count seen."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.now = 0
        self.peak = 0

    def current(self) -> int:
        with self._lock:
            return self.now

    def enter(self) -> None:
        with self._lock:
            self.now += 1
            self.peak = max(self.peak, self.now)

    def leave(self) -> None:
        with self._lock:
            self.now -= 1
