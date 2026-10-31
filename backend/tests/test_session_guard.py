"""The session guard of ADR 0002 section 6 (``session_scan``), and proof that it can fail."""

from pathlib import Path

import pytest

from session_scan import GUARDED, SYSTEM_SESSION, USER_SESSION, Allowlist, scan_source

SRC = Path(__file__).resolve().parent.parent / "src"


def _module(path: Path) -> str:
    parts = list(path.relative_to(SRC).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def test_only_allowlisted_modules_open_a_session() -> None:
    paths = sorted((SRC / "ownvitals").rglob("*.py"))
    assert len(paths) > 10, "the scan must see the package"
    violations = [
        (_module(path), line, why)
        for path in paths
        for line, why in scan_source(
            path.read_text("utf-8"), _module(path), is_package=path.name == "__init__.py"
        )
    ]
    assert violations == []


def test_the_allowlists_are_exactly_the_reviewed_ones() -> None:
    # A later package joins in its own PR, with its reason (session_scan's docstring).
    user = Allowlist(
        prefixes=("ownvitals.db", "ownvitals.auth", "ownvitals.connections"),
        modules=("ownvitals.jobs.fence", "ownvitals.jobs.service", "ownvitals.jobs.progress"),
    )
    system = Allowlist(
        prefixes=("ownvitals.db", "ownvitals.auth"),
        modules=("ownvitals.jobs.queue", "ownvitals.crypto.keks"),
    )
    private = Allowlist(prefixes=("ownvitals.db",))
    assert (user, system) == (USER_SESSION, SYSTEM_SESSION)
    assert {
        "user_session": user,
        "system_session": system,
        "_owned_session": private,
        "SET_USER": private,
    } == GUARDED


@pytest.mark.parametrize(
    "module",
    [
        "ownvitals.worker",
        "ownvitals.worker.loop",
        "ownvitals.web",
        "ownvitals.cli",
        "ownvitals.jobs",
        "ownvitals.jobs.runner",
        "ownvitals.jobs.run",
        "ownvitals.jobs.transitions",
        "ownvitals.jobs.issuer",
        "ownvitals.jobs.routes",
        "ownvitals.jobs.sync",  # a job kind under ownvitals.jobs gets no exemption
        "ownvitals.jobs.fence.kind",  # exact modules: nothing under them
        "ownvitals.jobs.queuex",
        "ownvitals.withings.sync",
        "ownvitals.analysis",
        "ownvitals.admin",
        "ownvitals.flags",
        "ownvitals.dbtools",
        "ownvitals.authx",
        "ownvitals.connectionsx",
    ],
)
def test_worker_kind_and_other_code_is_not_allowlisted(module: str) -> None:
    assert not USER_SESSION.allows(module)
    assert not SYSTEM_SESSION.allows(module)


@pytest.mark.parametrize(
    ("module", "user", "system"),
    [
        ("ownvitals.db", True, True),
        ("ownvitals.db.session", True, True),
        ("ownvitals.auth.deps", True, True),
        ("ownvitals.connections.repository", True, False),
        ("ownvitals.jobs.fence", True, False),
        ("ownvitals.jobs.service", True, False),
        ("ownvitals.jobs.progress", True, False),
        ("ownvitals.jobs.queue", False, True),
        # The key ring's startup check (#8): that exact module, nothing else in the package.
        ("ownvitals.crypto.keks", False, True),
        ("ownvitals.crypto", False, False),
        ("ownvitals.crypto.envelope", False, False),
        ("ownvitals.crypto.keks.other", False, False),
    ],
)
def test_the_owning_modules_are_allowlisted(module: str, user: bool, system: bool) -> None:
    assert (USER_SESSION.allows(module), SYSTEM_SESSION.allows(module)) == (user, system)


M = "ownvitals.worker.loop"

ALLOWED = [
    "from ownvitals.jobs import fenced_session\nasync with fenced_session(f, lease) as fs: pass",
    "from ownvitals.db import TransactionOwnedError, create_engine",
    "from ownvitals.auth import revoke_user_sessions\nrevoke_user_sessions(db, uid)",
    '"""Writes go through fenced_session, never user_session or app.user_id."""',
    "def f():\n    '''A docstring may name system_session and app.user_id.'''",
    "x = 'user sessions'",
]
REFUSED = {
    "import": "from ownvitals.db import user_session",
    "alias": "from ownvitals.db import user_session as us",
    "system import": "from ownvitals.db import system_session",
    "system alias": "from ownvitals.db import system_session as ss",
    "module attribute": "import ownvitals.db\nownvitals.db.user_session(f, ctx)",
    "system module attribute": "import ownvitals.db\nownvitals.db.system_session(f)",
    "package attribute": "from ownvitals import db\ndb.user_session(f, ctx)",
    "session module": "from ownvitals.db.session import TransactionOwnedError",
    "session module import": "import ownvitals.db.session",
    "session submodule": "from ownvitals.db import session",
    "private helper": "from ownvitals.db.session import _owned_session",
    "getattr string": "import ownvitals.db as d\ngetattr(d, 'user_session')",
    "getattr string, system": "import ownvitals.db as d\ngetattr(d, 'system_session')",
    "session module string": "import importlib\nimportlib.import_module('ownvitals.db.session')",
    "raw set_config": "q = \"SELECT set_config('app.user_id', :u, true)\"",
    "raw SET": "q = 'SET LOCAL app.user_id = x'",
    # Postgres ignores the case of a setting's name (the #65 review, finding 3).
    "upper-case set_config": "q = \"SELECT set_config('APP.USER_ID', :u, true)\"",
    "mixed-case SET": "q = 'SET LOCAL App.User_Id = x'",
    "SET_USER by name": "from ownvitals.db.session import SET_USER",
    "SET_USER as a string": "q = getattr(m, 'SET_USER')",
    "SET_USER as an attribute": "q = m.SET_USER",
}


# The #5 review's bypasses of the import ban, in a job-kind module: each reaches
# ownvitals.db.session (whose SET_USER sets app.user_id to any value). Line numbers of the
# findings: the import itself where it is refused, and the line that reaches the module.
JOB_KIND = "ownvitals.jobs.sync"
REACHES = {
    "relative import from the module": ("from ..db.session import TransactionGuard", [1]),
    "relative import of the module": ("from ..db import session", [1]),
    "relative package, then a chain": ("from .. import db\n\nq = db.session.TransactionGuard", [3]),
    "absolute chain": ("import ownvitals.db\n\nq = ownvitals.db.session.TransactionGuard", [3]),
    "aliased package, then a chain": ("from ownvitals import db as d\n\nq = d.session.X", [3]),
    "aliased module, then a chain": (
        "import ownvitals.db.session as m\n\nq = m.TransactionGuard",
        [1, 3],
    ),
    "aliased name from the module": (
        "from ownvitals.db.session import _Guard as G\n\nG(x)",
        [1, 3],
    ),
    "relative import above the top level": ("from .... import ownvitals", [1]),
}


@pytest.mark.parametrize(("source", "lines"), REACHES.values(), ids=REACHES.keys())
def test_relative_imports_and_attribute_chains_are_resolved(source: str, lines: list[int]) -> None:
    assert sorted({line for line, _ in scan_source(source, JOB_KIND)}) == lines


def test_a_package_resolves_relative_imports_from_itself() -> None:
    source = "from .. import db\n\nq = db.session.TransactionGuard"
    assert [line for line, _ in scan_source(source, "ownvitals.jobs", is_package=True)] == [3]
    # The same text in a module one level higher climbs above the top level.
    assert [line for line, _ in scan_source(source, "ownvitals.jobs")] == [1]
    assert scan_source("from ..db import create_engine", JOB_KIND) == []
    assert scan_source("from . import session\nsession.SET_USER", "ownvitals.db.engine") == []


@pytest.mark.parametrize("source", ALLOWED)
def test_allowed_sources(source: str) -> None:
    assert scan_source(source, M) == []


@pytest.mark.parametrize("source", REFUSED.values(), ids=REFUSED.keys())
def test_refused_sources(source: str) -> None:
    assert scan_source(source, M) != []


def test_each_allowlist_grants_only_its_own_session() -> None:
    connections = "ownvitals.connections.repository"
    assert scan_source(REFUSED["import"], connections) == []
    assert scan_source(REFUSED["system import"], connections) != []  # user_session only
    assert scan_source(REFUSED["system import"], "ownvitals.auth.sessions") == []
    # Even an allowlisted package reaches the sessions only through ownvitals.db.
    for name in ("private helper", "session module", "raw set_config"):
        assert scan_source(REFUSED[name], "ownvitals.auth.sessions") != [], name
    assert scan_source(REFUSED["private helper"], "ownvitals.db.engine") == []


def test_the_job_modules_get_only_their_own_session() -> None:
    assert scan_source(REFUSED["import"], "ownvitals.jobs.fence") == []
    assert scan_source(REFUSED["system import"], "ownvitals.jobs.fence") != []
    assert scan_source(REFUSED["system import"], "ownvitals.jobs.queue") == []
    assert scan_source(REFUSED["import"], "ownvitals.jobs.queue") != []
    for name in ("private helper", "session module", "raw set_config", "upper-case set_config"):
        assert scan_source(REFUSED[name], "ownvitals.jobs.fence") != [], name


def test_a_setting_name_in_any_letter_case_is_caught() -> None:
    """Postgres compares setting names case-insensitively, so ``APP.USER_ID`` sets the RLS user
    too (the #65 review, finding 3)."""
    for spelling in ("app.user_id", "APP.USER_ID", "App.User_Id", "app.USER_id"):
        source = f"q = \"SELECT set_config('{spelling}', :u, true)\""
        assert scan_source(source, "ownvitals.worker.loop") == [(1, "sets app.user_id itself")]
        assert scan_source(source, "ownvitals.db.engine") == []  # the session module's package


def test_a_planted_session_in_a_real_module_is_found(tmp_path: Path) -> None:
    """The violation the guard exists for: a worker module opening a user session itself."""
    planted = tmp_path / "loop.py"
    planted.write_text(
        "from ownvitals.db import user_session\n\n\n"
        "async def run(factory, ctx):\n"
        "    async with user_session(factory, ctx) as db:\n"
        "        await db.execute(...)\n",
        encoding="utf-8",
    )
    found = scan_source(planted.read_text("utf-8"), "ownvitals.worker.loop")
    assert [line for line, _ in found] == [1, 5]
