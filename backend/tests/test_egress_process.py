"""ADR 0009 item 7: the routes that start a process without an audit event the egress hook
sees. ``_posixsubprocess.fork_exec`` raises none, and ``multiprocessing``'s spawn and forkserver
methods use it; ``_winapi.CreateProcess`` is the Windows primitive. The static guard refuses them
in ``src/`` under any name; the hook refuses the routes that raise audit events."""

from pathlib import Path

import pytest

from egress_classified import PROCESS_MODULES, PROCESS_NAMES
from egress_scan import scan_source, scan_tree

SRC = Path(__file__).resolve().parent.parent / "src"
WHY = "starts a process the egress hook cannot see"
MODULES = ["ownvitals.web.planted", "ownvitals.worker.loop", "ownvitals.mail", "ownvitals.cli"]

REFUSED = [
    "import _posixsubprocess\n_posixsubprocess.fork_exec",
    "from _posixsubprocess import fork_exec as f\nf()",
    "import importlib\nimportlib.import_module('_posixsubprocess')",
    "import _winapi\n_winapi.CreateProcess",
    "from _winapi import CreateProcess",
    "import multiprocessing",
    "from multiprocessing import get_context\nget_context('spawn')",
    "import multiprocessing.util as u\nu.spawnv_passfds",
    "from multiprocessing.context import SpawnProcess",
    "from concurrent.futures import ProcessPoolExecutor",
    "import concurrent.futures as cf\ncf.ProcessPoolExecutor()",
    "from concurrent import futures\nfutures.ProcessPoolExecutor(2)",
    "from concurrent.futures.process import ProcessPoolExecutor",
    "import concurrent.futures.process",
    # B1 of the FAIL on #123: a direct call through an alias is judged as an import.
    "from importlib import import_module as load\nload('_posixsubprocess')",
    "import importlib as il\nil.import_module('multiprocessing.pool')",
    "import builtins\nbuiltins.__import__('_posixsubprocess')",
    "import importlib\nimportlib.__import__('_winapi')",
]
# ... and a stored or ``getattr`` importer is refused (``test_egress_reflection.py``).
STORED = [
    "import importlib\ni = importlib.import_module\ni('_posixsubprocess')",
    "i = __import__\ni('_posixsubprocess')",
    "import importlib\ngetattr(importlib, 'import_module')('_posixsubprocess')",
    "import builtins\ngetattr(builtins, '__import__')('_posixsubprocess')",
]
NEAR_MISSES = [
    "from concurrent.futures import ThreadPoolExecutor\nThreadPoolExecutor()",
    "import concurrent.futures\nconcurrent.futures.ThreadPoolExecutor(2)",
    "from concurrent import futures\nfutures.Future()",
    "import concurrent.futures.thread",
    "posixsubprocess = 1\nmultiprocessing_count = 2",  # names that only look alike
]


@pytest.mark.parametrize("source", REFUSED)
def test_the_guard_refuses_a_process_route(source: str) -> None:
    for module in MODULES:
        found = scan_source(source, module)
        assert any(WHY in why for _, why in found), (module, found)


@pytest.mark.parametrize("source", STORED)
def test_a_stored_importer_is_refused(source: str) -> None:
    for module in MODULES:
        found = scan_source(source, module)
        assert any("an importer is only called directly" in why for _, why in found), found


@pytest.mark.parametrize("source", NEAR_MISSES)
def test_the_guard_allows_the_near_misses(source: str) -> None:
    assert scan_source(source, "ownvitals.web.planted") == []


def test_the_process_routes_are_these() -> None:
    assert set(PROCESS_MODULES) == {
        "_posixsubprocess",
        "_winapi",
        "multiprocessing",
        "concurrent.futures.process",
    }
    assert {"concurrent.futures.ProcessPoolExecutor"} == PROCESS_NAMES


def test_src_starts_no_process() -> None:
    assert [f for f in scan_tree(SRC) if WHY in f[2]] == []
