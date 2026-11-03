"""Listing, renaming, removing and re-authenticating with passkeys (ADR 0011 sections 2, 3, 8).

Passkeys live in a system table under guard 8 (ADR 0011 section 2), so the isolation is the
user filter of every query: one user can't list, rename, remove or use another's credential
(request context), and deleting or purging one account leaves the other's passkeys alone
(purge context). Every key and account is synthetic.
"""

from uuid import UUID

from authtools import (  # noqa: F401 - fixtures
    Env,
    auth_schema_fixture,
    clean_auth_fixture,
    env_fixture,
    running_env,
)
from ctx import make_context
from flows import Browser, reason, signed_in
from ownvitals.auth.purge import delete_account, purge_expired
from passkeytools import BASE, SoftAuthenticator, failed, reauth, register

A, B = "a@example.com", "b@example.com"


async def owner(env: Env, email: str) -> tuple[Browser, SoftAuthenticator, str]:
    browser = await signed_in(env, email)
    auth = SoftAuthenticator()
    answer = await register(browser, auth, name=email.split("@")[0])
    assert answer.status_code == 201, answer.text
    return browser, auth, answer.json()["id"]


async def test_each_user_lists_only_their_own_passkeys(env: Env) -> None:
    a, _, a_id = await owner(env, A)
    b, _, b_id = await owner(env, B)
    listed = (await a.get(BASE)).json()
    assert [p["id"] for p in listed["passkeys"]] == [a_id]
    assert listed["limit"] == 10
    assert set(listed["passkeys"][0]) == {
        "id", "name", "created_at", "last_used_at", "synced", "backed_up", "suspended",
    }  # fmt: skip
    assert [p["id"] for p in (await b.get(BASE)).json()["passkeys"]] == [b_id]


async def test_rename_is_the_users_own_and_another_users_id_is_404(env: Env) -> None:
    a, _, a_id = await owner(env, A)
    _, _, b_id = await owner(env, B)
    renamed = await a.request("PATCH", f"{BASE}/{a_id}", {"name": "Phone ①"})
    assert renamed.status_code == 200
    assert renamed.json()["name"] == "Phone 1"  # NFKC
    foreign = await a.request("PATCH", f"{BASE}/{b_id}", {"name": "mine now"})
    assert foreign.status_code == 404
    assert (await a.request("PATCH", f"{BASE}/not-a-uuid", {"name": "x"})).status_code == 404
    assert await env.scalar("SELECT name FROM webauthn_credentials WHERE id = :i", i=b_id) == "b"
    kinds = await env.scalar("SELECT count(*) FROM auth_events WHERE kind = 'passkey_renamed'")
    assert kinds == 1


async def test_remove_is_the_users_own_and_another_users_id_is_404(env: Env) -> None:
    a, _, a_id = await owner(env, A)
    _, _, b_id = await owner(env, B)
    assert (await a.request("DELETE", f"{BASE}/{b_id}")).status_code == 404
    assert await env.scalar("SELECT count(*) FROM webauthn_credentials") == 2
    assert (await a.request("DELETE", f"{BASE}/{a_id}")).status_code == 204
    assert (await a.request("DELETE", f"{BASE}/{a_id}")).status_code == 404
    assert await env.scalar("SELECT count(*) FROM webauthn_credentials") == 1
    assert await env.scalar("SELECT count(*) FROM auth_events WHERE kind = 'passkey_removed'") == 1


async def test_a_passkey_reauth_sets_recent_auth_and_never_a_step_up(env: Env) -> None:
    browser, auth, _ = await owner(env, A)
    await env.run(
        "UPDATE sessions SET reauth_at = now() - interval '11 minutes', "
        "stepup_at = now() - interval '11 minutes'"
    )
    answer = await reauth(browser, auth)
    assert answer.status_code == 200, answer.text
    row = await env.row(
        "SELECT reauth_at > now() - interval '1 minute' AS fresh, "
        "stepup_at < now() - interval '10 minutes' AS stale FROM sessions"
    )
    assert row.fresh
    assert row.stale
    # A step-up action after a passkey alone is still refused.
    refused = await browser.post("/api/auth/logout-all")
    assert (refused.status_code, reason(refused)) == (403, "step_up_required")
    assert await env.scalar("SELECT count(*) FROM auth_events WHERE kind = 'passkey_reauth'") == 1


async def test_reauth_with_another_users_credential_is_refused(env: Env) -> None:
    a, _, _ = await owner(env, A)
    _, b_auth, _ = await owner(env, B)
    options = (await a.post(f"{BASE}/reauth/options")).json()
    # The options list only A's credentials.
    ids = {c["id"] for c in options["allowCredentials"]}
    assert len(ids) == 1
    answer = await a.post(f"{BASE}/reauth", {"credential": b_auth.get(options)})
    assert failed(answer)
    detail = await env.scalar("SELECT detail FROM auth_events WHERE kind = 'passkey_signin_failed'")
    assert detail == {"reason": "unknown_credential", "purpose": "reauth"}
    assert await env.scalar(
        "SELECT sign_count FROM webauthn_credentials WHERE name = 'b'"
    ) == 0  # fmt: skip


async def test_a_user_without_passkeys_gets_no_reauth_options(env: Env) -> None:
    browser = await signed_in(env, A)
    answer = await browser.post(f"{BASE}/reauth/options")
    assert (answer.status_code, reason(answer)) == (404, "no_passkeys")


async def test_deleting_one_account_removes_only_its_passkeys(env: Env) -> None:
    await owner(env, A)
    await owner(env, B)
    a_id = UUID(str(await env.scalar("SELECT id FROM users WHERE email = :e", e=A)))
    await delete_account(env.factory, make_context(a_id))
    assert await env.scalar("SELECT count(*) FROM webauthn_credentials") == 1
    assert await env.scalar("SELECT name FROM webauthn_credentials") == "b"


async def test_the_hourly_purge_keeps_every_passkey(env: Env) -> None:
    await owner(env, A)
    await env.run("UPDATE webauthn_credentials SET created_at = now() - interval '400 days'")
    await purge_expired(env.factory, env.settings)
    assert await env.scalar("SELECT count(*) FROM webauthn_credentials") == 1


async def test_passkeys_are_off_on_an_ip_address_host(
    app_database_url: str, clean_auth: None
) -> None:
    async with running_env(app_database_url, public_base_url="http://127.0.0.1:8000") as env:
        assert env.settings.passkey_rp_id is None
        csrf = (await env.client.get("/api/auth/session")).json()["csrf_token"]
        env.client.cookies.clear()
        answer = await env.client.post(
            f"{BASE}/signin/options",
            headers={
                "Origin": "http://127.0.0.1:8000",
                "X-CSRF-Token": csrf,
                "Cookie": f"__Host-ownvitals_csrf={csrf}",
            },
        )
        assert (answer.status_code, reason(answer)) == (404, "passkeys_unavailable")
