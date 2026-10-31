"""Rules the Withings fake enforces so that a client which regressed fails against it.

Synthetic only, standard library only. ``withingscore`` and ``withingsnotify`` call these; every
refusal is an API error (HTTP 200 with a non-zero ``status``), like Withings' own. Where the
public documents (``docs/withings-findings.md`` section 2, PLAN 4.2) do not state a rule or its
status code, the stricter reading was taken and the line below says so.

Request shape. PLAN 4.2: calls are ``POST`` with a form body and a Bearer token. The fake reads
only the form body of an API call and refuses (a) any method but ``POST`` and (b) the secrets
``SECRET_PARAMS`` in the URL's query string, even when the body repeats them: a query string ends
up in access logs and proxies. The authorize page is the exception (a browser ``GET``, query only).
Assumed: status 2554 ("not implemented", the answer to a wrong webservice) for (a) and 503
("invalid params") for (b); that Withings ignores other query parameters; that the check applies to
every API endpoint (token, revoke, getnonce, getdemoaccess, notify and the data endpoints).

Callback URL (findings section 2: HTTPS on port 443 with a domain name, no IP or localhost, at
most 255 characters; an application whose callback breaks the rule falls into restricted mode, 10
linked users). Modelled as a refusal (status 293, like a bad URL), the stricter reading of
"restricted mode", so that the subscription-URL builder of #24 and #25 has something to fail
against. Assumed: "domain name" means at least two non-empty labels whose last label is
alphabetic (a TLD, not a number such as ``127.1`` or ``2130706433``); a trailing dot is refused;
``.localhost`` names are localhost; the scheme and host are compared case-insensitively; userinfo
and fragments are not judged; reserved names such as ``*.example.test`` are domain names.

Rate limit (findings section 2: 120 requests per minute per application, 601 when exceeded).
Modelled as a token bucket per application on the fake clock: capacity 120, refilled at 2 per
second, every API request that names its application (a ``client_id``, or a known Bearer token)
takes one. Assumed: Withings' exact algorithm is not published, so a burst of 120 is allowed;
requests that name no known application are not limited; the 601 carries no ``wait_seconds`` (the
stricter case: a client must back off without being told how long).
"""

from __future__ import annotations

import ipaddress
from urllib.parse import urlsplit

from withingswire import STATUS_BAD_PARAMS, STATUS_NOT_IMPLEMENTED, Request

SECRET_PARAMS = frozenset({"client_secret", "code", "refresh_token", "access_token", "signature"})
CALLBACK_MAX_LENGTH = 255
CALLBACK_PORT = 443
RATE_LIMIT_PER_MINUTE = 120


def api_refusal(request: Request) -> tuple[int, str] | None:
    """``(status, text)`` when an API call is shaped the way Withings does not take it."""
    if request.method.upper() != "POST":
        return STATUS_NOT_IMPLEMENTED, "wrong webservice: the API takes POST requests only"
    leaked = sorted(SECRET_PARAMS & {name.lower() for name in request.query_params()})
    if leaked:
        return STATUS_BAD_PARAMS, f"invalid_params: {','.join(leaked)} in the query string"
    return None


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def callback_url_problem(url: str) -> str | None:
    """Why Withings would not take ``url`` as a callback URL, or ``None`` when it would."""
    if not url or len(url) > CALLBACK_MAX_LENGTH:
        return f"empty or longer than {CALLBACK_MAX_LENGTH} characters"
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return "not a valid URL"
    if parts.scheme.lower() != "https":
        return "not HTTPS"
    if port not in (None, CALLBACK_PORT):
        return f"port is not {CALLBACK_PORT}"
    host = (parts.hostname or "").lower()
    labels = host.split(".")
    if _is_ip(host):
        return "an IP address, not a domain name"
    if labels[-1] == "localhost":
        return "localhost"
    tld = labels[-1]
    if len(labels) < 2 or not all(labels) or not (tld.isalpha() or tld.startswith("xn--")):
        return "not a domain name"
    return None


class RateLimiter:
    """A token bucket per application: ``per_minute`` tokens, refilled evenly over a minute,
    starting full. ``per_minute`` of ``None`` or 0 switches the limit off."""

    def __init__(self, per_minute: int | None = RATE_LIMIT_PER_MINUTE) -> None:
        self.per_minute = per_minute
        self._buckets: dict[str, tuple[float, float]] = {}  # client_id -> (tokens, last update)

    def admit(self, client_id: str, now: float) -> bool:
        if not self.per_minute:
            return True
        capacity = float(self.per_minute)
        tokens, at = self._buckets.get(client_id, (capacity, now))
        tokens = min(capacity, tokens + max(0.0, now - at) * capacity / 60)
        admitted = tokens >= 1 - 1e-6  # epoch-second floats are only good to about 3e-7 s
        self._buckets[client_id] = (tokens - 1 if admitted else tokens, now)
        return admitted
