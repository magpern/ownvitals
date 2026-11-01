"""``python -m ownvitals``: run the web app with uvicorn and JSON logging, or an operator
subcommand (see ``ownvitals.cli``)."""

import argparse
import sys
from collections.abc import Sequence
from types import FrameType

import uvicorn
from fastapi import FastAPI

from ownvitals.cli import add_subcommands, run_command
from ownvitals.config import Settings
from ownvitals.egress import install_web_opener
from ownvitals.log import configure_logging
from ownvitals.web import create_app

# The standard library's selector loop, never uvloop ("auto" would pick it when installed): only
# its sockets raise the audit events the egress hook checks (ADR 0009). On Windows uvicorn's
# "asyncio" means the proactor loop, which neither psycopg's async mode nor the hook can use.
UVICORN_LOOP = "asyncio:SelectorEventLoop" if sys.platform == "win32" else "asyncio"
# After SIGTERM, requests get this long to finish before uvicorn cancels them and runs the
# lifespan shutdown; inside Docker's default 10 s stop timeout for web.
GRACEFUL_SHUTDOWN_SECONDS = 5


class Server(uvicorn.Server):
    """uvicorn's server that also tells the open job event streams to end when shutdown begins
    (``app.state.event_hub.close_threadsafe()``, set by the job queue's lifespan), so they do not
    hold the graceful shutdown. Without a hub it is plain uvicorn."""

    def __init__(self, config: uvicorn.Config, app: FastAPI) -> None:
        super().__init__(config)
        self.app = app

    def handle_exit(self, sig: int, frame: FrameType | None) -> None:
        hub = getattr(self.app.state, "event_hub", None)
        if hub is not None:
            hub.close_threadsafe()
        super().handle_exit(sig, frame)


def server_config(app: FastAPI, host: str, port: int) -> uvicorn.Config:
    # log_config=None: uvicorn's loggers propagate to the root JSON handler. Its access log is
    # off (it records full request paths, and a webhook path carries a secret) and so is its
    # proxy header handling (we read X-Forwarded-For ourselves, from trusted proxies only).
    return uvicorn.Config(
        app,
        host=host,
        port=port,
        log_config=None,
        loop=UVICORN_LOOP,
        access_log=False,
        proxy_headers=False,
        timeout_graceful_shutdown=GRACEFUL_SHUTDOWN_SECONDS,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m ownvitals", description="Run the ownvitals API, or an operator command."
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    add_subcommands(parser)
    args = parser.parse_args(argv)

    settings = Settings()
    configure_logging(settings.log_level)
    if args.command is not None:
        return run_command(args, settings)
    install_web_opener()  # no urllib request leaves web (#10); its calls go through egress
    app = create_app(settings)
    Server(server_config(app, args.host, args.port), app).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
