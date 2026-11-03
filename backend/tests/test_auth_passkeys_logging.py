"""Log redaction for the passkey routes (ADR 0011 section 9, T7).

Runs every passkey route through the real app at DEBUG with logs captured, then greps the logs
and every ``auth_events`` row for the challenges, credential ids, user handles, public keys,
client data, signatures and the pre-session cookie that went through. Synthetic keys only.
"""

import io
import json
from typing import Any

import pytest

from authtools import (  # noqa: F401 - fixtures
    Env,
    auth_schema_fixture,
    clean_auth_fixture,
    env_fixture,
)
from flows import Browser, signed_in
from ownvitals.auth.cookies import PRE_COOKIE
from ownvitals.log import configure_logging
from passkeytools import BASE, SoftAuthenticator, b64, register_options, signin_options, unb64


def forms(value: bytes) -> set[str]:
    """How a value could show up in a log line: base64url, hex and its repr."""
    return {b64(value), value.hex(), repr(value)}


@pytest.mark.usefixtures("restore_logging")
async def test_no_challenge_credential_handle_key_or_cookie_reaches_logs_or_events(
    env: Env,
) -> None:
    stream = io.StringIO()
    configure_logging("DEBUG", stream)
    planted: set[str] = set()
    sent: list[dict[str, Any]] = []

    browser = await signed_in(env)
    auth = SoftAuthenticator()
    options = await register_options(browser)
    created = auth.create(options)
    sent.append(created)
    assert (await browser.post(f"{BASE}/register", {"credential": created})).status_code == 201
    planted |= forms(unb64(options["challenge"])) | forms(unb64(options["user"]["id"]))
    planted |= forms(auth.credentials[0].credential_id) | forms(auth.credentials[0].cose())
    key = await env.scalar("SELECT public_key FROM webauthn_credentials")
    planted |= forms(bytes(key))  # pyright: ignore[reportArgumentType]

    # A passkey re-auth, a rename and a sign-in that works.
    reauth = (await browser.post(f"{BASE}/reauth/options")).json()
    planted |= forms(unb64(reauth["challenge"]))
    asserted = auth.get(reauth)
    sent.append(asserted)
    assert (await browser.post(f"{BASE}/reauth", {"credential": asserted})).status_code == 200
    passkey = (await browser.get(BASE)).json()["passkeys"][0]["id"]
    await browser.request("PATCH", f"{BASE}/{passkey}", {"name": "Laptop"})
    anonymous = Browser(env)
    signin = await signin_options(anonymous)
    planted.add(anonymous.cookies[PRE_COOKIE])
    planted |= forms(unb64(signin["challenge"]))
    asserted = auth.get(signin)
    sent.append(asserted)
    assert (await anonymous.post(f"{BASE}/signin", {"credential": asserted})).status_code == 200

    # Failures: a replay, a wrong origin, a malformed body, a regression that suspends.
    other = Browser(env)
    retry = await signin_options(other)
    planted.add(other.cookies[PRE_COOKIE])
    await other.post(f"{BASE}/signin", {"credential": asserted})
    bad = auth.get(retry, origin="https://evil.test")
    sent.append(bad)
    await other.post(f"{BASE}/signin", {"credential": bad})
    await other.post(f"{BASE}/signin", {"credential": {"id": b64(b"x" * 40), "junk": True}})
    third = Browser(env)
    regress = auth.get(await signin_options(third), sign_count=0)
    sent.append(regress)
    planted.add(third.cookies[PRE_COOKIE])
    await third.post(f"{BASE}/signin", {"credential": regress})
    assert await env.scalar("SELECT suspended_at IS NOT NULL FROM webauthn_credentials")
    await browser.request("DELETE", f"{BASE}/{passkey}")

    for credential in sent:
        response = credential["response"]
        for field in ("clientDataJSON", "attestationObject", "authenticatorData", "signature"):
            if field in response:
                planted.add(response[field])
        if "userHandle" in response:
            planted.add(response["userHandle"])

    out = stream.getvalue()
    assert '"ownvitals.access"' in out  # the access log did run
    events = json.dumps(
        [str(row) for row in (await env.row("SELECT array_agg(detail) FROM auth_events"))]
    )
    kinds = await env.scalar("SELECT array_agg(DISTINCT kind) FROM auth_events")
    assert {
        "passkey_registered",
        "passkey_reauth",
        "passkey_renamed",
        "passkey_signin",
        "passkey_signin_failed",
        "passkey_suspended",
        "passkey_removed",
    } <= set(kinds)  # pyright: ignore[reportArgumentType]
    for secret in planted:
        assert secret not in out, "a passkey secret reached the logs"
        assert secret not in events, "a passkey secret reached auth_events"
