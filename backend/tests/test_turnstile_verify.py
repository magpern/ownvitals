"""Turnstile's settings, verdict and CSP (ADR 0018). No database, no network: the siteverify
answers come from an ``httpx.MockTransport`` under the egress checks, or from a fake sender.
Every key and token is synthetic."""

import json
import logging
import urllib.request
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from ownvitals.auth.turnstile import CloudflareVerifier, build_verifier, session_config
from ownvitals.auth.turnstile_policy import passed
from ownvitals.config import Settings
from ownvitals.egress import TRANSPORT_ERRORS, core, verify_turnstile_token
from ownvitals.headers import (
    CONTENT_SECURITY_POLICY,
    TURNSTILE_CONTENT_SECURITY_POLICY,
    content_security_policy,
)

HOST = "ownvitals.test"
SITE_KEY = "synthetic-site-key"
SECRET = "synthetic-turnstile-secret"
TOKEN = "synthetic.token"
ON: dict[str, Any] = {
    "turnstile_enabled": True,
    "turnstile_site_key": SITE_KEY,
    "turnstile_secret_key": SECRET,
}
GOOD = {"success": True, "hostname": HOST, "action": "signin", "error-codes": []}


@pytest.fixture(autouse=True)
def _fresh_egress(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(urllib.request, "_opener", None)
    monkeypatch.setattr(core, "_worker_list", None)


# --- settings and the install check ------------------------------------------------------------


def test_it_is_off_by_default_and_then_builds_nothing() -> None:
    settings = Settings()
    assert settings.turnstile_enabled is False
    assert build_verifier(settings) is None
    assert session_config(settings) is None


@pytest.mark.parametrize(
    ("values", "field"),
    [
        ({"turnstile_enabled": True}, "turnstile_site_key"),
        ({"turnstile_enabled": True, "turnstile_site_key": SITE_KEY}, "turnstile_secret_key"),
        ({"turnstile_enabled": True, "turnstile_secret_key": SECRET}, "turnstile_site_key"),
        ({**ON, "turnstile_secret_key": ""}, "turnstile_secret_key"),
    ],
)
def test_on_without_both_keys_refuses_to_start(values: dict[str, Any], field: str) -> None:
    with pytest.raises(ValidationError) as raised:
        Settings(**values)
    assert field in str(raised.value)
    assert SECRET not in str(raised.value)


@pytest.mark.parametrize("secret", ["has space", "a&b", "a=b", "a;b", "a#b", "x" * 257, "ä"])
def test_an_unusable_secret_refuses_to_start_without_showing_it(secret: str) -> None:
    with pytest.raises(ValidationError) as raised:
        Settings(**{**ON, "turnstile_secret_key": secret})
    assert "turnstile_secret_key" in str(raised.value)
    assert secret not in str(raised.value)


@pytest.mark.parametrize("site_key", ["", "a b", "<script>", "x" * 129, "a/b"])
def test_a_malformed_site_key_is_refused(site_key: str) -> None:
    with pytest.raises(ValidationError):
        Settings(**{**ON, "turnstile_site_key": site_key})


def test_the_secret_comes_only_from_its_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("OWNVITALS_TURNSTILE_ENABLED", "true")
    monkeypatch.setenv("OWNVITALS_TURNSTILE_SITE_KEY", SITE_KEY)
    monkeypatch.setenv("OWNVITALS_TURNSTILE_SECRET_KEY", SECRET)  # never read
    monkeypatch.setenv("OWNVITALS_SECRETS_DIR", str(tmp_path))
    with pytest.raises(ValidationError, match="turnstile_secret_key"):
        Settings()
    (tmp_path / "turnstile_secret_key").write_text(SECRET + "\n", encoding="utf-8")
    settings = Settings()
    assert settings.turnstile_secret_key is not None
    assert settings.turnstile_secret_key.get_secret_value() == SECRET
    assert SECRET not in repr(settings)
    assert SECRET not in str(settings.model_dump())
    verifier = build_verifier(settings)
    assert verifier is not None
    assert SECRET not in repr(verifier)


def test_on_a_public_base_url_without_a_host_is_refused() -> None:
    settings = Settings(**ON)
    settings.public_base_url = "https://"  # no host: nothing a siteverify answer could match
    with pytest.raises(ValueError, match="public_base_url"):
        build_verifier(settings)


def test_on_it_builds_a_verifier_for_the_public_host_and_a_page_config() -> None:
    settings = Settings(**ON, public_base_url=f"https://{HOST}")
    verifier = build_verifier(settings)
    assert verifier is not None
    assert verifier.hostname == HOST
    config = session_config(settings)
    assert config is not None
    assert config.site_key == SITE_KEY
    assert config.script_url == (
        "https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit"
    )


# --- the verdict (pure) ------------------------------------------------------------------------


def body(**changes: Any) -> bytes:
    return json.dumps({**GOOD, **changes}).encode()


def test_only_a_complete_success_for_this_host_and_form_passes() -> None:
    assert passed(200, body(), hostname=HOST, action="signin")
    assert passed(200, body(hostname="OwnVitals.Test"), hostname=HOST, action="signin")


@pytest.mark.parametrize(
    ("status", "raw"),
    [
        (200, body(success=False, **{"error-codes": ["invalid-input-response"]})),  # invalid
        (200, body(success=False, **{"error-codes": ["timeout-or-duplicate"]})),  # expired
        (200, body(success="true")),
        (200, body(success=1)),
        (200, body(hostname="evil.example")),
        (200, body(hostname=None)),
        (200, body(action="signup")),  # a token solved on another form
        (200, json.dumps({"hostname": HOST, "action": "signin"}).encode()),
        (200, b"not json"),
        (200, b"\xff\xfe"),
        (200, b"[true]"),
        (200, b" " * 20_000),
        (500, body()),
        (302, body()),
    ],
)
def test_anything_else_fails_closed(status: int, raw: bytes) -> None:
    assert not passed(status, raw, hostname=HOST, action="signin")


def test_an_unknown_action_never_passes() -> None:
    assert not passed(200, body(action="other"), hostname=HOST, action="other")  # type: ignore[arg-type]  # pyright: ignore[reportArgumentType]


# --- the verifier over the egress call (fake transport) -----------------------------------------


def over(handle: Any) -> CloudflareVerifier:
    async def post(secret: str, token: str) -> httpx.Response:
        return await verify_turnstile_token(
            secret=secret, token=token, inner=httpx.MockTransport(handle)
        )

    return CloudflareVerifier(SECRET, HOST, post)


async def test_a_good_answer_passes_and_sends_the_token() -> None:
    seen: list[bytes] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request.content)
        return httpx.Response(200, json=GOOD)

    assert await over(handle).verify(TOKEN, "signin")
    assert seen == [f"secret={SECRET}&response={TOKEN}".encode()]


