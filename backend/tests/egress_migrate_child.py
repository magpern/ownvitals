"""A fresh interpreter for ``test_egress_migrate.py`` (#284): runs alembic's command line (the
``alembic`` console script's ``main``) with the arguments it is given, as compose's ``migrate``
does, then prints one JSON line: the egress hook ``env.py`` installed and what it refuses.

Nothing here reaches the network: the refused lookup is refused before it resolves, the refused
connect is refused before the socket connects, the refused process before it forks. The hosts in
the environment are synthetic.
"""

import json
import socket
import subprocess
import sys
from collections.abc import Callable
from typing import Any

from alembic.config import main as alembic_main

from ownvitals.egress import EgressRefused, installed_kind
from ownvitals.egress.hook import resolvable_hosts


def refused(call: Callable[[], Any]) -> str | None:
    """The audit event the hook refused ``call`` on, else ``None``."""
    try:
        call()
    except EgressRefused as exc:
        return exc.event
    return None


def connect_outside_scope() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.connect(("127.0.0.1", 9))


def start_process() -> None:
    subprocess.run(["true"], check=False)  # noqa: S607 - refused before it starts


def main() -> None:
    alembic_main(argv=sys.argv[1:], prog="alembic")
    report: dict[str, Any] = {"kind": installed_kind(), "hosts": sorted(resolvable_hosts())}
    if report["kind"] is not None:  # without a hook the lookup below would resolve for real
        report["lookup"] = refused(lambda: socket.getaddrinfo("example.invalid", 443))
        report["connect"] = refused(connect_outside_scope)
        report["process"] = refused(start_process)
    print(json.dumps(report))


if __name__ == "__main__":
    main()
