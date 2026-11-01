"""The outbound lists, the refusal and the process roles (PLAN §2, roadmap Amendment 1).

Every list is code: nothing in the environment widens one. ``Allowlist`` admits ``https`` to an
exact, lower-case host on port 443 without userinfo, nothing else.

**Process roles.** ``install_worker_allowlist`` (``ownvitals.egress.opener``) marks this process
as the worker and records its list (``worker_allowlist``): Withings' two hosts, and
``api.anthropic.com`` only when AI is enabled. From then on:

- a **worker-only** client (Withings API, model) is built and sends only while the worker list is
  installed, and only to hosts on it (``worker_only``); in the web process it is refused;
- a **web-only** call site (Google sign-in, the Withings authorization-code exchange, account
  mail) calls ``refuse_in_worker`` first, so it cannot reach the network from the worker even if
  worker code ends up calling it.

A refused request raises ``OutboundBlocked`` (a job then fails ``outbound_blocked``, ADR 0005)
and logs the host only, never the URL, its path or query, a header or a body.
"""

import logging
import math
import ssl
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Final
from urllib.parse import urlsplit

logger = logging.getLogger("ownvitals.egress")

HTTPS: Final = "https"
HTTPS_PORT: Final = 443
_LOGGED_HOST_MAX: Final = 253  # the longest DNS name

# Withings' public API (D1) and its account host (PLAN §2). The token endpoint is on the API host
# (withings-findings §2); the authorize page on the account host is opened by the browser.
WITHINGS_API_HOST: Final = "wbsapi.withings.net"
WITHINGS_ACCOUNT_HOST: Final = "account.withings.com"
WITHINGS_HOSTS: Final = frozenset({WITHINGS_API_HOST, WITHINGS_ACCOUNT_HOST})
WITHINGS_TOKEN_PATH: Final = "/v2/oauth2"  # noqa: S105 - a path, not a secret
WITHINGS_TOKEN_URL: Final = f"{HTTPS}://{WITHINGS_API_HOST}{WITHINGS_TOKEN_PATH}"
# The model provider (M7), on the worker's list only when AI is enabled.
MODEL_HOST: Final = "api.anthropic.com"
# Google sign-in (ADR 0003 §5), from the web process: the token endpoint and the JWKS. The
# authorize page (``accounts.google.com``) is the browser's; the server never connects to it.
GOOGLE_TOKEN_HOST: Final = "oauth2.googleapis.com"  # noqa: S105 - a host name
GOOGLE_JWKS_HOST: Final = "www.googleapis.com"
GOOGLE_SIGNIN_HOSTS: Final = frozenset({GOOGLE_TOKEN_HOST, GOOGLE_JWKS_HOST})
# The worker's hosts without the model host (``worker_hosts`` adds it when AI is enabled).
WORKER_HOSTS: Final = WITHINGS_HOSTS


class OutboundBlocked(Exception):
    """A request the allowlist refused. ``args == ("outbound request blocked",)``; ``host`` is
    the refused host (lower case, printable, at most 253 characters), never the URL.

    ``after_request`` is True when the refusal came on a redirect hop: the first request had
    already been sent, so whatever it asked the provider to do may have happened. On the urllib
    path it is False only for a refusal before anything left the process. On the httpx path it is
    set only for a refused redirect answer; a same-host hop to ``http`` or another port is
    refused by ``Allowlist.check`` with False although the first request was sent, so nothing
    made over httpx may rely on this flag to prove that nothing was sent."""

    def __init__(self, host: str, *, after_request: bool = False) -> None:
        super().__init__("outbound request blocked")
        self.host = host
        self.after_request = after_request


def _printable_host(host: str | None) -> str:
    if not host:
        return ""
    text = "".join(c if c.isprintable() and not c.isspace() else "?" for c in host)
    return text.lower()[:_LOGGED_HOST_MAX]


def blocked(host: str | None, *, after_request: bool = False) -> OutboundBlocked:
    """Log the refusal (host only) and return the exception to raise."""
    shown = _printable_host(host)
    logger.warning("outbound request blocked", extra={"host": shown})
    return OutboundBlocked(shown, after_request=after_request)


