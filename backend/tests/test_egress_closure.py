"""The closure (``egress_closure``) and the reviewed table (``egress_classified``) behind the
outbound guard: the definition on planted libraries, the cuts, the table's coverage and
staleness, and the review's cases (PR #55 round 2), each refused through the closure and the
table rather than a hand-written list."""

import ast
import sys
from pathlib import Path

import pytest

import egress_scan
from egress_classified import EDGE_CUTS, LOOP_METHODS, TABLE, Entry
from egress_closure import SEEDS, Closure
from egress_scan import (
    CLOSURE,
    imported_modules,
    in_egress,
    object_network_names,
    scan_source,
    scan_tree,
    sources,
)
from test_egress_guard import SRC, plant

LINUX = sys.platform.startswith("linux")
WEB = "ownvitals.auth.routes"
CAPABLE = "can reach the network"

LIBRARY = {
    "fakenet.py": "import socket\n",
    "fakeuses.py": "import fakenet\n",
    "fakelazy.py": "def f():\n    import socket\n",
    "fakelazyhttp.py": "def f():\n    import http.client\n",
    "faketyped.py": "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    import ssl\n",
    "fakemain.py": "if __name__ == '__main__':\n    import socket\n",
    "faketry.py": "try:\n    import _ssl\nexcept ImportError:\n    pass\n",
    "fakeclass.py": "class A:\n    import select\n",
    "fakedyn.py": "import importlib\nimportlib.import_module('socket')\n",
    "fakepkg/__init__.py": "import socket\n",
    "fakepkg/sub.py": "x = 1\n",
    "fakecut.py": "import _socket\nCapsule = type(getattr(_socket, 'CAPI', None))\n",
    "fakeviacut.py": "import fakecut\n",
    "fakemissing.py": "import fake_not_installed\nimport fakenet.nothing\n",
}
EXPECTED = {
    "fakenet": True,
    "fakeuses": True,  # a module-level import of a capable module
    "fakelazy": True,  # a function body imports a seed
    "fakelazyhttp": False,  # a function-level import of a non-seed is not followed
    "faketyped": False,
    "fakemain": False,
    "faketry": True,
    "fakeclass": True,
    "fakedyn": True,
    "fakepkg.sub": True,  # importing it imports its package
    "fakecut": True,
    "fakeviacut": True,
    "fakemissing": True,  # the missing module is skipped, the rest still followed
}


@pytest.fixture
def library(tmp_path: Path) -> Path:
    root = tmp_path / "lib"
    for name, source in LIBRARY.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(source, encoding="utf-8")
    return root


def test_the_definition_on_planted_libraries(library: Path) -> None:
    closure = Closure(extra_paths=[str(library)])
    assert {m: closure.capable(m) for m in EXPECTED} == EXPECTED
    assert closure.path("fakeuses") == ["fakeuses", "fakenet", "socket"]
    assert closure.unresolved >= {"fake_not_installed"}
    assert not any(name.startswith("fakenet") for name in closure.unresolved)
    assert all(closure.capable(seed) for seed in SEEDS if closure.is_module(seed))


def test_a_cut_stops_propagation_through_it(library: Path) -> None:
    closure = Closure(cuts=["fakecut"], extra_paths=[str(library)])
    assert not closure.capable("fakecut")
    assert not closure.capable("fakeviacut")
    assert closure.capable("fakeuses")


def _with(monkeypatch: pytest.MonkeyPatch, library: Path, cuts: object = EDGE_CUTS) -> None:
    closure = Closure(cuts=cuts, extra_paths=[str(library)])  # pyright: ignore[reportArgumentType]
    monkeypatch.setattr(egress_scan, "CLOSURE", closure)


