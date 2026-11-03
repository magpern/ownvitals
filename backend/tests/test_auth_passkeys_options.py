"""Passkey options, settings and the import rule (ADR 0011 sections 1 and 3).

The options are the ADR's, one by one: discoverable credentials, user verification required,
attestation ``none``, no attachment restriction and no ``hints`` (so the hybrid transport,
"Use a phone", stays available), ES256, EdDSA and RS256 in that order, 120 seconds, and a user
handle that is neither the user id nor the email. Synthetic accounts only.
"""

import ast
from pathlib import Path

import pytest
from pydantic import ValidationError

from authtools import (  # noqa: F401 - fixtures
    ORIGIN,
    Env,
    auth_schema_fixture,
    clean_auth_fixture,
    env_fixture,
    make_settings,
)
from flows import Browser, only_user_id, signed_in
from passkeytools import RP_ID, register_options, signin_options, unb64

SRC = Path(__file__).resolve().parent.parent / "src"
URL = "postgresql+psycopg://app@127.0.0.1/x"


async def test_registration_options(env: Env) -> None:
    browser = await signed_in(env)
    options = await register_options(browser)
    user_id = await only_user_id(env)
    assert options["rp"] == {"id": RP_ID, "name": "ownvitals"}
    assert options["attestation"] == "none"
    assert options["authenticatorSelection"] == {
        "residentKey": "required",
        "requireResidentKey": True,
        "userVerification": "required",
    }
    assert "authenticatorAttachment" not in options["authenticatorSelection"]
    assert "hints" not in options or options["hints"] == []
    assert [p["alg"] for p in options["pubKeyCredParams"]] == [-7, -8, -257]
    assert options["timeout"] == 120_000
    assert options["excludeCredentials"] == []
    handle = unb64(options["user"]["id"])
    assert len(handle) == 32
    assert handle not in (user_id.bytes, str(user_id).encode(), b"user@example.com")
    assert options["user"]["name"] == options["user"]["displayName"] == "user@example.com"
    stored = await env.scalar("SELECT webauthn_handle FROM users")
    assert bytes(stored) == handle  # pyright: ignore[reportArgumentType]
    again = await register_options(browser)
    assert again["user"]["id"] == options["user"]["id"]  # created once, then kept
    assert len(unb64(again["challenge"])) == 32
    assert again["challenge"] != options["challenge"]
    assert await env.scalar("SELECT count(*) FROM webauthn_challenges") == 1  # replaced


async def test_sign_in_options(env: Env) -> None:
    options = await signin_options(Browser(env))
    assert options["rpId"] == RP_ID
    assert options["userVerification"] == "required"
    assert options["timeout"] == 120_000
    assert options.get("allowCredentials", []) == []
    assert "hints" not in options or options["hints"] == []
    assert len(unb64(options["challenge"])) == 32
    row = await env.row(
        "SELECT purpose, user_id, session_id, presession_id IS NOT NULL AS bound, "
        "expires_at - created_at AS life FROM webauthn_challenges"
    )
    assert (row.purpose, row.user_id, row.session_id, row.bound) == ("signin", None, None, True)
    assert row.life.total_seconds() == 300


def test_the_relying_party_follows_the_public_base_url() -> None:
    assert make_settings(URL).passkey_rp_id == RP_ID
    assert f"https://{RP_ID}" == ORIGIN
    local = make_settings(URL, public_base_url="http://localhost:5173")
    assert local.passkey_rp_id == "localhost"
    assert make_settings(URL, public_base_url="http://127.0.0.1:8000").passkey_rp_id is None
    sub = make_settings(URL, public_base_url="https://app.ownvitals.test")
    assert (
        make_settings(
            URL, public_base_url="https://app.ownvitals.test", webauthn_rp_id=RP_ID
        ).passkey_rp_id
        == RP_ID
    )
    assert sub.passkey_rp_id == "app.ownvitals.test"


@pytest.mark.parametrize(
    "rp_id",
    ["evil.test", "test", "vitals.test", "OWNVITALS.TEST", "", "203.0.113.5", "x.ownvitals.test"],
)
def test_a_foreign_relying_party_id_is_refused(rp_id: str) -> None:
    with pytest.raises(ValidationError):
        make_settings(URL, webauthn_rp_id=rp_id)


def test_a_passkey_as_second_factor_is_refused_until_the_owner_decides() -> None:
    assert make_settings(URL).passkey_satisfies_second_factor is False
    with pytest.raises(ValidationError, match="open question 1"):
        make_settings(URL, passkey_satisfies_second_factor=True)


def test_the_passkey_limit_is_ten_at_most() -> None:
    assert make_settings(URL).passkeys_per_account == 10
    with pytest.raises(ValidationError):
        make_settings(URL, passkeys_per_account=11)


def test_only_ownvitals_auth_imports_webauthn() -> None:
    """ADR 0011 section 1: the WebAuthn library stays inside ``ownvitals.auth``."""
    importers = []
    for path in sorted((SRC / "ownvitals").rglob("*.py")):
        tree = ast.parse(path.read_text("utf-8"))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                names = [node.value] if node.value.startswith("webauthn") else []
            if any(n == "webauthn" or n.startswith("webauthn.") for n in names):
                importers.append(path.relative_to(SRC).as_posix())
    assert importers
    assert {p for p in importers if not p.startswith("ownvitals/auth/")} == set()
    assert set(importers) == {"ownvitals/auth/passkey_ceremonies.py"}
