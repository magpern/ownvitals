"""``keys wipe`` removes credentials and keeps the row (ADR 0004 section 3).

Synthetic tokens only. The command runs as the owner role, refuses while the app role is
connected or the configured KEK id already has a check row, and mails no token.
"""

import argparse
from io import StringIO

import pytest
from sqlalchemy import create_engine, text

from authtools import (  # noqa: F401
    Env,
    auth_schema_fixture,
    clean_auth_fixture,
    env_fixture,
    make_settings,
    smtp_fixture,
)
from ctx import make_context
from ownvitals.connections.repository import connect
from ownvitals.keys_cli import add_keys_subcommands, wipe
from smtpfake import SmtpFake

ACCESS = "synthetic-access-token-aaaaaaaa"
REFRESH = "synthetic-refresh-token-bbbbbbb"


def _settings(app_database_url: str, owner_database_url: str, **overrides: object):
    values: dict[str, object] = {
        "migrations_database_url": owner_database_url,
        "connection_kek_id": "kek-9",
    }
    values.update(overrides)
    return make_settings(app_database_url, **values)


async def _stop_web(env: Env) -> None:
    """Web is stopped before a wipe: the event hub's LISTEN, then the pool."""
    await env.app.state.event_hub.stop()
    await env.app.state.engine.dispose()


async def _wipe(settings: object) -> tuple[int, str, str]:
    out, err = StringIO(), StringIO()
    code = await wipe(
        argparse.Namespace(action="wipe", confirm=True, resend=False),
        settings,  # pyright: ignore[reportArgumentType]
        out,
        err,
    )
    return code, out.getvalue(), err.getvalue()


def test_wipe_needs_a_mode() -> None:
    parser = argparse.ArgumentParser()
    add_keys_subcommands(parser.add_subparsers(dest="command"))
    with pytest.raises(SystemExit):
        parser.parse_args(["keys", "wipe"])


async def test_wipe_refuses_while_the_app_role_is_connected(
    env: Env, app_database_url: str, owner_database_url: str
) -> None:
    await env.scalar("SELECT 1")
    code, _out, err = await _wipe(_settings(app_database_url, owner_database_url))
    assert code == 1
    assert "the app role is still connected" in err


async def test_wipe_refuses_while_the_configured_key_id_has_a_check_row(
    env: Env, app_database_url: str, owner_database_url: str
) -> None:
    await _stop_web(env)
    settings = _settings(app_database_url, owner_database_url, connection_kek_id="kek-1")
    code, _out, err = await _wipe(settings)
    assert code == 1
    assert "check row" in err


async def test_wipe_removes_credentials_and_keeps_the_row(
    env: Env,
    app_database_url: str,
    owner_database_url: str,
    database_url: str,
    smtp: SmtpFake,
) -> None:
    user_id = await env.user("wipe@example.com")
    await env.enrol(user_id)
    await env.user("idle@example.com")
    info = await connect(
        env.factory, env.app.state.keyring, make_context(user_id), "test", ACCESS, REFRESH
    )
    await _stop_web(env)
    settings = _settings(
        app_database_url,
        owner_database_url,
        smtp_host="127.0.0.1",
        smtp_port=smtp.port,
        smtp_tls="none",
    )
    code, out, err = await _wipe(settings)
    assert code == 0, err
    assert "wiped: 1" in out
    assert "nothing to wipe: 1" in out
    assert "mails sent: 1" in out
    assert "KEK check rows deleted:" in out
    assert "CHECKPOINT" in out
    assert ACCESS not in out
    assert ACCESS not in err
    assert "wipe@example.com" not in out
    assert str(user_id) not in out

    engine = create_engine(database_url)
    with engine.connect() as conn:
        status, empty, generation = conn.execute(
            text("SELECT status, secret IS NULL, token_generation FROM connections")
        ).one()
        totp = conn.execute(text("SELECT count(*) FROM mfa_totp")).scalar_one()
        checks = conn.execute(text("SELECT count(*) FROM connection_keks")).scalar_one()
        detail = conn.execute(
            text("SELECT detail::text FROM auth_events WHERE kind = 'connections_wiped'")
        ).scalar_one()
        rows = conn.execute(text("SELECT count(*) FROM connections")).scalar_one()
    engine.dispose()
    assert (status, empty, generation, rows) == ("needs_reconnect", True, info.token_generation, 1)
    assert totp == 0
    assert checks == 0
    assert ACCESS not in detail
    assert '"test"' in detail
    assert len(smtp.messages) == 1
    assert ACCESS not in smtp.messages[0].text
    assert "wipe@example.com" not in smtp.messages[0].text
    assert smtp.messages[0].subject == "Reconnect your ownvitals account"


async def test_an_unsent_wipe_mail_can_be_resent(
    env: Env,
    app_database_url: str,
    owner_database_url: str,
    smtp: SmtpFake,
) -> None:
    user_id = await env.user("resend@example.com")
    await connect(
        env.factory, env.app.state.keyring, make_context(user_id), "test", ACCESS, REFRESH
    )
    await _stop_web(env)
    code, out, err = await _wipe(_settings(app_database_url, owner_database_url))
    assert code == 0, err
    assert "mails unsent: 1" in out
    assert smtp.messages == []

    mailed = _settings(
        app_database_url,
        owner_database_url,
        smtp_host="127.0.0.1",
        smtp_port=smtp.port,
        smtp_tls="none",
    )
    out, err = StringIO(), StringIO()
    code = await wipe(
        argparse.Namespace(action="wipe", confirm=False, resend=True), mailed, out, err
    )
    assert code == 0, err.getvalue()
    assert "1 sent" in out.getvalue()
    assert len(smtp.messages) == 1
    assert ACCESS not in smtp.messages[0].text
