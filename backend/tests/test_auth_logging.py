"""Log redaction (ADR 0003 section 10, T7): plant each kind of secret, grep the logs."""

import contextlib
import io
import json
import logging

import pytest

from authtools import ORIGIN, Env
from ownvitals import auth
from ownvitals.log import configure_logging, redact

SESSION = "SESSIONTOKEN-aaaaaaaaaaaaaaaaaaaaaaaa"
DEVICE = "DEVICETOKEN-bbbbbbbbbbbbbbbbbbbbbbbbb"
CSRF = "CSRFTOKEN-cccccccccccccccccccccccccc"
OAUTH = "OAUTHFLOW-dddddddddddddddddddddddddd"
PENDING = "PENDINGTOKEN-eeeeeeeeeeeeeeeeeeeeeeee"
BEARER = "BEARERTOKEN-fffffffffffffffffffffffff"
CODE = "OAUTHCODE-ggggggggggggggggggggggggg"
STATE = "OAUTHSTATE-hhhhhhhhhhhhhhhhhhhhhhhhh"
PASSWORD = "PLANTED-PASSWORD-iiiiiiiiiiiiiiii"
SECRETS = [SESSION, DEVICE, CSRF, OAUTH, PENDING, BEARER, CODE, STATE, PASSWORD]

COOKIE_HEADER = (
    f"__Host-ownvitals_session={SESSION}; __Host-ownvitals_device={DEVICE}; "
    f"__Host-ownvitals_csrf={CSRF}; __Host-ownvitals_oauth={OAUTH}; "
    f"__Host-ownvitals_pending={PENDING}"
)


def leaked(text: str) -> list[str]:
    return [secret for secret in SECRETS if secret in text]


@pytest.mark.parametrize(
    "line",
    [
        f"Cookie: {COOKIE_HEADER}",
        f"cookie={COOKIE_HEADER}",
        f"Set-Cookie: __Host-ownvitals_session={SESSION}; HttpOnly; Secure",
        f"Authorization: Bearer {BEARER}",
        f"authorization='Basic {BEARER}'",
        f"X-CSRF-Token: {CSRF}",
        f"headers={{'x-csrf-token': '{CSRF}', 'cookie': '__Host-ownvitals_session={SESSION}'}}",
        f"__Host-ownvitals_session={SESSION}",
        f"sent Bearer {BEARER} upstream",
        f'"GET /oauth/google/callback?code={CODE}&state={STATE} HTTP/1.1" 200',
        f"GET /oauth/github/callback?state={STATE}&code={CODE}",
    ],
)
def test_redact_removes_each_planted_secret(line: str) -> None:
    assert leaked(redact(line)) == []


def test_redact_keeps_the_rest_of_the_message_readable() -> None:
    assert redact("request failed for /api/auth/session") == "request failed for /api/auth/session"
    assert redact("GET /oauth/google/callback?code=x HTTP/1.1") == (
        "GET /oauth/google/callback HTTP/1.1"
    )
    assert "[redacted]" in redact(f"__Host-ownvitals_session={SESSION}")


@pytest.mark.usefixtures("restore_logging")
def test_the_formatter_redacts_messages_extras_and_tracebacks() -> None:
    stream = io.StringIO()
    configure_logging("DEBUG", stream)
    log = logging.getLogger("ownvitals.test")
    log.info("headers were %s", {"cookie": COOKIE_HEADER, "authorization": f"Bearer {BEARER}"})
    log.info("extra", extra={"request": f"GET /oauth/x/callback?code={CODE}", "n": 1})
    try:
        raise ValueError(f"Cookie: {COOKIE_HEADER}")
    except ValueError:
        log.exception("boom")
    out = stream.getvalue()
    assert leaked(out) == []
    for line in out.splitlines():
        json.loads(line)  # still one valid JSON object per line


@pytest.mark.usefixtures("restore_logging")
def test_sql_parameters_are_not_logged_even_at_debug() -> None:
    configure_logging("DEBUG", io.StringIO())
    assert logging.getLogger("sqlalchemy.engine").getEffectiveLevel() == logging.WARNING