@pytest.mark.parametrize(
    "error",
    [
        httpx.ReadTimeout("synthetic"),
        httpx.ConnectTimeout("synthetic"),
        httpx.ConnectError("synthetic"),
        RuntimeError("synthetic"),
    ],
)
async def test_a_timeout_or_transport_error_fails_closed(
    error: Exception, caplog: pytest.LogCaptureFixture
) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        raise error

    caplog.set_level(logging.WARNING)
    assert not await over(handle).verify(TOKEN, "signin")
    assert any(r.getMessage() == "turnstile check failed" for r in caplog.records)
    assert TOKEN not in caplog.text
    assert SECRET not in caplog.text


async def test_the_total_deadline_is_a_timeout_and_fails_closed() -> None:
    async def slow(secret: str, token: str) -> httpx.Response:
        raise TRANSPORT_ERRORS[-1]("synthetic deadline")

    assert not await CloudflareVerifier(SECRET, HOST, slow).verify(TOKEN, "signin")


async def test_a_token_egress_refuses_to_send_fails_closed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    sent: list[httpx.Request] = []
    assert not await over(sent.append).verify("bad token", "signin")
    assert sent == []
    assert "bad token" not in caplog.text


async def test_a_refused_answer_logs_the_outcome_only(caplog: pytest.LogCaptureFixture) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={**GOOD, "success": False})

    caplog.set_level(logging.WARNING)
    assert not await over(handle).verify(TOKEN, "signin")
    [record] = [r for r in caplog.records if r.getMessage() == "turnstile check failed"]
    assert record.__dict__["outcome"] == "refused"
    assert TOKEN not in caplog.text
    assert SECRET not in caplog.text


# --- the CSP -----------------------------------------------------------------------------------


class _App:
    def __init__(self, settings: object) -> None:
        self.state = type("State", (), {"settings": settings})()


def directives(policy: str) -> dict[str, list[str]]:
    parsed: dict[str, list[str]] = {}
    for part in policy.split(";"):
        name, *values = part.split()
        parsed[name] = values
    return parsed


def test_the_csp_is_unchanged_while_it_is_off() -> None:
    assert content_security_policy({"app": _App(Settings())}) == CONTENT_SECURITY_POLICY
    assert content_security_policy({}) == CONTENT_SECURITY_POLICY
    assert content_security_policy({"app": _App(None)}) == CONTENT_SECURITY_POLICY
    assert "://" not in CONTENT_SECURITY_POLICY


def test_on_only_script_and_frame_gain_the_turnstile_origin() -> None:
    policy = content_security_policy({"app": _App(Settings(**ON))})
    assert policy == TURNSTILE_CONTENT_SECURITY_POLICY
    on, off = directives(policy), directives(CONTENT_SECURITY_POLICY)
    origin = "https://challenges.cloudflare.com"
    assert on["script-src"] == ["'self'", origin]
    assert on["frame-src"] == [origin]
    assert {k: v for k, v in on.items() if k not in ("script-src", "frame-src")} == {
        k: v for k, v in off.items() if k not in ("script-src", "frame-src")
    }
    assert "unsafe" not in policy
    assert "*" not in policy
