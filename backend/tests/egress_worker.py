"""The worker's side of the outbound guard: which packages run in the worker, what the worker
process imports at start, and a fresh-interpreter check that it never loads the web-only mail
module or a client library it does not use (``test_egress_guard.py``)."""

from egress_closure import under

MAIL = "ownvitals.mail"

# Packages that run in the worker: they may never import the web-only mail module.
WORKER_PACKAGES = (
    "ownvitals.worker",
    "ownvitals.jobs",
    "ownvitals.withings",
    "ownvitals.ingest",
    "ownvitals.analysis",
    "ownvitals.ecg",
    "ownvitals.scores",
    "ownvitals.findings",
    "ownvitals.reports",
    "ownvitals.insights",
)
# What the worker process imports at start. Add each new worker entry point here (the worker
# loop, the job runner, the Withings client ...); the reachability test imports them all in a
# fresh interpreter and fails if mail, or a client library they use, is loaded.
WORKER_ENTRYPOINTS = (
    "ownvitals.egress",
    "ownvitals.config",
    "ownvitals.log",
    "ownvitals.db",
    "ownvitals.jobs",
    "ownvitals.withings.client",
)
# What the worker must not load (the egress package itself runs on httpx and urllib).
_REACHABILITY_WATCHED = (
    MAIL,
    "authlib",
    "smtplib",
    "anthropic",
    "requests",
    "aiohttp",
    "urllib3",
    "pycurl",
    "websocket",
    "websockets",
    "httpx2",
    "openai",
    "ftplib",
    "telnetlib",
    "imaplib",
    "poplib",
    "nntplib",
    "smtpd",
    "xmlrpc.client",
    "logging.handlers",
    "urllib.robotparser",
)


def loaded_watched_modules(entry_modules: tuple[str, ...]) -> list[str]:
    """Import ``entry_modules`` in a fresh interpreter; the web-only and client-library modules
    (``_REACHABILITY_WATCHED``) that end up in ``sys.modules``.

    This sees import-time loads only. A deferred import inside a function is not seen: only the
    runtime refusal covers it (``test_egress_runtime.py``). Any new web-only outbound call site
    must therefore call ``ownvitals.egress.refuse_in_worker`` itself, and get a runtime test."""
    import json
    import subprocess
    import sys

    probe = (
        "import importlib, json, sys\n"
        f"for name in {list(entry_modules)!r}:\n"
        "    importlib.import_module(name)\n"
        "print(json.dumps(sorted(sys.modules)))\n"
    )
    result = subprocess.run(  # noqa: S603 - our own interpreter, a fixed probe
        [sys.executable, "-c", probe], capture_output=True, text=True, timeout=60, check=True
    )
    loaded = set(json.loads(result.stdout.splitlines()[-1]))
    return sorted(m for m in loaded if under(m, _REACHABILITY_WATCHED))