@pytest.mark.usefixtures("restore_logging")
async def test_requests_and_session_handling_log_no_token_hash_cookie_or_body(env: Env) -> None:
    stream = io.StringIO()
    configure_logging("DEBUG", stream)

    @env.app.get("/api/test/leaky")
    async def leaky() -> None:  # pyright: ignore[reportUnusedFunction]
        logging.getLogger("ownvitals.test").error("failing request: %s", COOKIE_HEADER)
        raise RuntimeError(f"Authorization: Bearer {BEARER}")

    issued = await env.login(await env.user())
    stored_hash = auth.token_hash(issued.token)
    headers = {
        "Cookie": COOKIE_HEADER.replace(SESSION, issued.token),
        "Authorization": f"Bearer {BEARER}",
        "X-CSRF-Token": CSRF,
        "Origin": ORIGIN,
    }
    client = env.client
    await client.get("/api/auth/session", headers=headers)
    await client.get(f"/oauth/google/callback?code={CODE}&state={STATE}", headers=headers)
    await client.post("/api/auth/login", headers=headers, json={"password": PASSWORD})
    await client.post("/api/auth/logout-all", headers=headers, content=PASSWORD)
    async with env.db() as db:
        info = (await auth.resolve_session(env.factory, env.settings, issued.token)).info
        assert info is not None
        rotated = await auth.rotate_session(db, info, mfa=True)
    await client.get(
        "/api/auth/session", headers={"Cookie": f"{auth.SESSION_COOKIE}={rotated.token}"}
    )
    with contextlib.suppress(RuntimeError):  # the test transport re-raises server errors
        await client.get("/api/test/leaky", headers=headers)

    out = stream.getvalue()
    assert "request" in out  # the access log did run
    assert leaked(out) == []
    for token in (issued.token, rotated.token):
        assert token not in out
    for digest in (stored_hash, auth.token_hash(rotated.token)):
        assert digest.hex() not in out
        assert repr(digest) not in out
    # The access log records path and status, never a query string.
    access = [json.loads(line) for line in out.splitlines() if '"ownvitals.access"' in line]
    assert any(entry["path"] == "/oauth/google/callback" for entry in access)
    assert all("?" not in entry["path"] for entry in access)


@pytest.mark.parametrize(
    "line",
    [
        f"{{'__Host-ownvitals_session': '{SESSION}', '__Host-ownvitals_device': '{DEVICE}'}}",
        f'{{"__Host-ownvitals_csrf": "{CSRF}", "__Host-ownvitals_oauth": "{OAUTH}"}}',
        f"{{'__Host-ownvitals_pending':'{PENDING}'}}",
    ],
)
def test_redact_catches_dict_repr_cookie_values(line: str) -> None:
    assert leaked(redact(line)) == []


@pytest.mark.usefixtures("restore_logging")
async def test_database_errors_do_not_put_hashes_or_emails_in_the_logs(
    env: Env, database_url: str
) -> None:
    from sqlalchemy import create_engine, text
    from sqlalchemy.exc import SQLAlchemyError

    stream = io.StringIO()
    configure_logging("DEBUG", stream)
    log = logging.getLogger("ownvitals.test")
    # Built at run time so the traceback's source lines do not contain the needles.
    email = "private" + ".person@example.org"
    email2 = "private" + ".person2@example.org"
    issued = await env.login(await env.user(email))
    digest = auth.token_hash(issued.token)
    # (1) a broken statement on the path that carries the token hash.
    admin = create_engine(database_url, isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as connection:
            connection.execute(text("ALTER TABLE sessions RENAME COLUMN last_seen_at TO seen"))
        try:
            with pytest.raises(SQLAlchemyError) as hidden:
                await auth.resolve_session(env.factory, env.settings, issued.token)
            log.exception("resolve failed", exc_info=hidden.value)
            assert "hide_parameters" in str(hidden.value)  # the statement is there, values not
        finally:
            with admin.connect() as connection:
                connection.execute(text("ALTER TABLE sessions RENAME COLUMN seen TO last_seen_at"))
    finally:
        admin.dispose()
    # (2) a failing statement that carries an email as a parameter. (A unique violation is not
    # used: Postgres itself puts the key value in the error's DETAIL, which #37 must catch.)
    admin = create_engine(database_url, isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as connection:
            connection.execute(text("ALTER TABLE users RENAME COLUMN email TO mail"))
        try:
            with pytest.raises(SQLAlchemyError) as failing:
                await env.user(email2)
            log.exception("create user failed", exc_info=failing.value)
        finally:
            with admin.connect() as connection:
                connection.execute(text("ALTER TABLE users RENAME COLUMN mail TO email"))
    finally:
        admin.dispose()

    out = stream.getvalue()
    assert "resolve failed" in out
    assert "create user failed" in out
    escaped = json.dumps(repr(digest))[1:-1]
    for needle in (digest.hex(), repr(digest), escaped, email, email2):
        assert needle not in out, (
            needle,
            out[max(out.find(needle) - 300, 0) : out.find(needle) + 200],
        )


@pytest.mark.usefixtures("restore_logging")
async def test_a_unique_violation_reaching_the_app_is_logged_as_class_and_sqlstate(
    env: Env,
) -> None:
    stream = io.StringIO()
    configure_logging("DEBUG", stream)
    email = "dup" + ".person@example.org"
    await env.user(email)

    @env.app.get("/api/test/duplicate")
    async def duplicate() -> None:  # pyright: ignore[reportUnusedFunction]
        await env.user(email)  # a unique violation whose DETAIL names the email

    response = await env.client.get("/api/test/duplicate")
    assert response.status_code == 500
    assert response.json() == {"detail": "Internal Server Error"}
    # A DBAPIError logged by anything else is scrubbed by the formatter too.
    with pytest.raises(Exception, match="duplicate key") as caught:
        await env.user(email)
    logging.getLogger("ownvitals.test").error("failed", exc_info=caught.value)

    out = stream.getvalue()
    assert "IntegrityError (SQLSTATE 23505)" in out
    assert out.count("IntegrityError (SQLSTATE 23505)") >= 2
    assert email not in out
    assert "dup" + ".person" not in out
