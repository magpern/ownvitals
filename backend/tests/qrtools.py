"""QR approve-login flows for the tests (ADR 0011 section 5).

Synthetic only: every account, address and code is made up in the test process. A "desktop" is
a ``Browser`` without a session; a "phone" is a ``Browser`` with a full session
(``flows.signed_in``). ``QrBrowser`` sends its requests through the default trusted proxy
(loopback) with an ``X-Forwarded-For`` of its own, so each can have its own client IP.
"""

from typing import Any
from uuid import UUID

import httpx

from authtools import TEST_TOTP_SECRET, Env
from flows import Browser, signed_in
from ownvitals.auth import mfa, totp

BASE = "/api/auth/qr"
DESKTOP_IP = "198.51.100.7"  # TEST-NET-2
PHONE_IP = "198.51.100.7"  # the household case: one NAT
FIREFOX_ON_WINDOWS = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:128.0) Gecko/20100101 Firefox/128.0"
)


class QrBrowser(Browser):
    def __init__(self, env: Env, ip: str = DESKTOP_IP, user_agent: str | None = None) -> None:
        super().__init__(env, ip)
        self.user_agent = user_agent
        self.extra: dict[str, str] = {}  # more headers on every request

    def _cookie_header(self) -> dict[str, str]:
        headers = super()._cookie_header()
        if self.ip:
            headers["X-Forwarded-For"] = self.ip
        if self.user_agent:
            headers["User-Agent"] = self.user_agent
        return {**headers, **self.extra}


async def phone(env: Env, email: str = "user@example.com", ip: str = PHONE_IP) -> QrBrowser:
    """A phone with a full session (password and TOTP)."""
    signed = await signed_in(env, email)
    browser = QrBrowser(env, ip)
    browser.cookies = dict(signed.cookies)
    return browser


async def fresh_code(env: Env) -> str:
    """A TOTP code of the current step that has not been used yet (synthetic secret)."""
    await env.run("UPDATE mfa_totp SET last_step = 0")
    return totp.code_for_step(TEST_TOTP_SECRET, int(totp.clock() // totp.PERIOD))


async def recovery_codes(env: Env, user_id: UUID) -> list[str]:
    async with env.db() as db:
        codes = await mfa.regenerate_recovery_codes(db, env.app.state.keys, user_id)
    assert codes is not None
    return codes


async def start(desktop: Browser) -> dict[str, Any]:
    answer = await desktop.post(f"{BASE}/start")
    assert answer.status_code == 200, answer.text
    return answer.json()


def nonce_of(started: dict[str, Any]) -> str:
    return started["qr_url"].split("#", 1)[1]


async def scan(phone_browser: Browser, started: dict[str, Any]) -> httpx.Response:
    return await phone_browser.post(f"{BASE}/scan", {"nonce": nonce_of(started)})


async def approve(phone_browser: Browser, qr_id: str, code: str) -> httpx.Response:
    return await phone_browser.post(f"{BASE}/{qr_id}/approve", {"code": code})


async def complete(desktop: Browser, qr_id: str) -> httpx.Response:
    return await desktop.post(f"{BASE}/{qr_id}/complete")


async def approved(env: Env, desktop: Browser, phone_browser: Browser) -> str:
    """Start, scan and approve; the row id."""
    started = await start(desktop)
    assert (await scan(phone_browser, started)).status_code == 200
    answer = await approve(phone_browser, started["id"], await fresh_code(env))
    assert answer.status_code == 200, answer.text
    return started["id"]


def unavailable(response: httpx.Response) -> bool:
    return response.status_code == 404 and response.json() == {
        "detail": {"reason": "qr_unavailable"}
    }


def frames(body: str) -> list[str]:
    """The event names of an SSE body, in order (keepalive comments left out)."""
    return [
        line.removeprefix("event: ") for line in body.splitlines() if line.startswith("event: ")
    ]
