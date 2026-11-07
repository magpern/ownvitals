"""The worker's process-wide urllib opener and the total deadline of one urllib request.

``install_worker_allowlist()`` installs, before anything else in the worker, an opener with an
empty ``ProxyHandler`` (no ``HTTPS_PROXY`` or ``NO_PROXY`` setting reroutes a call) and a guard
that refuses every request, redirect hops included, that is not ``https`` to a host on the
worker's list on port 443. It has no ``http``, ``ftp``, ``file`` or ``data`` handler, follows a
redirect only to the same host, and verifies TLS without a key log. ownvitals' own calls go
through the httpx clients (``ownvitals.egress.clients``); this opener catches any library that
reaches for ``urllib.request.urlopen`` in the worker.

Every request has a total deadline: the network handler is ``DeadlineHTTPSHandler``
(``ownvitals.egress.deadline``, re-exported here with the deadline helpers).
"""

import urllib.request
from typing import Any, Final
from urllib.parse import urlsplit

from ownvitals.egress.core import (
    HTTPS_PORT,
    WORKER_ALLOWLIST,
    Allowlist,
    OutboundBlocked,
    _install_worker_list,  # the package's own; the guard refuses private names outside it
    blocked,
    spend,
    tls_client_context,
    worker_allowlist,
)
from ownvitals.egress.deadline import (  # noqa: F401 (re-exported)
    REQUEST_FALLBACK_SECONDS,
    DeadlineHTTPSConnection,
    DeadlineHTTPSHandler,
    call_deadline,
    call_deadline_until,
    request_deadline,
)
from ownvitals.egress.hook import _connection_scope  # the package's own


def _check_urllib_request(allowlist: Allowlist, req: urllib.request.Request) -> None:
    try:
        host = allowlist.check_url(req.full_url)
        # What http.client will actually connect to (``Request.host``), never through a proxy.
        if req.has_proxy() or req.host.lower() not in (host, f"{host}:{HTTPS_PORT}"):
            raise blocked(req.host)
    except OutboundBlocked as refused:
        # urllib's redirect handler gives every hop after the first a ``redirect_dict``.
        refused.after_request = hasattr(req, "redirect_dict")
        raise


class _AllowlistGuard(urllib.request.BaseHandler):
    """``default_open`` runs before any protocol handler, for every request and redirect hop."""

    handler_order = 0

    def __init__(self, allowlist: Allowlist) -> None:
        self._allowlist = allowlist

    def default_open(self, req: urllib.request.Request) -> None:
        _check_urllib_request(self._allowlist, req)
        spend(urlsplit(req.full_url).hostname)  # counted once per request or hop, before sending
        return None  # go on to https_open


def _same_host(old: str, new: str) -> bool:
    a, b = urlsplit(old), urlsplit(new)
    return (a.hostname or "").lower() == (b.hostname or "").lower()


class _SameHostRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Follows a redirect only to the same host: urllib copies every header (an ``Authorization``
    too) onto the next hop, so a hop to another listed host would hand it a token meant for the
    first. The new hop still goes through the guard."""

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> urllib.request.Request | None:
        if not _same_host(req.full_url, newurl):
            raise blocked(
                urlsplit(newurl).hostname if "://" in newurl else None, after_request=True
            )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class _AllowlistHTTPSHandler(DeadlineHTTPSHandler):
    """The network handler; checks again right before connecting."""

    def __init__(self, allowlist: Allowlist) -> None:
        super().__init__(context=tls_client_context())
        self._allowlist = allowlist

    def https_open(self, req: urllib.request.Request) -> Any:
        _check_urllib_request(self._allowlist, req)
        with _connection_scope("urllib"):  # the audit hook lets this request connect (ADR 0009)
            return super().https_open(req)


def build_allowlist_opener(
    allowlist: Allowlist = WORKER_ALLOWLIST,
    *,
    https_handler: urllib.request.BaseHandler | None = None,
) -> urllib.request.OpenerDirector:
    """An opener that only does ``https`` to ``allowlist``.

    Assembled by hand rather than with ``build_opener``, which would add the default ``http``,
    ``ftp``, ``file`` and ``data`` handlers and a ``ProxyHandler`` reading the environment.
    ``https_handler`` replaces the network handler in tests (no network); the guard still runs.
    """
    opener = urllib.request.OpenerDirector()
    handlers: tuple[urllib.request.BaseHandler, ...] = (
        # Empty: no proxy from the environment. With no proxies it registers no ``<scheme>_open``,
        # so OpenerDirector drops it, which is the point (a test checks no handler proxies).
        urllib.request.ProxyHandler({}),
        _AllowlistGuard(allowlist),
        https_handler if https_handler is not None else _AllowlistHTTPSHandler(allowlist),
        urllib.request.UnknownHandler(),
        urllib.request.HTTPDefaultErrorHandler(),
        _SameHostRedirectHandler(),  # same host only; each hop goes through the guard again
        urllib.request.HTTPErrorProcessor(),  # 4xx and 5xx raise HTTPError
    )
    for handler in handlers:
        opener.add_handler(handler)
    return opener


# The web process's urllib list: empty. Web's own calls go through the httpx clients and the
# mail module; no library may reach for ``urlopen`` there.
WEB_URLLIB_ALLOWLIST: Final = Allowlist(frozenset())


def install_web_opener() -> urllib.request.OpenerDirector:
    """Install, in the web process, an opener that refuses every ``urllib.request.urlopen``
    (``OutboundBlocked`` before anything is sent: no host is on its list, and it has no
    ``http``, ``ftp``, ``file`` or ``data`` handler and no proxy). A third-party library calling
    ``urlopen`` in web is refused at run time, not only by the static guard. Called first thing
    by ``python -m ownvitals`` when it serves (#10); the worker installs its own list instead.
    It records no process role: the web-only clients keep working."""
    opener = build_allowlist_opener(WEB_URLLIB_ALLOWLIST)
    urllib.request.install_opener(opener)
    return opener


def install_worker_allowlist(*, ai_enabled: bool = False) -> urllib.request.OpenerDirector:
    """Make this process the worker: record its list (Withings; the model host only when
    ``ai_enabled``, the ``ai_insights`` setting) for the worker-only clients, and install its
    opener for every ``urllib.request.urlopen`` in the process. From now on the web-only call
    sites refuse (``refuse_in_worker``). Called first thing at worker start."""
    allowlist = worker_allowlist(ai_enabled=ai_enabled)
    opener = build_allowlist_opener(allowlist)
    urllib.request.install_opener(opener)
    _install_worker_list(allowlist)
    return opener
