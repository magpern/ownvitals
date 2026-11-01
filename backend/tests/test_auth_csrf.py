"""Origin and CSRF checks on every state-changing request (ADR 0003 section 4)."""

import pytest

from authtools import ORIGIN, Env, session_cookie, set_cookies
from ownvitals import auth


@pytest.fixture
def write_route(env: Env) -> None:
    @env.app.post("/api/test/write")
    async def write() -> dict[str, bool]:  # pyright: ignore[reportUnusedFunction]
        return {"done": True}

    @env.app.put("/api/test/write")
    async def put() -> dict[str, bool]:  # pyright: ignore[reportUnusedFunction]
        return {"done": True}

    @env.app.patch("/api/test/write")
    async def patch() -> dict[str, bool]:  # pyright: ignore[reportUnusedFunction]
        return {"done": True}

    @env.app.delete("/api/test/write")
    async def delete() -> dict[str, bool]:  # pyright: ignore[reportUnusedFunction]
        return {"done": True}


async def anonymous_csrf(env: Env) -> str:
    """The anonymous double-submit value, as the SPA gets it."""
    response = await env.client.get("/api/auth/session")
    value = response.json()["csrf_token"]
    assert f"{auth.CSRF_COOKIE}={value}" in set_cookies(response)[0]
    env.client.cookies.clear()  # each test sends the cookies it means to send
    return value


def anon_headers(csrf: str, **extra: str) -> dict[str, str]:
    return {"Origin": ORIGIN, "Cookie": f"{auth.CSRF_COOKIE}={csrf}", "X-CSRF-Token": csrf, **extra}


async def reason(response: object) -> str:
    return response.json()["detail"]["reason"]  # pyright: ignore[reportAttributeAccessIssue]


async def test_the_anonymous_double_submit_passes(env: Env, write_route: None) -> None:
    csrf = await anonymous_csrf(env)
    response = await env.client.post("/api/test/write", headers=anon_headers(csrf))
    assert response.status_code == 200


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
async def test_every_unsafe_method_is_checked(env: Env, write_route: None, method: str) -> None:
    csrf = await anonymous_csrf(env)
    bad = anon_headers(csrf, **{"X-CSRF-Token": "wrong"})
    response = await env.client.request(method, "/api/test/write", headers=bad)
    assert response.status_code == 403
    good = await env.client.request(method, "/api/test/write", headers=anon_headers(csrf))
    assert good.status_code == 200


async def test_safe_methods_need_no_token(env: Env) -> None:
    assert (await env.client.get("/api/health")).status_code == 200


async def test_a_foreign_origin_is_refused(env: Env, write_route: None) -> None:
    csrf = await anonymous_csrf(env)
    headers = anon_headers(csrf, Origin="https://evil.example")
    response = await env.client.post("/api/test/write", headers=headers)
    assert (response.status_code, await reason(response)) == (403, "origin_mismatch")
    for lookalike in (ORIGIN + ".evil.example", ORIGIN.upper(), "null", "http://ownvitals.test"):
        headers = anon_headers(csrf, Origin=lookalike)
        assert (await env.client.post("/api/test/write", headers=headers)).status_code == 403


async def test_a_missing_origin_needs_sec_fetch_site_same_origin(
    env: Env, write_route: None
) -> None:
    csrf = await anonymous_csrf(env)
    base = {"Cookie": f"{auth.CSRF_COOKIE}={csrf}", "X-CSRF-Token": csrf}
    neither = await env.client.post("/api/test/write", headers=base)
    assert (neither.status_code, await reason(neither)) == (403, "origin_missing")
    for site in ("cross-site", "same-site", "none"):
        response = await env.client.post(
            "/api/test/write", headers={**base, "Sec-Fetch-Site": site}
        )
        assert (response.status_code, await reason(response)) == (403, "origin_mismatch")
    same = await env.client.post(
        "/api/test/write", headers={**base, "Sec-Fetch-Site": "same-origin"}
    )
    assert same.status_code == 200


async def test_a_wrong_origin_is_not_rescued_by_sec_fetch_site(env: Env, write_route: None) -> None:
    csrf = await anonymous_csrf(env)
    headers = anon_headers(csrf, Origin="https://evil.example", **{"Sec-Fetch-Site": "same-origin"})
    assert (await env.client.post("/api/test/write", headers=headers)).status_code == 403


