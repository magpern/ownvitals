"""db-functions' exemption from the outbound guard (#101): the queue's one-shot runner
(``ownvitals.db.install_functions``) may use psycopg's ``connect``, the two error classes it
catches and the two conninfo helpers, and nothing else; no other module may import psycopg
directly. It installs no egress hook (docs/dev/deploy.md, "db-functions and the egress hook"),
so this static scan is its guard."""

from pathlib import Path

import pytest

from egress_classified import TABLE
from egress_scan import scan_source

SRC = Path(__file__).resolve().parent.parent / "src"
RUNNER = "ownvitals.db.install_functions"


def test_the_runner_is_classified_by_exactly_its_names() -> None:
    assert TABLE["psycopg"].names == {"connect", "Error", "ProgrammingError"}
    assert TABLE["psycopg.conninfo"].names == {"conninfo_to_dict", "make_conninfo"}
    for key in ("psycopg", "psycopg.conninfo"):
        assert TABLE[key].kind == "allowed"
        assert TABLE[key].only_in == (RUNNER,)
        assert not TABLE[key].exceptions
    assert "psycopg.*" not in TABLE  # no submodule is classified by a prefix


def test_the_runner_as_written_passes() -> None:
    source = (SRC / "ownvitals" / "db" / "install_functions.py").read_text(encoding="utf-8")
    assert scan_source(source, RUNNER) == []


DIRECT_IMPORTS = [
    "import psycopg\npsycopg.connect('host=x')",
    "from psycopg import connect\nconnect('host=x')",
    "from psycopg.conninfo import make_conninfo\nmake_conninfo(host='x')",
    "import psycopg",
]


@pytest.mark.parametrize("source", DIRECT_IMPORTS)
@pytest.mark.parametrize(
    "module",
    [
        "ownvitals.db.engine",
        "ownvitals.db",
        "ownvitals.db.install_functions_extra",
        "ownvitals.jobs.run",
        "ownvitals.web",
        "ownvitals.cli",
    ],
)
def test_no_other_module_may_import_psycopg(source: str, module: str) -> None:
    found = scan_source(source, module)
    assert found != []
    assert any(why.endswith("not classified") for _, why in found), found


@pytest.mark.parametrize(
    "source",
    [
        "import psycopg\npsycopg.AsyncConnection.connect('host=x')",
        "import psycopg\npsycopg.Connection.connect('host=x')",
        "from psycopg import Connection",
        "import psycopg\npsycopg.pq",
        "from psycopg.conninfo import conninfo_attempts",
        "import psycopg.pq",
        "import psycopg.errors",
    ],
)
def test_the_runner_may_use_no_other_psycopg_name(source: str) -> None:
    assert scan_source(source, RUNNER) != [], source


def test_the_runner_may_use_its_names() -> None:
    source = (
        "import psycopg\nfrom psycopg.conninfo import conninfo_to_dict, make_conninfo\n"
        "try:\n    psycopg.connect(make_conninfo(**conninfo_to_dict('host=x')))\n"
        "except (psycopg.ProgrammingError, psycopg.Error):\n    pass\n"
    )
    assert scan_source(source, RUNNER) == []
