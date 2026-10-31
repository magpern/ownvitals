"""The outbound allowlist (PLAN §2 "The worker owns every outbound call", roadmap Amendment 1).

This package is the only place in ``ownvitals`` that builds an HTTP client, a transport or a
``urllib`` opener (the guard in ``tests/egress_scan.py`` fails on any other, and on any use of a
private name of the package from outside it). The lists are code; nothing in the environment
widens one.

**What each process can reach** (``tests/test_egress_runtime.py`` enumerates it):

- **worker**: ``wbsapi.withings.net`` and ``account.withings.com`` (``withings_async_client`` and
  the process-wide urllib opener), and ``api.anthropic.com`` only when AI is enabled
  (``install_worker_allowlist(ai_enabled=...)``, ``model_async_client``);
- **web**: Google's sign-in hosts (``google_signin_transport``, ``google_async_client``), the one
  Withings authorization-code exchange (``withings_code_exchange_client``) and the configured
  SMTP server through ``ownvitals.mail``, the one module the guard lets open a connection of its
  own (its host is a setting, so it is not a list entry). Each refuses in the worker
  (``refuse_in_worker``).

Modules: ``core`` (lists, refusal, process roles, request budget, TLS context), ``opener`` (the
worker's urllib opener and the total deadline of a urllib request), ``clients`` (the httpx
transport and clients), ``code_exchange`` (the web process's one Withings call).

The checks run inside the process; code in it could still open a socket around them (the guard
makes that visible in review; a network-level egress rule is the deployment's).
"""

from ownvitals.egress.clients import (
    GOOGLE_TOTAL_SECONDS,
    MODEL_TIMEOUT_SECONDS,
    WITHINGS_TOTAL_SECONDS,
    AllowlistTransport,
    google_async_client,
    google_signin_transport,
    model_async_client,
    withings_async_client,
)
from ownvitals.egress.code_exchange import (
    CODE_EXCHANGE_FIELDS,
    CODE_EXCHANGE_TOTAL_SECONDS,
    is_code_exchange_form,
    withings_code_exchange_client,
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
    installed_worker_hosts,
    refuse_in_worker,
    request_budget,
    tls_client_context,
    worker_allowlist,
    worker_allowlist_installed,
    worker_hosts,
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
    "OutboundBlocked",
    "RequestBudget",
    "RequestBudgetExhausted",
    "RequestDeadline",
    "blocked",
    "build_allowlist_opener",
    "call_deadline",
    "call_deadline_until",
    "google_async_client",
    "google_signin_transport",
    "install_worker_allowlist",
    "installed_worker_hosts",
    "is_code_exchange_form",
    "model_async_client",
    "refuse_in_worker",
    "request_budget",
    "request_deadline",
    "tls_client_context",
    "withings_async_client",
    "withings_code_exchange_client",
    "worker_allowlist",
    "worker_allowlist_installed",
    "worker_hosts",
]
