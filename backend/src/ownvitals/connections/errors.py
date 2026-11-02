"""Connection failures (ADR 0004 section 8).

A failure names a category and nothing else. It is raised after the ``except`` that noticed
it, so it has no chained exception and no provider text.
"""

CATEGORIES = (
    "needs_reconnect",
    "rate_limited",
    "upstream_unavailable",
    "blocked_by_provider_plan",
    "key_unavailable",
    "decrypt_failed",
    "not_connected",
    "revoke_failed",
    "unknown",
)


class ConnectionFailure(Exception):
    """One category from ``CATEGORIES``. ``args`` is that category and nothing more."""

    def __init__(self, category: str) -> None:
        if category not in CATEGORIES:
            raise ValueError("unknown connection category")
        super().__init__(category)
        self.category = category
