"""The operator commands (ADR 0003 section 7) and the functions behind them."""

import argparse
import hashlib
import io
from datetime import timedelta
from typing import Any

import pytest

from authtools import (  # noqa: F401 - fixtures
    ADMIN_EMAIL,
    Env,
    auth_schema_fixture,
    clean_auth_fixture,
    env_fixture,
    make_settings,
)
from ownvitals.__main__ import main
from ownvitals.auth.accounts import grant_admin_if_listed
from ownvitals.auth.devices import remember_device
from ownvitals.auth.hooks import register_reset_link_sender, send_reset_link
from ownvitals.cli import add_subcommands, parse_duration, run_command


def parse(*argv: str) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    add_subcommands(parser)
    return parser.parse_args(argv)


def test_without_a_subcommand_the_server_arguments_still_parse() -> None:
    args = parse("--host", "0.0.0.0", "--port", "9000")  # noqa: S104
    assert (args.command, args.host, args.port) == (None, "0.0.0.0", 9000)  # noqa: S104


def test_the_subcommands_parse() -> None:
    assert parse("invite", "create").expires == timedelta(days=7)
    assert parse("invite", "create", "--expires", "12h").expires == timedelta(hours=12)
    for action in ("grant", "revoke", "mfa-reset"):
        args = parse("admin", action, "a@example.com")
        assert (args.command, args.action, args.email) == ("admin", action, "a@example.com")


@pytest.mark.parametrize("bad", ["", "7", "d", "7w", "0m", "400d", "-1d"])
def test_bad_durations_are_refused(bad: str) -> None:
    with pytest.raises(argparse.ArgumentTypeError):
        parse_duration(bad)


@pytest.fixture
def cli(env: Env, app_database_url: str) -> Any:
    """Run a command as the CLI does (its own engine, as the app role)."""
    settings = make_settings(app_database_url, admin_emails=ADMIN_EMAIL)

    def run(*argv: str, settings: Any = settings) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        code = run_command(parse(*argv), settings, out, err)
        return code, out.getvalue(), err.getvalue()

    return run


async def test_invite_create_prints_one_code_and_stores_only_its_hash(env: Env, cli: Any) -> None:
    import asyncio

    code, out, err = await asyncio.to_thread(cli, "invite", "create", "--expires", "2d")
    assert code == 0
    invite = out.strip()
    assert len(invite) >= 22  # 16 random bytes, url-safe
    assert invite not in err
    row = (
        await env.scalar("SELECT code_hash FROM invites"),
        await env.scalar("SELECT created_by FROM invites"),
        await env.scalar("SELECT expires_at - now() > interval '1 day 23 hours' FROM invites"),
    )
    assert row == (hashlib.sha256(invite.encode()).digest(), None, True)
    assert invite.encode() not in bytes(row[0])  # pyright: ignore[reportArgumentType]


async def test_two_invites_differ(env: Env, cli: Any) -> None:
    import asyncio

    first = (await asyncio.to_thread(cli, "invite", "create"))[1]
    second = (await asyncio.to_thread(cli, "invite", "create"))[1]
    assert first != second


async def test_admin_grant_sets_the_role_revokes_sessions_and_writes_an_event(
    env: Env, cli: Any
) -> None:
    import asyncio

    user_id = await env.user("Grantee@Example.com")
    await env.login(user_id)
    await env.login(user_id)
    code, _, err = await asyncio.to_thread(cli, "admin", "grant", "grantee@EXAMPLE.com")
    assert code == 0
    assert "granted" in err
    assert await env.scalar("SELECT role FROM users") == "admin"
    assert await env.scalar("SELECT count(*) FROM sessions") == 0
    assert await env.scalar("SELECT kind FROM auth_events WHERE user_id = :u", u=user_id) == (
        "admin_granted"
    )
    again = await asyncio.to_thread(cli, "admin", "grant", "grantee@example.com")
    assert again[0] == 0
    assert "already" in again[2]
    assert await env.scalar("SELECT count(*) FROM auth_events") == 1


async def test_admin_grant_for_an_unknown_user_fails_cleanly(env: Env, cli: Any) -> None:
    import asyncio

    code, out, err = await asyncio.to_thread(cli, "admin", "grant", "nobody@example.com")
    assert code == 1
    assert out == ""
    assert "no user" in err


async def test_admin_revoke_demotes_and_revokes_sessions(env: Env, cli: Any) -> None:
    import asyncio

    user_id = await env.user("demote@example.com", role="admin")
    await env.login(user_id, mfa=True)
    code, _, _ = await asyncio.to_thread(cli, "admin", "revoke", "demote@example.com")
    assert code == 0
    assert await env.scalar("SELECT role FROM users") == "user"
    assert await env.scalar("SELECT count(*) FROM sessions") == 0
    assert await env.scalar("SELECT kind FROM auth_events") == "admin_revoked"


