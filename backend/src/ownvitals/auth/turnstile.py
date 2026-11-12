"""Cloudflare Turnstile on sign-up, sign-in and the reset request (ADR 0018, roadmap Amendment 12).

Off by default (``turnstile_enabled``). On, each of the three routes takes the widget's token in
the ``X-Turnstile-Token`` header and checks it at Cloudflare (``egress.verify_turnstile_token``,
web only) before it reads the database, hashes a password or counts against a rate limit. It
fails closed: no token, more than one, a token the egress package refuses to send, a refused or
unexpected answer (``turnstile_policy``), a timeout or any other error is answered 400
``turnstile_failed`` and nothing else happens. Off, the check returns at once: no verifier
exists, nothing is sent, and the runtime hook does not resolve the host.

The log line of a failure names the outcome only, never the token or the secret.
"""

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Final, Protocol
from urllib.parse import urlsplit

from fastapi import HTTPException, Request
from pydantic import BaseModel

from ownvitals.auth.turnstile_policy import Action, passed
from ownvitals.config import Settings
from ownvitals.egress import TURNSTILE_SCRIPT_URL, verify_turnstile_token

logger = logging.getLogger(__name__)

TOKEN_HEADER: Final = "X-Turnstile-Token"  # noqa: S105 - a header name
FAILED: Final = {"reason": "turnstile_failed"}


class Answer(Protocol):
    """What the verifier reads of the siteverify answer."""

    @property
    def status_code(self) -> int: ...

    @property
    def content(self) -> bytes: ...


Post = Callable[[str, str], Awaitable[Answer]]


async def _post(secret: str, token: str) -> Answer:
    return await verify_turnstile_token(secret=secret, token=token)


class Verifier(Protocol):
    async def verify(self, token: str, action: Action) -> bool: ...


@dataclass(frozen=True)
class CloudflareVerifier:
    """Checks a token at Cloudflare through the egress package. ``post`` is the sender (tests
    hand in one over a fake transport; there is no other)."""

    secret: str = field(repr=False)
    hostname: str
    post: Post = field(default=_post, repr=False)

    async def verify(self, token: str, action: Action) -> bool:
        try:
            answer = await self.post(self.secret, token)
        except Exception as error:  # fail closed; the class only (a message may hold values)
            logger.warning("turnstile check failed", extra={"outcome": type(error).__name__})
            return False
        ok = passed(answer.status_code, answer.content, hostname=self.hostname, action=action)
        if not ok:
            logger.warning("turnstile check failed", extra={"outcome": "refused"})
        return ok


class TurnstileConfig(BaseModel):
    """What the page needs to show the widget (``GET /api/auth/session``)."""

    site_key: str
    script_url: str


def build_verifier(settings: Settings) -> CloudflareVerifier | None:
    """The process's verifier, or None while Turnstile is off. The settings refuse "on" without
    both keys (``config_turnstile``)."""
    if not settings.turnstile_enabled or settings.turnstile_secret_key is None:
        return None
    hostname = urlsplit(settings.public_base_url).hostname or ""
    return CloudflareVerifier(settings.turnstile_secret_key.get_secret_value(), hostname)


def session_config(settings: Settings) -> TurnstileConfig | None:
    if not settings.turnstile_enabled or not settings.turnstile_site_key:
        return None
    return TurnstileConfig(site_key=settings.turnstile_site_key, script_url=TURNSTILE_SCRIPT_URL)


def require_turnstile(action: Action) -> Callable[[Request], Awaitable[None]]:
    """A route dependency: with Turnstile on, refuse the request unless its token passes."""

    async def check(request: Request) -> None:
        settings: Settings = request.app.state.settings
        if settings.turnstile_enabled is not True:
            return
        verifier: Verifier | None = getattr(request.app.state, "turnstile", None)
        tokens = request.headers.getlist(TOKEN_HEADER)
        if verifier is None or len(tokens) != 1 or not tokens[0]:
            outcome = "unavailable" if verifier is None else "missing"
            logger.warning("turnstile check failed", extra={"outcome": outcome})
            raise HTTPException(status_code=400, detail=FAILED)
        if not await verifier.verify(tokens[0], action):
            raise HTTPException(status_code=400, detail=FAILED)

    return check
