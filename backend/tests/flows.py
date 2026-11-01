"""A tiny browser for the account-flow tests: keeps its cookies, fetches the CSRF token."""

from http.cookies import SimpleCookie
from typing import Any
from uuid import UUID

import httpx
from sqlalchemy import text

from authtools import ORIGIN, Env
from ownvitals import auth

PASSWORD = "correct horse battery staple"


class Browser:
    def __init__(self, env: Env, ip: str | None = None) -> None:
        self.env = env
        self.cookies: dict[str, str] = {}
        self.ip = ip

    def _store(self, response: httpx.Response) -> None:
        for header in response.headers.get_list("set-cookie"):
            parsed: SimpleCookie = SimpleCookie()
            parsed.load(header)
            for name, morsel in parsed.items():
                if morsel.value and morsel["max-age"] != "0":
                    self.cookies[name] = morsel.value
                else:
                    self.cookies.pop(name, None)

    def _cookie_header(self) -> dict[str, str]:
        if not self.cookies:
            return {}
        return {"Cookie": "; ".join(f"{k}={v}" for k, v in self.cookies.items())}

    async def get(self, path: str) -> httpx.Response:
        self.env.client.cookies.clear()
        response = await self.env.client.get(path, headers=self._cookie_header())
        self._store(response)
        return response

    async def csrf(self) -> str:
        return (await self.get("/api/auth/session")).json()["csrf_token"]

    async def request(
        self, method: str, path: str, json: Any = None, *, csrf: str | None = None
    ) -> httpx.Response:
        token = csrf if csrf is not None else await self.csrf()
        headers = {"Origin": ORIGIN, "X-CSRF-Token": token, **self._cookie_header()}
        self.env.client.cookies.clear()
        response = await self.env.client.request(method, path, json=json, headers=headers)
        self._store(response)
        return response

    async def post(self, path: str, json: Any = None, **kwargs: Any) -> httpx.Response:
        return await self.request("POST", path, json, **kwargs)

    @property
    def signed_in(self) -> bool:
        return auth.SESSION_COOKIE in self.cookies

    async def sign_in(self, email: str, password: str = PASSWORD) -> httpx.Response:
        return await self.post("/api/auth/login", {"email": email, "password": password})


def reason(response: httpx.Response) -> str:
    return response.json()["detail"]["reason"]


async def user_with_password(env: Env, email: str = "user@example.com", **kwargs: Any):
    """A user whose password is ``PASSWORD``."""
    hashed = await env.app.state.password_hasher.hash(PASSWORD)
    return await env.user(email, password_hash=hashed, **kwargs)


async def signed_in(env: Env, email: str = "user@example.com") -> Browser:
    browser = Browser(env)
    await user_with_password(env, email)
    assert (await browser.sign_in(email)).status_code == 200
    return browser


async def age_reauth(env: Env) -> None:
    """Make every session's last authentication older than the 10-minute window."""
    await env.run("UPDATE sessions SET reauth_at = now() - interval '11 minutes'")


async def only_user_id(env: Env) -> UUID:
    async with env.db() as db:
        return (await db.execute(text("SELECT id FROM users"))).scalar_one()