def tls_client_context(*, cafile: str | None = None) -> ssl.SSLContext:
    """A verifying TLS client context, as ``ssl.create_default_context()`` builds it on Python
    3.12 (``PROTOCOL_TLS_CLIENT``: hostname check, ``CERT_REQUIRED``; ``cafile`` or the system
    trust store), but without the key log. ``create_default_context`` sets ``keylog_filename``
    from ``SSLKEYLOGFILE``, which then receives every session's secrets, enough to decrypt a
    capture of the traffic (tokens, the client secret, mail credentials). This one never opens
    such a file."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    if cafile is None:
        context.load_default_certs(ssl.Purpose.SERVER_AUTH)
    else:
        context.load_verify_locations(cafile=cafile)
    return context


@dataclass(frozen=True, slots=True)
class Allowlist:
    """``https`` to one of ``hosts`` (exact, lower case) on port 443, nothing else."""

    hosts: frozenset[str]

    def __post_init__(self) -> None:
        if any(h != h.lower() or not h or h.endswith(".") for h in self.hosts):
            raise ValueError("allowlisted hosts are lower case DNS names without a trailing dot")

    def check(self, scheme: str, host: str | None, port: int | None, userinfo: bool) -> str:
        """The host (lower case) if the request may go out; else log, raise ``OutboundBlocked``."""
        allowed = (
            scheme.lower() == HTTPS
            and host is not None
            and host.lower() in self.hosts
            and port in (None, HTTPS_PORT)
            and not userinfo
        )
        if not allowed:
            raise blocked(host)
        assert host is not None  # noqa: S101 - narrowed by ``allowed``
        return host.lower()

    def check_url(self, url: str) -> str:
        """``check`` for a URL string; one that does not parse is refused."""
        parts = None
        port: int | None = None
        try:
            parts = urlsplit(url)
            port = parts.port
        except ValueError:
            parts = None
        if parts is None:
            # Outside the except block: no __context__ (the ValueError's text holds URL parts).
            raise blocked(None)
        has_userinfo = parts.username is not None or parts.password is not None
        return self.check(parts.scheme, parts.hostname, port, has_userinfo)


WORKER_ALLOWLIST: Final = Allowlist(WORKER_HOSTS)
WITHINGS_ALLOWLIST: Final = Allowlist(WITHINGS_HOSTS)
MODEL_ALLOWLIST: Final = Allowlist(frozenset({MODEL_HOST}))
GOOGLE_SIGNIN_ALLOWLIST: Final = Allowlist(GOOGLE_SIGNIN_HOSTS)
# The web process's one Withings request (roadmap Amendment 1); the transport narrows it further.
CODE_EXCHANGE_ALLOWLIST: Final = Allowlist(frozenset({WITHINGS_API_HOST}))


def worker_hosts(*, ai_enabled: bool) -> frozenset[str]:
    """The worker's hosts: Withings, and the model host only when ``ai_enabled`` (the
    ``ai_insights`` setting). Anything but a real bool is refused, never read as truthy."""
    if not isinstance(ai_enabled, bool):
        raise TypeError("ai_enabled is a bool")
    return WORKER_HOSTS | {MODEL_HOST} if ai_enabled else WORKER_HOSTS


def worker_allowlist(*, ai_enabled: bool = False) -> Allowlist:
    return Allowlist(worker_hosts(ai_enabled=ai_enabled))


# --- process roles ---------------------------------------------------------------------------

# Set by ``install_worker_allowlist``: this process is the worker, and this is its list.
_worker_list: Allowlist | None = None


def _install_worker_list(allowlist: Allowlist) -> None:
    global _worker_list
    _worker_list = allowlist


def worker_allowlist_installed() -> bool:
    return _worker_list is not None


def installed_worker_hosts() -> frozenset[str]:
    """The installed worker list's hosts; empty outside the worker."""
    return frozenset() if _worker_list is None else _worker_list.hosts


def refuse_in_worker(host: str | None) -> None:
    """For the web-only outbound call sites: raise ``OutboundBlocked`` inside the worker."""
    if _worker_list is not None:
        raise blocked(host)


def worker_only(hosts: frozenset[str], shown: str) -> Allowlist:
    """For a worker-only client: the part of ``hosts`` on the installed worker list. Outside the
    worker, or when none of ``hosts`` is listed (the model host with AI off), raise
    ``OutboundBlocked`` naming ``shown``."""
    usable = hosts & installed_worker_hosts()
    if not usable:
        raise blocked(shown)
    return Allowlist(usable)


def require_worker_host(host: str) -> None:
    """Per request of a worker-only client: the worker list is installed and holds ``host``."""
    if host.lower() not in installed_worker_hosts():
        raise blocked(host)


# --- the total deadline ----------------------------------------------------------------------


class RequestDeadline(TimeoutError):
    """A request outlived its total deadline (or would have started after it). No text."""

    def __init__(self) -> None:
        super().__init__("request deadline passed")


def check_total_seconds(seconds: object) -> float:
    """A total deadline is a positive, finite number of seconds (``inf`` would never end and
    ``nan`` compares false with everything)."""
    if (
        not isinstance(seconds, int | float)
        or isinstance(seconds, bool)
        or not math.isfinite(seconds)
        or not seconds > 0
    ):
        raise ValueError("a total deadline is a positive, finite number of seconds")
    return float(seconds)


# --- the per-job request budget --------------------------------------------------------------


class RequestBudgetExhausted(Exception):
    """A request to a budgeted host would have gone over its job's budget, so it was **never
    sent**. No text, no host, no count."""

    def __init__(self) -> None:
        super().__init__("request budget used up")


class RequestBudget:
    """How many requests to ``hosts`` one job may still make: ``limit`` in all, ``used`` of them
    already (across its stages and attempts; the job runner reads and stores ``used``). Every
    request and every redirect hop to one of the hosts takes one before it is sent; the one that
    would go over ``limit`` raises ``RequestBudgetExhausted`` instead. Thread-safe."""

    def __init__(self, hosts: frozenset[str], limit: int, used: int = 0) -> None:
        if limit < 0 or used < 0:
            raise ValueError("a request budget is not negative")
        self.hosts = frozenset(h.lower() for h in hosts)
        self.limit = limit
        self._used = used
        self._lock = threading.Lock()

    @property
    def used(self) -> int:
        with self._lock:
            return self._used

    def take(self, host: str) -> None:
        if host.lower() not in self.hosts:
            return
        with self._lock:
            if self._used >= self.limit:
                raise RequestBudgetExhausted
            self._used += 1


_request_budget: ContextVar[RequestBudget | None] = ContextVar("request_budget", default=None)


@contextmanager
def request_budget(budget: RequestBudget) -> Iterator[RequestBudget]:
    """Inside the block (and in threads started from it with the context, as ``asyncio.to_thread``
    does), every urllib and httpx request to the budget's hosts is counted."""
    token = _request_budget.set(budget)
    try:
        yield budget
    finally:
        _request_budget.reset(token)


def spend(host: str | None) -> None:
    """Take one request from the current budget, if any, before sending to ``host``."""
    budget = _request_budget.get()
    if budget is not None and host is not None:
        budget.take(host)
