"""Derived keys (ADR 0003 section 1, "Derived keys") and email normalization. TOTP seeds are on
the envelope (``test_totp_envelope.py``), so no TOTP key is derived any more."""

from pathlib import Path

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from authtools import APP_SECRET
from ownvitals.auth.keys import Keys, derive_key, hmac_digest
from ownvitals.config import Settings
from ownvitals.emailnorm import normalize_email

SECRET = APP_SECRET.encode()


def test_keys_are_hkdf_sha256_with_the_fixed_salt_and_their_own_info() -> None:
    expected = HKDF(
        algorithm=hashes.SHA256(), length=32, salt=b"ownvitals-v1", info=b"csrf"
    ).derive(SECRET)
    keys = Keys.from_secret(SECRET)
    assert keys.csrf == expected
    assert derive_key(SECRET, "csrf") == expected


def test_each_purpose_gets_a_different_32_byte_key() -> None:
    keys = Keys.from_secret(SECRET)
    values = [
        keys.csrf,
        keys.rate_limit,
        keys.device,
        keys.recovery_codes,
        keys.totp_pending,
    ]
    assert len(set(values)) == 5
    assert all(len(v) == 32 for v in values)
    assert not hasattr(keys, "totp")  # classbeat's totp-v1 key is not ported (ADR 0003 section 5)
    assert keys.totp_pending == derive_key(SECRET, "totp-pending")
    assert keys.recovery_codes == derive_key(SECRET, "recovery-codes")
    assert keys.rate_limit == derive_key(SECRET, "rate-limit")
    assert keys.device == derive_key(SECRET, "device")
    assert Keys.from_secret(SECRET + b"x").csrf != keys.csrf


def test_keys_never_show_material() -> None:
    keys = Keys.from_secret(SECRET)
    assert repr(keys) == "Keys(...)"
    assert keys.csrf.hex() not in repr(keys) + str(keys)


def test_a_short_secret_is_refused() -> None:
    with pytest.raises(ValueError, match="at least 32 bytes"):
        Keys.from_secret(b"short")
    with pytest.raises(ValueError, match="at least 32"):
        Settings(app_secret_key="short")  # pyright: ignore[reportArgumentType]


def test_a_missing_secret_stops_startup_with_a_clear_message(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("OWNVITALS_SECRETS_DIR", str(tmp_path))  # no app_secret_key file
    with pytest.raises(RuntimeError, match="app_secret_key"):
        Keys.from_settings(Settings())
    assert Keys.from_settings(Settings(app_secret_key=APP_SECRET)).csrf  # pyright: ignore[reportArgumentType]


def test_the_secret_is_read_only_from_its_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setenv("OWNVITALS_SECRETS_DIR", str(empty))  # no app_secret_key file
    monkeypatch.setenv("OWNVITALS_APP_SECRET_KEY", "env-" * 20)
    assert Settings().app_secret_key is None
    (tmp_path / "app_secret_key").write_text(APP_SECRET + "\n")  # pyright: ignore[reportOperatorIssue]
    monkeypatch.setenv("OWNVITALS_SECRETS_DIR", str(tmp_path))
    settings = Settings()
    assert settings.app_secret_key is not None
    assert settings.app_secret_key.get_secret_value() == APP_SECRET
    assert APP_SECRET not in repr(settings)


def test_hmac_parts_are_not_ambiguous() -> None:
    key = b"k" * 32
    assert hmac_digest(key, "a|b", "c") != hmac_digest(key, "a", "b|c")
    assert hmac_digest(key, "a", "b") == hmac_digest(key, "a", "b")


@pytest.mark.parametrize(
    ("raw", "normalized"),
    [
        ("  User@Example.COM ", "user@example.com"),
        ("Straße@Example.com", "straße@example.com"),  # ß is kept, never folded to ss
        ("İ@example.com", "İ@example.com"),  # non-ASCII letters are kept as typed
        ("ÅNGSTRÖM@x.se", "ÅngstrÖm@x.se"),
    ],
)
def test_email_normalization_lowercases_ascii_only(raw: str, normalized: str) -> None:
    assert normalize_email(raw) == normalized


def test_admin_emails_are_compared_in_the_normalized_form() -> None:
    settings = Settings(admin_emails=" Owner@Example.com ,, other@example.com")
    assert settings.admin_email_set == {"owner@example.com", "other@example.com"}
