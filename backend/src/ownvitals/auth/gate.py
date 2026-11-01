"""The second-factor gate (ownvitals ADR 0003 section 2).

A session that has not passed its second factor reaches only the routes below; every other
request that carries one is answered 403 ``two_factor_required`` by the middleware, before any
route runs, so a new route can't skip the gate. The dependencies refuse it a ``UserContext`` as
well (``deps.require_user``): two independent checks.

Anonymous requests and full sessions are not affected. Paths outside ``/api`` and ``/oauth``
are the single-page app's files (``GET`` only), which hold no data.
"""

from typing import Literal

from ownvitals.auth.sessions import SessionInfo

Need = Literal["any", "enrol", "verify"]

# (method, path) -> which sessions without a second factor may use it.
SECOND_FACTOR_ROUTES: dict[tuple[str, str], Need] = {
    ("GET", "/api/auth/session"): "any",  # says what is missing, and the CSRF token
    ("POST", "/api/auth/logout"): "any",
    ("GET", "/api/health"): "any",
    ("GET", "/api/ready"): "any",
    ("POST", "/api/auth/mfa/verify"): "verify",  # a confirmed TOTP: the code of this sign-in
    ("POST", "/api/auth/mfa/totp/start"): "enrol",  # no TOTP yet: the first enrolment
    ("POST", "/api/auth/mfa/totp/confirm"): "enrol",
}

GATED_PREFIXES = ("/api/", "/oauth/")
REASON = "two_factor_required"


def gated(method: str, path: str) -> bool:
    """Whether the gate looks at this request at all (``/api``, ``/oauth`` and any write)."""
    if path == "/api" or path.startswith(GATED_PREFIXES):
        return True
    return method not in ("GET", "HEAD")  # the SPA's files are read-only


def refusal(method: str, path: str, info: SessionInfo | None) -> str | None:
    """``two_factor_required`` when a session without its second factor may not make this
    request; ``None`` otherwise."""
    if info is None or info.full or not gated(method, path):
        return None
    need = SECOND_FACTOR_ROUTES.get((method, path.rstrip("/") or "/"))
    if need == "any" or (need is not None and need == info.second_factor):
        return None
    return REASON
