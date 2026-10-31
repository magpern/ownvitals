"""The outbound allowlist (PLAN §2 "The worker owns every outbound call", roadmap Amendment 1).

This package is the only place in ``ownvitals`` that builds an HTTP client, a transport or a
``urllib`` opener. The guard in ``tests/egress_scan.py`` is closed-world: which modules can
reach the network is computed from the locked environment, and outside this package a module
may use one only as the reviewed table ``tests/egress_classified.py`` classifies it (inbound and
driver code by design, a few allowed names, ``smtplib``'s two clients only in ``ownvitals.mail``).
Nothing outside uses a private name of the package or of ``ownvitals.mail``, assigns to either,
or calls ``install_worker_allowlist`` (the worker's entry point and the probe CLI excepted).
The lists are code; nothing in the environment widens one. Callers catch the
package's own exceptions (``OutboundBlocked``, ``RequestDeadline``, ``RequestBudgetExhausted``);
a network library's exception class a caller needs is re-exported here, never imported from the
library (none is needed yet).

**What each process can reach** (``tests/test_egress_runtime.py`` enumerates it):

- **worker**: ``wbsapi.withings.net`` and ``account.withings.com`` (``withings_async_client`` and
  the process-wide urllib opener), and ``api.anthropic.com`` only when AI is enabled
  (``install_worker_allowlist(ai_enabled=...)``, ``model_async_client``);
- **web**: Google's sign-in hosts (``google_oauth_client``, ``google_async_client``), the one
  Withings authorization-code exchange (``exchange_withings_code``) and the configured
  SMTP server through ``ownvitals.mail``, the one module the guard lets open a connection of its
  own (its host is a setting, so it is not a list entry). Each refuses in the worker
  (``refuse_in_worker``).

**At run time** (ADR 0009) every process installs the egress hook first thing
(``install_egress_hook``): a PEP 578 audit hook that refuses lookups of hosts outside the
process's set, every reverse lookup, and any connection outside a scope that only this package's
transports (after their allowlist check) and ``ownvitals.mail`` (``smtp_session``) open. It is
the control for what the static guard cannot see, such as a library's lazy URL fetcher; the
event loop must be the standard library's selector loop (``require_standard_event_loop``).

Modules: ``core`` (lists, refusal, process roles, request budget, TLS context), ``opener`` (the
worker's urllib opener and the total deadline of a urllib request), ``clients`` (the httpx
transport and clients), ``code_exchange`` (the web process's one Withings call), ``hook`` (the
audit hook).

The hook sees only Python's ``socket`` module (not C extensions such as libpq, nor ``ctypes``);
a network-level egress rule stays the deployment's.
"""

from ownvitals.egress.clients import (
    GOOGLE_TOTAL_SECONDS,
    MODEL_TIMEOUT_SECONDS,
    MODEL_TOTAL_SECONDS,
    WITHINGS_TOTAL_SECONDS,
    AllowlistTransport,
    google_async_client,
    google_oauth_client,
    model_async_client,
    withings_async_client,
)
from ownvitals.egress.code_exchange import (
    CODE_EXCHANGE_FIELDS,
    CODE_EXCHANGE_HEADERS,
    CODE_EXCHANGE_TOTAL_SECONDS,
    encode_code_exchange,
    exchange_withings_code,
    is_code_exchange_form,
)
from ownvitals.egress.core import (
    CODE_EXCHANGE_ALLOWLIST,
    GOOGLE_JWKS_HOST,
    GOOGLE_SIGNIN_ALLOWLIST,
    GOOGLE_SIGNIN_HOSTS,
    GOOGLE_TOKEN_HOST,
    HTTPS_PORT,
    MODEL_ALLOWLIST,
    MODEL_HOST,
    WITHINGS_ACCOUNT_HOST,
    WITHINGS_ALLOWLIST,
    WITHINGS_API_HOST,
    WITHINGS_HOSTS,
    WITHINGS_TOKEN_PATH,
    WITHINGS_TOKEN_URL,
    WORKER_ALLOWLIST,
    WORKER_HOSTS,
    Allowlist,
    OutboundBlocked,
    RequestBudget,
    RequestBudgetExhausted,
    RequestDeadline,
    blocked,
    check_total_seconds,
    installed_worker_hosts,
    refuse_in_worker,
    request_budget,
    tls_client_context,
    worker_allowlist,
    worker_allowlist_installed,
    worker_hosts,
)
from ownvitals.egress.hook import (
    EgressRefused,
    install_egress_hook,
    installed_kind,
    require_standard_event_loop,
    smtp_session,
)
from ownvitals.egress.opener import (
    REQUEST_FALLBACK_SECONDS,
    DeadlineHTTPSConnection,
    DeadlineHTTPSHandler,
    build_allowlist_opener,
    call_deadline,
    call_deadline_until,
    install_worker_allowlist,
    request_deadline,
)

__all__ = [
    "CODE_EXCHANGE_ALLOWLIST",
    "CODE_EXCHANGE_FIELDS",
    "CODE_EXCHANGE_HEADERS",
    "CODE_EXCHANGE_TOTAL_SECONDS",
    "GOOGLE_JWKS_HOST",
    "GOOGLE_SIGNIN_ALLOWLIST",
    "GOOGLE_SIGNIN_HOSTS",
    "GOOGLE_TOKEN_HOST",
    "GOOGLE_TOTAL_SECONDS",
    "HTTPS_PORT",
    "MODEL_ALLOWLIST",
    "MODEL_HOST",
    "MODEL_TIMEOUT_SECONDS",
    "MODEL_TOTAL_SECONDS",
    "REQUEST_FALLBACK_SECONDS",
    "WITHINGS_ACCOUNT_HOST",
    "WITHINGS_ALLOWLIST",
    "WITHINGS_API_HOST",
    "WITHINGS_HOSTS",
    "WITHINGS_TOKEN_PATH",
    "WITHINGS_TOKEN_URL",
    "WITHINGS_TOTAL_SECONDS",
    "WORKER_ALLOWLIST",
    "WORKER_HOSTS",
    "Allowlist",
    "AllowlistTransport",
    "DeadlineHTTPSConnection",
    "DeadlineHTTPSHandler",
    "EgressRefused",
    "OutboundBlocked",
    "RequestBudget",
    "RequestBudgetExhausted",
    "RequestDeadline",
    "blocked",
    "build_allowlist_opener",
    "call_deadline",
    "call_deadline_until",
    "check_total_seconds",
    "encode_code_exchange",
    "exchange_withings_code",
    "google_async_client",
    "google_oauth_client",
    "install_egress_hook",
    "install_worker_allowlist",
    "installed_kind",
    "installed_worker_hosts",
    "is_code_exchange_form",
    "model_async_client",
    "refuse_in_worker",
    "request_budget",
    "request_deadline",
    "require_standard_event_loop",
    "smtp_session",
    "tls_client_context",
    "withings_async_client",
    "worker_allowlist",
    "worker_allowlist_installed",
    "worker_hosts",
]