def test_the_guard_fails_on_a_planted_unclassified_import(
    library: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _with(monkeypatch, library)
    plant(tmp_path / "src", "ownvitals.web.planted", "import fakeuses\nimport fakelazyhttp\n")
    found = scan_tree(tmp_path / "src")
    assert [(m, line) for m, line, _ in found] == [("ownvitals.web.planted", 1)]
    assert found[0][2] == (
        "imports fakeuses: fakeuses can reach the network (fakeuses -> fakenet -> socket); "
        "not classified"
    )
    monkeypatch.setitem(TABLE, "fakeuses", Entry("by_design", "test"))
    assert scan_tree(tmp_path / "src") == []


def test_a_planted_edge_cut(library: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    plant(tmp_path / "src", "ownvitals.web.planted", "import fakeviacut\n")
    _with(monkeypatch, library)
    assert scan_tree(tmp_path / "src") != []
    _with(monkeypatch, library, {**EDGE_CUTS, "fakecut": "test"})
    assert scan_tree(tmp_path / "src") == []
    chain = "import fakeviacut\nfakeviacut.fakecut._socket.socket()"
    assert any("_socket " + CAPABLE in why for _, why in scan_source(chain, WEB))


def test_the_typing_extensions_cut_is_what_its_reason_says() -> None:
    """It imports ``_socket`` once, reads only ``_socket.CAPI`` from it, and exports nothing of
    it; a chain to ``typing_extensions._socket`` is still refused."""
    found = CLOSURE.find("typing_extensions")
    assert found is not None
    assert found.source is not None
    tree = ast.parse(found.source.read_bytes())
    seeds = [
        ast.unparse(n)
        for n in ast.walk(tree)
        if (
            isinstance(n, ast.Import | ast.ImportFrom)
            and any(a.name.split(".")[0] in SEEDS for a in n.names)
        )
        or (isinstance(n, ast.ImportFrom) and (n.module or "").split(".")[0] in SEEDS)
    ]
    assert seeds == ["import _socket"]
    loads = [n for n in ast.walk(tree) if isinstance(n, ast.Name) and n.id == "_socket"]
    reads = [
        ast.unparse(n)
        for n in ast.walk(tree)
        if isinstance(n, ast.Call) and any(a in loads for a in n.args)
    ]
    assert len(loads) == 1
    assert reads == ["getattr(_socket, 'CAPI', None)"]
    exported = next(
        n.value
        for n in tree.body
        if isinstance(n, ast.Assign) and ast.unparse(n.targets[0]) == "__all__"
    )
    assert "_socket" not in ast.literal_eval(exported)
    assert any(
        CAPABLE in w
        for _, w in scan_source("import typing_extensions\ntyping_extensions._socket.socket()", WEB)
    )
    assert Closure().capable("typing_extensions")  # without the cut it would be capable


def _imports_a_seed(node: ast.Import | ast.ImportFrom) -> bool:
    if isinstance(node, ast.ImportFrom):
        return (node.module or "").split(".")[0] in SEEDS
    return any(a.name.split(".")[0] in SEEDS for a in node.names)


def test_the_platform_cut_is_what_its_reason_says() -> None:
    """Only ``_node()`` imports a seed (``import socket``), and uses only
    ``socket.gethostname``; nothing at module level reaches the network or binds a socket."""
    found = CLOSURE.find("platform")
    assert found is not None
    assert found.source is not None
    tree = ast.parse(found.source.read_bytes())
    seeded: dict[str, list[str]] = {}
    for function in ast.walk(tree):
        if isinstance(function, ast.FunctionDef | ast.AsyncFunctionDef):
            for n in ast.walk(function):
                if isinstance(n, ast.Import | ast.ImportFrom) and _imports_a_seed(n):
                    seeded.setdefault(function.name, []).append(ast.unparse(n))
    assert seeded == {"_node": ["import socket"]}
    node = next(f for f in ast.walk(tree) if isinstance(f, ast.FunctionDef) and f.name == "_node")
    uses = {
        ast.unparse(n)
        for n in ast.walk(node)
        if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id == "socket"
    }
    assert uses == {"socket.gethostname"}
    without = Closure()
    facts = without.facts("platform")
    assert facts is not None
    assert facts.seed_in_function
    assert "socket" not in facts.bindings
    assert not any(without.capable(edge) for edge in without.edges("platform"))
    assert without.capable("platform")  # without the cut it would be capable
    assert not CLOSURE.capable("platform")


# --- the table ------------------------------------------------------------------------------


def _used() -> set[str]:
    used: set[str] = set()
    assert scan_tree(SRC, used) == []
    return used


def test_every_entry_has_a_reason_and_by_design_is_only_inbound_or_driver_code() -> None:
    assert all(entry.reason for entry in TABLE.values())
    assert all(reason for reason in EDGE_CUTS.values())
    by_design = {key for key, entry in TABLE.items() if entry.kind == "by_design"}
    allowed = {"fastapi", "fastapi.responses", "starlette.*", "uvicorn", "sqlalchemy.*", "psycopg"}
    assert by_design <= allowed


def test_an_allowed_entry_without_names_admits_only_the_import() -> None:
    assert TABLE["alembic"].names == frozenset()
    assert scan_source("from alembic import op\nop.execute('select 1')", WEB) == []
    refused = scan_source("import alembic\nalembic.__version__", WEB)
    assert any("__version__ is not an allowed name of alembic" in w for _, w in refused)
    assert scan_source("from alembic.op import get_bind", WEB) != []


def test_no_allowed_name_is_stale() -> None:
    used = _used()
    stale = {(k, n) for k, e in TABLE.items() for n in e.names if f"{k}:{n}" not in used}
    assert stale == set()


@pytest.mark.skipif(not LINUX, reason="entries and cuts are exact for Linux, where CI runs")
def test_no_entry_or_cut_is_stale() -> None:
    used = _used()
    for key, entry in TABLE.items():
        if entry.kind == "denied":
            assert CLOSURE.capable(key.removesuffix(".*")), f"{key}: not capable, drop it"
        else:
            assert key in used, f"{key}: no capable use in src, drop it"
    direct = {
        name
        for module, source in sources(SRC)
        if not in_egress(module)
        for name in imported_modules(source, module)
        if not name.startswith("ownvitals") and CLOSURE.is_module(name)
    }
    for cut in EDGE_CUTS:
        without = Closure(cuts=set(EDGE_CUTS) - {cut})
        assert any(without.capable(n) != CLOSURE.capable(n) for n in direct), f"{cut}: drop it"


def test_the_closure_counts_what_it_skipped_on_this_platform() -> None:
    _used()
    skipped = sorted(CLOSURE.unresolved)
    print(f"modules skipped on {sys.platform}: {len(skipped)} {skipped}")
    assert all("." not in name for name in skipped)


def test_the_object_names_come_from_the_stdlib() -> None:
    found = CLOSURE.find("asyncio.events")
    assert found is not None
    assert found.source is not None
    loop = next(
        c
        for c in ast.parse(found.source.read_bytes()).body
        if isinstance(c, ast.ClassDef) and c.name == "AbstractEventLoop"
    )
    methods = {f.name for f in loop.body if isinstance(f, ast.FunctionDef | ast.AsyncFunctionDef)}
    assert methods >= LOOP_METHODS
    refused = object_network_names()
    assert refused.isdisjoint(LOOP_METHODS)
    assert {
        "create_connection",
        "sock_connect",
        "getaddrinfo",
        "create_datagram_endpoint",
    } <= refused
    assert {"open_connection", "start_server", "open_unix_connection"} <= refused


# --- the review's cases -------------------------------------------------------------------------

REVIEWER_CASES = [
    ("import anyio\nawait anyio.TCPConnectable('h', 443).connect()", "anyio " + CAPABLE),
    ("from anyio import UNIXConnectable", "anyio " + CAPABLE),
    ("import anyio\nanyio.as_connectable(('h', 443), tls=True)", "anyio " + CAPABLE),
    ("from anyio.streams.tls import TLSConnectable", "anyio.streams.tls " + CAPABLE),
    ("from logging.handlers import SocketHandler", "logging.handlers " + CAPABLE),
    ("from logging.handlers import DatagramHandler", "logging.handlers " + CAPABLE),
    ("import logging.handlers\nlogging.handlers.SysLogHandler(address=('h', 514))", CAPABLE),
    ("from logging.handlers import HTTPHandler as H\nH('h', '/')", "logging.handlers " + CAPABLE),
    ("from logging import handlers\nhandlers.SMTPHandler('h', 'a', ['b'], 's')", CAPABLE),
    (
        "import logging.config\nlogging.config.dictConfig("
        "{'handlers': {'h': {'class': 'logging.handlers.HTTPHandler'}}})",
        "names logging.handlers.HTTPHandler",
    ),
    ("LOG = {'handlers': {'s': {'class': 'logging.handlers.SysLogHandler'}}}", "names logging"),
    ("CLASS = 'handlers.SMTPHandler'  # fileConfig", "names logging.handlers.SMTPHandler"),
    ("FACTORY = 'ext://socket.create_connection'", "names socket.create_connection"),
    ("from urllib.robotparser import RobotFileParser", "urllib.robotparser " + CAPABLE),
    ("import urllib.robotparser as r\nr.RobotFileParser('https://x').read()", CAPABLE),
]


@pytest.mark.parametrize(("source", "why"), REVIEWER_CASES)
def test_the_reviews_cases_are_refused_through_the_closure(source: str, why: str) -> None:
    assert any(why in found for _, found in scan_source(source, WEB)), scan_source(source, WEB)


def test_a_denied_entry_is_refused_inside_its_by_design_prefix() -> None:
    assert TABLE["starlette.testclient"].kind == "denied"
    assert scan_source("from starlette.responses import JSONResponse", WEB) == []
    for source in (
        "from starlette.testclient import TestClient",
        "import starlette.testclient",
        "import starlette\nstarlette.testclient.TestClient(app)",
        "x = 'starlette.testclient.TestClient'",
    ):
        assert any("starlette.testclient is denied" in w for _, w in scan_source(source, WEB))


def test_a_name_an_ownvitals_module_imported_is_judged_where_it_lands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A re-export through any ``ownvitals`` module is followed to the module it came from."""
    src = tmp_path / "src"
    plant(src, "ownvitals.web.reexport", "from asyncio import create_subprocess_exec\n")
    plant(
        src,
        "ownvitals.web.user",
        "from ownvitals.web import reexport\nreexport.create_subprocess_exec('x')\n",
    )
    plant(src, "ownvitals.web.user2", "from ownvitals.web.reexport import create_subprocess_exec\n")
    monkeypatch.setattr(egress_scan, "CLOSURE", Closure(cuts=EDGE_CUTS, src=src))
    found = {(m, why) for m, _, why in scan_tree(src)}
    landed = "create_subprocess_exec is not an allowed name of asyncio"
    assert (
        "ownvitals.web.user",
        f"uses ownvitals.web.reexport.create_subprocess_exec: {landed}",
    ) in found
    assert (
        "ownvitals.web.user2",
        f"imports ownvitals.web.reexport.create_subprocess_exec: {landed}",
    ) in found


def test_classifying_a_module_is_what_lets_it_through(monkeypatch: pytest.MonkeyPatch) -> None:
    """An allowed entry admits its names only: a new connection API, or a submodule, is refused
    by default."""
    monkeypatch.setitem(TABLE, "anyio", Entry("allowed", "test", frozenset({"sleep"})))
    assert scan_source("import anyio\nanyio.sleep(1)", WEB) == []
    assert scan_source("import anyio\nanyio.TCPConnectable('h', 443)", WEB) != []
    assert scan_source("from anyio import connect_tcp", WEB) != []
    assert scan_source("import anyio\nanyio.to_thread.run_sync(f)", WEB) != []


N7_REFERENCES = [
    "import asyncio\nconnect = asyncio.get_running_loop().create_connection",
    "import functools\nfunctools.partial(loop.open_connection, h)",
    "retry(loop.sock_connect)",
    "handlers = [loop.getaddrinfo]",
    "import asyncio\nopen_it = asyncio.open_connection",
    "from asyncio import start_server as serve",
    "import asyncio, functools\nfunctools.partial(asyncio.open_connection, 'h')",
]
N8_CHAINS = [
    "import asyncio\nasyncio.base_events.socket.socket()",
    "import asyncio\nasyncio.sslproto.ssl.create_default_context()",
    "import http.client\nhttp.client.socket",
    "import logging\nlogging.handlers.socket.socket()",
    "import email.message\nemail.message.utils.socket.getfqdn()",
    "import typing_extensions\ntyping_extensions._socket.socket()",
    "import importlib.metadata\nimportlib.metadata.email.utils",
]
N9_PARTIAL = [
    "import importlib\nimportlib.import_module(name)",
    "import importlib\nimportlib.import_module('.client', 'http')",
    "import importlib\nimportlib.import_module('.client', package='http')",
    "__import__('urllib', fromlist=['request'])",
    "__import__(name)",
    "import pkgutil\npkgutil.resolve_name('socket:create_connection')",
    "from ownvitals import egress\ngetattr(egress, 'install_worker_allowlist')()",
    "import asyncio\ngetattr(asyncio, 'open_connection')('h', 443)",
]
N11_ASSIGNMENTS = [
    "from ownvitals.egress import clients\nclients.worker_only = f",
    "from ownvitals.egress import core\ncore.refuse_in_worker = f",
    "import ownvitals.egress\nownvitals.egress.WORKER_ALLOWLIST = a",
    "from ownvitals.egress import core\ncore.WORKER_HOSTS |= {'evil.example'}",
    "from ownvitals import mail\nmail.SMTP_TOTAL_SECONDS = 1e9",
    "from ownvitals import mail\ndel mail.Mailer",
    "from ownvitals import egress\nsetattr(egress, 'refuse_in_worker', f)",
    "from ownvitals import egress\nsetattr(egress, name, f)",
    "from ownvitals.egress import code_exchange\ndelattr(code_exchange, 'CODE_EXCHANGE_FIELDS')",
]
UNRESOLVABLE = [
    "import anthropic",
    "import requests",
    "from httpx2 import Client",
    "import fastapi.x",
]


@pytest.mark.parametrize(
    "source", N7_REFERENCES + N8_CHAINS + N9_PARTIAL + N11_ASSIGNMENTS + UNRESOLVABLE
)
def test_the_guard_refuses(source: str) -> None:
    assert scan_source(source, WEB) != []


def test_an_unresolvable_import_says_so_and_is_allowed_for_type_checking() -> None:
    assert scan_source("import anthropic", WEB) == [
        (1, "imports anthropic: not in the locked environment")
    ]
    typed = "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    import anthropic\n"
    assert scan_source(typed, WEB) == []


STRINGS_ALLOWED = [
    "x = 'uvicorn.error'",
    "x = 'sqlalchemy.engine'",
    "x = 'wbsapi.withings.net'",
    "x = 'ownvitals.egress'",
    "x = 'json.loads'",
    "x = 'e.g'",
    "x = 'application/json'",
]


@pytest.mark.parametrize("source", STRINGS_ALLOWED)
def test_a_string_that_names_nothing_classified_out_is_allowed(source: str) -> None:
    assert scan_source(source, WEB) == []


def test_mail_may_assign_its_own_attributes() -> None:
    assert scan_source("import smtplib\nSMTP_TOTAL_SECONDS = 20.0\nx.y = 1", "ownvitals.mail") == []
