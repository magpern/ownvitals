"""``python -m ownvitals.worker``: run the worker until SIGTERM or SIGINT (ADR 0005 section 5).

It refuses to start, with exit code 2 and no override, on a privileged database role (ADR 0002
section 5) or a drifted queue (ADR 0005 section 3); a database that is down at startup is checked
on its first connection instead, and nothing is claimed before that check passed.

First thing it installs the egress hook (``install_egress_hook("worker")``, ADR 0009), and the
loop refuses to run unless it is the standard library's selector loop, the only one whose
sockets the hook sees. Two start-up steps join this module in #77: the worker's egress allowlist
(``ownvitals.egress.install_worker_allowlist``, #6; this is the only non-test module besides the
probe that may call it) and the KEK ring check (``ownvitals.crypto.keks.load_keyring``, #8). Until
then the worker makes no outbound call and reads no connection secret: #9 registers no kind.
"""

import asyncio
import logging
import signal
import sys

from ownvitals.auth.owners import ACCOUNTS
from ownvitals.config import Settings
from ownvitals.db import DatabaseRoleError, event_loop_factory
from ownvitals.egress import install_egress_hook, require_standard_event_loop
from ownvitals.log import configure_logging
from ownvitals.worker.loop import create_worker_engine, run_worker, verify_startup

logger = logging.getLogger("ownvitals.worker")


def _install_signal_handlers(loop: asyncio.AbstractEventLoop, stop: asyncio.Event) -> None:
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # Windows event loops
            signal.signal(sig, lambda *_: loop.call_soon_threadsafe(stop.set))


async def _run(settings: Settings) -> None:
    require_standard_event_loop()  # the egress hook sees no other loop's sockets
    engine = create_worker_engine(settings)
    try:
        await verify_startup(engine)  # refuses a privileged role or a drifted queue
    finally:
        await engine.dispose()
    stop = asyncio.Event()
    # Before "worker started" is logged, so a signal after that line is always handled.
    _install_signal_handlers(asyncio.get_running_loop(), stop)
    await run_worker(settings, stop, accounts=ACCOUNTS)  # ownvitals.auth's owner check (#10)


def main() -> int:
    settings = Settings()
    configure_logging(settings.log_level)
    install_egress_hook("worker", settings)  # before anything can connect (ADR 0009)
    try:
        asyncio.run(_run(settings), loop_factory=event_loop_factory())
    except DatabaseRoleError as refused:
        # The message names roles and objects only, never a credential.
        logger.error("worker refused to run", extra={"reason": str(refused)})
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
