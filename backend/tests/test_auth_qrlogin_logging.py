"""Log redaction for the QR approve-login routes (ADR 0011 section 9, T7).

Runs every QR route through the real app at DEBUG with logs captured (start, events, scan,
approve, deny, complete, refusals), then greps the logs and every ``auth_events`` row for the
nonces, the QR URLs, the pre-session cookies, the session tokens and the codes that went
through. Synthetic accounts and codes only."""

import asyncio
import io
import json

import pytest

from authtools import (  # noqa: F401 - fixtures
    Env,
    auth_schema_fixture,
    clean_auth_fixture,
    env_fixture,
)
from ownvitals.auth.cookies import PRE_COOKIE, SESSION_COOKIE
from ownvitals.log import configure_logging
from qrtools import (
    BASE,
    QrBrowser,
    approve,
    complete,
    fresh_code,
    nonce_of,
    phone,
    scan,
    start,
)


@pytest.mark.usefixtures("restore_logging")
async def test_no_nonce_cookie_token_or_code_reaches_logs_or_events(env: Env) -> None:
    stream = io.StringIO()
    configure_logging("DEBUG", stream)
    planted: set[str] = set()

    mobile = await phone(env)
    planted.add(mobile.cookies[SESSION_COOKIE])
    # A completed login, with its stream.
    desktop = QrBrowser(env)
    started = await start(desktop)
    planted |= {nonce_of(started), started["qr_url"], desktop.cookies[PRE_COOKIE]}
    events = asyncio.create_task(desktop.get(f"{BASE}/{started['id']}/events"))
    assert (await scan(mobile, started)).status_code == 200
    wrong = "123456"
    assert (await approve(mobile, started["id"], wrong)).status_code == 400
    code = await fresh_code(env)
    planted.add(code)
    assert (await approve(mobile, started["id"], code)).status_code == 200
    await asyncio.wait_for(events, 10)
    assert (await complete(desktop, started["id"])).status_code == 200
    planted.add(desktop.cookies[SESSION_COOKIE])
    # A denied one, a refused scan and a refused complete.
    other = QrBrowser(env, "198.51.100.12")
    denied = await start(other)
    planted |= {nonce_of(denied), other.cookies[PRE_COOKIE]}
    assert (await scan(mobile, denied)).status_code == 200
    assert (await mobile.post(f"{BASE}/{denied['id']}/deny")).status_code == 200
    assert (await scan(mobile, denied)).status_code == 404
    assert (await complete(other, denied["id"])).status_code == 404

    out = stream.getvalue()
    assert '"ownvitals.access"' in out  # the access log did run
    events_text = json.dumps(
        [str(row) for row in (await env.row("SELECT array_agg(detail) FROM auth_events"))]
    )
    kinds = await env.scalar("SELECT array_agg(DISTINCT kind) FROM auth_events")
    assert {
        "qr_login_started",
        "qr_login_scanned",
        "qr_login_approved",
        "qr_login_denied",
        "qr_login_completed",
        "qr_login_refused",
        "mfa_failed",
    } <= set(kinds)  # pyright: ignore[reportArgumentType]
    for secret in planted:
        assert secret not in out, "a QR secret reached the logs"
        assert secret not in events_text, "a QR secret reached auth_events"
    assert wrong not in events_text