async def test_admin_revoke_refuses_while_the_address_is_in_admin_emails(
    env: Env, cli: Any
) -> None:
    import asyncio

    await env.user(ADMIN_EMAIL, role="admin")
    code, _, err = await asyncio.to_thread(cli, "admin", "revoke", ADMIN_EMAIL.lower())
    assert code == 1
    assert "ADMIN_EMAILS" in err
    assert await env.scalar("SELECT role FROM users") == "admin"


async def test_mfa_reset_removes_totp_and_recovery_codes_revokes_sessions_and_mails_a_link(
    env: Env, cli: Any, app_database_url: str
) -> None:
    import asyncio

    from smtpfake import SmtpFake

    user_id = await env.user("lost@example.com", role="admin")
    other = await env.user("other@example.com")
    await env.login(user_id, mfa=True)
    await env.run(
        "INSERT INTO mfa_totp (user_id, secret_ciphertext, nonce, key_id, wrapped_dek, "
        "dek_nonce) SELECT id, 'x', decode(repeat('00', 12), 'hex'), 'kek-1', "
        "decode(repeat('00', 48), 'hex'), decode(repeat('00', 12), 'hex') FROM users"
    )
    await env.run("INSERT INTO mfa_recovery_codes (user_id, code_hash) SELECT id, 'h' FROM users")
    async with env.db() as db:
        await remember_device(db, env.app.state.keys, user_id, None)
    async with SmtpFake() as smtp:
        settings = make_settings(
            app_database_url, smtp_host="127.0.0.1", smtp_port=smtp.port, smtp_tls="none"
        )
        code, _, err = await asyncio.to_thread(
            cli, "admin", "mfa-reset", "lost@example.com", settings=settings
        )
    assert code == 0
    assert "reset link mailed" in err
    [mail] = smtp.to("lost@example.com")  # the registered sender mailed the reset link
    assert "/reset#token=" in mail.text
    assert await env.scalar("SELECT purpose FROM auth_tokens") == "reset_password"
    assert await env.scalar("SELECT count(*) FROM mfa_totp WHERE user_id = :u", u=user_id) == 0
    assert (
        await env.scalar("SELECT count(*) FROM mfa_recovery_codes WHERE user_id = :u", u=user_id)
        == 0
    )
    assert await env.scalar("SELECT count(*) FROM sessions") == 0
    assert await env.scalar("SELECT count(*) FROM known_devices") == 0
    assert (
        await env.scalar("SELECT kind FROM auth_events WHERE user_id = :u", u=user_id)
        == "mfa_reset"
    )
    assert await env.scalar("SELECT role FROM users WHERE id = :u", u=other) == "user"


async def test_mfa_reset_says_so_when_the_link_cannot_be_mailed(env: Env, cli: Any) -> None:
    import asyncio

    await env.user("lost@example.com")
    code, _, err = await asyncio.to_thread(cli, "admin", "mfa-reset", "lost@example.com")
    assert code == 1  # no smtp_host: the database part is done, the mail is not
    assert "NOT mailed" in err
    assert "lost@example.com" not in err


async def test_send_reset_link_calls_a_registered_sender(env: Env) -> None:
    sent: list[tuple[object, str]] = []

    async def sender(user_id: Any, email: str) -> None:
        sent.append((user_id, email))

    user_id = await env.user("lost@example.com")
    register_reset_link_sender(sender)
    try:
        assert await send_reset_link(user_id, "lost@example.com") is True
    finally:
        register_reset_link_sender(None)
    assert sent == [(user_id, "lost@example.com")]


async def test_the_admin_emails_bootstrap_grants_at_sign_in(env: Env) -> None:
    listed = await env.user(ADMIN_EMAIL)
    plain = await env.user("plain@example.com")
    settings = make_settings("postgresql+psycopg://x@h/d", admin_emails=ADMIN_EMAIL.upper())
    await env.login(listed)
    async with env.db() as db:
        assert await grant_admin_if_listed(db, settings, listed) is True
        assert await grant_admin_if_listed(db, settings, plain) is False
    assert await env.scalar("SELECT role FROM users WHERE id = :u", u=listed) == "admin"
    assert await env.scalar("SELECT count(*) FROM sessions") == 0  # the sign-in makes a new one
    async with env.db() as db:
        assert await grant_admin_if_listed(db, settings, listed) is False


def test_main_runs_a_subcommand_instead_of_the_server(
    monkeypatch: pytest.MonkeyPatch, app_database_url: str
) -> None:
    import uvicorn

    called: list[Any] = []
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: called.append(k))
    monkeypatch.setattr("ownvitals.__main__.run_command", lambda args, settings: 7)
    monkeypatch.setattr("ownvitals.__main__.configure_logging", lambda level: None)
    assert main(["admin", "grant", "a@example.com"]) == 7
    assert called == []