async def test_a_missing_token_is_refused(env: Env, write_route: None) -> None:
    csrf = await anonymous_csrf(env)
    headers = {"Origin": ORIGIN, "Cookie": f"{auth.CSRF_COOKIE}={csrf}"}
    response = await env.client.post("/api/test/write", headers=headers)
    assert (response.status_code, await reason(response)) == (403, "csrf_token_missing")


async def test_a_token_without_the_cookie_is_refused(env: Env, write_route: None) -> None:
    csrf = await anonymous_csrf(env)
    response = await env.client.post(
        "/api/test/write", headers={"Origin": ORIGIN, "X-CSRF-Token": csrf}
    )
    assert (response.status_code, await reason(response)) == (403, "csrf_token_invalid")


async def test_a_session_requires_its_own_hmac_token_not_the_cookie_value(
    env: Env, write_route: None
) -> None:
    issued = await env.login(await env.user())
    state = (await env.client.get("/api/auth/session", headers=session_cookie(issued.token))).json()
    token = state["csrf_token"]
    assert token == auth_token(env, issued.id)
    cookie = f"{auth.SESSION_COOKIE}={issued.token}; {auth.CSRF_COOKIE}=anon-value"
    base = {"Origin": ORIGIN, "Cookie": cookie}
    ok = await env.client.post("/api/test/write", headers={**base, "X-CSRF-Token": token})
    assert ok.status_code == 200
    # The anonymous double-submit value is not accepted for a signed-in request.
    anon = await env.client.post("/api/test/write", headers={**base, "X-CSRF-Token": "anon-value"})
    assert anon.status_code == 403


async def test_rotation_changes_the_session_token(env: Env, write_route: None) -> None:
    issued = await env.login(await env.user())
    old = auth_token(env, issued.id)
    async with env.db() as db:
        info = (await auth.resolve_session(env.factory, env.settings, issued.token)).info
        assert info is not None
        rotated = await auth.rotate_session(db, info, reauth=True)
    new = auth_token(env, rotated.id)
    assert new != old
    headers = {"Origin": ORIGIN, **session_cookie(rotated.token), "X-CSRF-Token": old}
    assert (await env.client.post("/api/test/write", headers=headers)).status_code == 403
    headers["X-CSRF-Token"] = new
    assert (await env.client.post("/api/test/write", headers=headers)).status_code == 200


async def test_an_unknown_session_token_falls_back_to_the_anonymous_check(
    env: Env, write_route: None
) -> None:
    csrf = await anonymous_csrf(env)
    headers = anon_headers(csrf, Cookie=f"{auth.SESSION_COOKIE}=stale; {auth.CSRF_COOKIE}={csrf}")
    response = await env.client.post("/api/test/write", headers=headers)
    assert response.status_code == 200
    assert not any(auth.SESSION_COOKIE in c for c in set_cookies(response))


async def test_session_endpoint_sets_the_anonymous_cookie_only_when_missing(env: Env) -> None:
    first = await env.client.get("/api/auth/session")
    env.client.cookies.clear()
    assert first.json()["authenticated"] is False
    assert len(set_cookies(first)) == 1
    value = first.json()["csrf_token"]
    again = await env.client.get(
        "/api/auth/session", headers={"Cookie": f"{auth.CSRF_COOKIE}={value}"}
    )
    assert again.json()["csrf_token"] == value
    assert set_cookies(again) == []


async def test_api_responses_are_no_store(env: Env) -> None:
    for path in ("/api/health", "/api/auth/session", "/api/nope"):
        response = await env.client.get(path)
        assert response.headers["cache-control"] == "no-store", path


async def test_the_csrf_check_covers_the_auth_endpoints_too(env: Env) -> None:
    issued = await env.login(await env.user())
    response = await env.client.post("/api/auth/logout-all", headers=session_cookie(issued.token))
    assert response.status_code == 403
    assert await env.scalar("SELECT count(*) FROM sessions") == 1


def auth_token(env: Env, session_id: object) -> str:
    from ownvitals.auth.middleware import csrf_token_for_session

    return csrf_token_for_session(env.app.state.keys, session_id)
