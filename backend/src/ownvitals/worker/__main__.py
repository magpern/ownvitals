"""``python -m ownvitals.worker``: run the worker until SIGTERM or SIGINT (ADR 0005 section 5).

It refuses to start, with exit code 2 and no override, without the worker allowlist (ADR 0003
section 6, ADR 0009), on a privileged database role (ADR 0002 section 5), on a drifted queue
(ADR 0005 section 3), or with a missing, malformed or swapped KEK (ADR 0004 section 3). A
database that is down at startup is checked on its first connection instead, and nothing is
claimed before the allowlist is installed and the role, queue and key ring checks have passed.

First thing it installs the egress hook (``install_egress_hook("worker")``, ADR 0009) and the
worker allowlist (``install_worker_allowlist``, the only non-test module besides the probe that
may call it). The loop refuses to run unless it is the standard library's selector loop, the
only one whose sockets the hook sees.

It runs the Withings kinds (``ownvitals.worker.kinds.worker_registry``), re-checks a job's
webhook-secret target (``WORKER_TARGETS``) and schedules the poll and the reconciliation
(``SCHEDULES``). Beside the loop it serves ``/metrics`` on ``metrics_bind`` when that is set and
refreshes the database-derived gauges (``ownvitals.worker.metrics``, #167); a bind address that
is not loopback or private refuses the start like the checks above.
"""

import asyncio
import logging
import signal
import sys

from ownvitals.auth.owners import ACCOUNTS
from ownvitals.config import Settings
from ownvitals.crypto.keks import KeyRingError, load_keyring
from ownvitals.db import (
    DatabaseRoleError,
    create_session_factory,
    event_loop_factory,
)
from ownvitals.egress import (
    install_egress_hook,
    install_worker_allowlist,
    require_standard_event_loop,
    worker_allowlist_installed,
)
from ownvitals.log import configure_logging
from ownvitals.metrics_server import BindRefused
from ownvitals.worker.kinds import SCHEDULES, WORKER_TARGETS, worker_registry
from ownvitals.worker.loop import create_worker_engine, run_worker, verify_startup
from ownvitals.worker.metrics import metrics_running

logger = logging.getLogger("ownvitals.worker")


class WorkerStartupError(RuntimeError):
    """The worker allowlist is not installed. Never contains credentials."""


def _install_signal_handlers(loop: asyncio.AbstractEventLoop, stop: asyncio.Event) -> None:
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # Windows event loops
            signal.signal(sig, lambda *_: loop.call_soon_threadsafe(stop.set))


async def _run(settings: Settings) -> None:
    require_standard_event_loop()  # the egress hook sees no other loop's sockets
    if not worker_allowlist_installed():
        raise WorkerStartupError("the worker allowlist is not installed")
    engine = create_worker_engine(settings)
    try:
        await verify_startup(engine)  # refuses a privileged role or a drifted queue
        # Kept, on this engine: a missing or malformed KEK raises here; an unreachable
        # database leaves the ring unverified. The loop claims and reaps nothing until
        # that ring's check has passed (a swapped key then stops the worker).
        factory = create_session_factory(engine)
        ring = await load_keyring(factory, settings, role="worker")
        stop = asyncio.Event()
        # Before "worker started" is logged, so a signal after that line is always handled.
        _install_signal_handlers(asyncio.get_running_loop(), stop)
        async with metrics_running(settings, factory, stop):
            await run_worker(
                settings,
                stop,
                registry=worker_registry(),
                accounts=ACCOUNTS,
                engine=engine,
                keyring=ring,
                targets=WORKER_TARGETS,
                schedules=SCHEDULES,
            )
    finally:
        await engine.dispose()


def main() -> int:
    settings = Settings()
    configure_logging(settings.log_level)
    # Both first, before ``_run`` makes the engine or runs the role, queue and key ring checks:
    # nothing in this process connects anywhere before them (ADR 0009). ``_run`` refuses to go
    # on if the allowlist is missing.
    install_egress_hook("worker", settings)
    install_worker_allowlist(ai_enabled=settings.ai_insights)
    try:
        asyncio.run(_run(settings), loop_factory=event_loop_factory())
    except (DatabaseRoleError, KeyRingError, WorkerStartupError, BindRefused) as refused:
        # The message names roles and objects only, never a credential.
        logger.error("worker refused to run", extra={"reason": str(refused)})
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
