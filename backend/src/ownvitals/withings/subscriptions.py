"""Withings notification subscriptions and webhook-secret derivation (PLAN section 4.4).

The callback URL carries the derived secret. It is passed to ``notify`` and nowhere else:
not logged, not stored, and not put on a job. A retired seed can rebuild that URL. Only a
current seed may mint a new one.
"""

import hashlib
import hmac
from base64 import urlsafe_b64encode
from typing import Any, Protocol
from uuid import UUID

PREFIX = "/api/webhooks/withings/"
# The applis PLAN section 4.4 subscribes on connect.
APPLIS = (1, 2, 4, 16, 44, 46, 54, 55, 58, 62, 63)


class NotifyClient(Protocol):
    async def notify_subscribe(
        self, token: str, *, callbackurl: str, appli: int, comment: str | None = None, **rest: Any
    ) -> None: ...


def derive_secret(seed: bytes, app_id: int, secret_id: UUID) -> str:
    """``HMAC-SHA256(seed, "ownvitals-webhook-v1|<app>|<secret>")``, base64url, no padding."""
    if not isinstance(seed, bytes) or len(seed) != 32:
        raise ValueError("a webhook seed is 32 bytes")
    if type(app_id) is not int or app_id < 1:
        raise ValueError("the app id is a positive integer")
    message = f"ownvitals-webhook-v1|{app_id}|{secret_id}".encode()
    digest = hmac.new(seed, message, hashlib.sha256).digest()
    return urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def pepper_hmac(pepper: bytes, presented: str) -> bytes:
    """``HMAC-SHA256(pepper, the presented secret)``. Stored; the secret is not."""
    if not isinstance(pepper, bytes) or len(pepper) != 32:
        raise ValueError("a webhook pepper is 32 bytes")
    return hmac.new(pepper, presented.encode("ascii"), hashlib.sha256).digest()


class CurrentWebhookSeed:
    """The current seed. It may derive a secret for a new id."""

    def __init__(self, key_id: str, key: bytes) -> None:
        self.key_id = key_id
        self._key = key

    def __repr__(self) -> str:
        return f"CurrentWebhookSeed(key_id={self.key_id!r})"

    def derive(self, app_id: int, secret_id: UUID) -> str:
        return derive_secret(self._key, app_id, secret_id)


class RetiredWebhookSeed:
    """A retired seed rebuilds an existing URL. It has no ``derive``."""

    def __init__(self, key_id: str, key: bytes) -> None:
        self.key_id = key_id
        self._key = key

    def __repr__(self) -> str:
        return f"RetiredWebhookSeed(key_id={self.key_id!r})"

    def rebuild(self, app_id: int, secret_id: UUID) -> str:
        return derive_secret(self._key, app_id, secret_id)


def wrap_seed(
    key_id: str, key: bytes, *, current_id: str
) -> CurrentWebhookSeed | RetiredWebhookSeed:
    """The ring returns every seed as one class. A non-current id is wrapped as retired."""
    if key_id == current_id:
        return CurrentWebhookSeed(key_id, key)
    return RetiredWebhookSeed(key_id, key)


def mint(seed: CurrentWebhookSeed, app_id: int, secret_id: UUID) -> str:
    """A new secret. A retired seed is refused."""
    if not isinstance(seed, CurrentWebhookSeed):
        raise TypeError("a retired webhook seed only rebuilds an existing url")
    return seed.derive(app_id, secret_id)


def callback_url(base: str, secret: str) -> str:
    """The callback Withings stores. ``base`` has no path; the secret is one path segment."""
    return base.rstrip("/") + PREFIX + secret


async def subscribe(
    client: NotifyClient, token: str, base: str, secret: str, applis: tuple[int, ...] = APPLIS
) -> None:
    """Subscribe each appli. The URL is an argument of ``notify`` and is not logged here."""
    url = callback_url(base, secret)
    for appli in applis:
        await client.notify_subscribe(token, callbackurl=url, appli=appli)
