"""The hosts and sockets each process kind may reach, for the egress hook (ADR 0009).

``_policy_for(kind, settings)`` builds the policy ``install_egress_hook`` installs: the kind's
resolvable host names (its egress hosts, the configured database hosts and, in web and the CLI,
the SMTP host) and the database's Unix socket paths. ``ownvitals.egress.hook`` applies it.

The ``eval`` kind is the model evals' process (``python -m ownvitals.ai.eval``, ADR 0014 §9): it
resolves the model host and the database hosts, nothing else. Its set is fixed: an installed
worker list never widens it, where every other kind also resolves the installed list's hosts.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Literal, get_args

from sqlalchemy.engine import make_url

from ownvitals.egress.core import (
    GOOGLE_SIGNIN_HOSTS,
    MODEL_HOST,
    TURNSTILE_HOST,
    WITHINGS_API_HOST,
    installed_worker_hosts,
)

if TYPE_CHECKING:
    from ownvitals.config import Settings

Kind = Literal["web", "worker", "cli", "migrate", "eval"]
KINDS: Final = frozenset(get_args(Kind))
WEB_HOSTS: Final = GOOGLE_SIGNIN_HOSTS | {WITHINGS_API_HOST}
POSTGRES_PORT: Final = 5432


@dataclass(frozen=True)
class _Policy:
    kind: Kind
    hosts: frozenset[str]  # resolvable host names, lower case, no trailing dot
    unix_paths: frozenset[str]  # AF_UNIX connect targets (the database's socket)

    def installed_hosts(self) -> frozenset[str]:
        """The installed worker list's hosts, which this kind resolves too; none for ``eval``."""
        return frozenset() if self.kind == "eval" else installed_worker_hosts()

    def resolvable(self, host: str) -> bool:
        return host in self.hosts or host in self.installed_hosts()


def _normalise(host: object) -> str | None:
    if isinstance(host, bytes | bytearray):
        host = bytes(host).decode("ascii", "replace")
    if not isinstance(host, str):
        return None
    return host.strip().rstrip(".").lower()


def _database_endpoints(settings: Settings) -> tuple[set[str], set[str]]:
    """The host names and Unix socket paths of every configured database URL (only the host,
    the port and the query's ``host`` are read)."""
    hosts: set[str] = set()
    paths: set[str] = set()
    urls = [settings.database_url, settings.migrations_database_url, settings.maint_database_url]
    for secret in urls:
        if secret is None:
            continue
        url = make_url(secret.get_secret_value())
        query_host = url.query.get("host")
        names = [url.host] if url.host else []
        names += [query_host] if isinstance(query_host, str) else list(query_host or ())
        for name in (n for item in names for n in item.split(",") if n):
            if name.startswith("/"):
                paths.add(f"{name.rstrip('/')}/.s.PGSQL.{url.port or POSTGRES_PORT}")
            elif (host := _normalise(name)) is not None:
                hosts.add(host)
    return hosts, paths


def _policy_for(kind: Kind, settings: Settings) -> _Policy:
    hosts, paths = _database_endpoints(settings)
    if kind == "web":
        hosts |= WEB_HOSTS
        if settings.turnstile_enabled is True:  # roadmap Amendment 12: only when it is on
            hosts.add(TURNSTILE_HOST)
    if kind == "eval":  # ADR 0014 §9: the model host only, no Withings, Google or SMTP host
        hosts.add(MODEL_HOST)
    if kind in ("web", "cli") and settings.smtp_host:
        hosts.add(_normalise(settings.smtp_host) or "")
    return _Policy(kind, frozenset(hosts - {""}), frozenset(paths))
