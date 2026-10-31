"""The ADR 0004 guards (``crypto_scan``) over the whole source tree and the migrated schema, and
proof they can fail."""

from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection

from crypto_scan import (
    NOT_SECRET_COLUMNS,
    check_secret_columns,
    holds_a_secret,
    may_use_crypto,
    scan_source,
    secret_column_problems,
)

SRC = Path(__file__).resolve().parent.parent / "src"


def _module(path: Path) -> str:
    parts = list(path.relative_to(SRC).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def test_only_the_allowed_modules_use_the_envelope_primitives_and_key_tables() -> None:
    paths = sorted((SRC / "ownvitals").rglob("*.py"))
    assert len(paths) > 10, "the scan must see the package"
    violations = [
        (_module(path), line, why)
        for path in paths
        for line, why in scan_source(path.read_text(encoding="utf-8"), _module(path))
    ]
    assert violations == []


def test_the_envelope_and_the_primitives_are_used_only_by_their_allowed_packages() -> None:
    """``auth`` (#10) and ``connections`` (#13) join ``crypto`` when they are written."""
    importers = set()
    for path in sorted((SRC / "ownvitals").rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        if "ownvitals.crypto.envelope import" in source or "from cryptography" in source:
            importers.add(_module(path))
    assert "ownvitals.crypto.keks" in importers
    assert "ownvitals.crypto.envelope" in importers
    assert all(may_use_crypto(module) for module in importers)
    assert {module.split(".")[1] for module in importers} <= {"auth", "connections", "crypto"}


@pytest.mark.parametrize(
    ("module", "allowed"),
    [
        ("ownvitals.crypto.envelope", True),
        ("ownvitals.crypto.keks", True),
        ("ownvitals.auth", True),
        ("ownvitals.auth.totp", True),
        ("ownvitals.connections.repository", True),
        ("ownvitals.crypto", False),  # the package itself: only its two modules
        ("ownvitals.crypto.other", False),
        ("ownvitals.web", False),
        ("ownvitals.jobs.sync", False),
        ("ownvitals.authx", False),
        ("ownvitals.connectionsx.repo", False),
    ],
)
def test_who_may_use_the_envelope_and_the_primitives(module: str, allowed: bool) -> None:
    assert may_use_crypto(module) is allowed


ALLOWED = [
    ("from ownvitals.crypto.envelope import seal", "ownvitals.connections.repository"),
    ("from ownvitals.crypto.envelope import seal", "ownvitals.auth.totp_keys"),
    ("from ownvitals.crypto.envelope import check_value", "ownvitals.crypto.keks"),
    ("from .envelope import seal", "ownvitals.crypto.keks"),
    ("from cryptography.hazmat.primitives.ciphers import aead", "ownvitals.crypto.envelope"),
    ("from cryptography.hazmat.primitives.kdf.hkdf import HKDF", "ownvitals.auth.keys"),
    ("import cryptography.exceptions", "ownvitals.connections.repository"),
    ("from ownvitals.crypto.keks import KeyRing, load_keyring", "ownvitals.web"),
    ("from ownvitals.crypto import keks\nkeks.load_keyring", "ownvitals.worker.main"),
    ("import hmac, hashlib, secrets", "ownvitals.web"),  # the standard library is not guarded
    ("x = 'the connections of a user'", "ownvitals.web"),
    ("x = 'connection_keks'", "ownvitals.db.tables"),
    ("x = 'cryptographic'", "ownvitals.web"),
    ("from ownvitals import connections\nconnections.save", "ownvitals.auth.deps"),
    ("x = 'SELECT 1 FROM connection_keks'", "ownvitals.crypto.keks"),
    ("x = 'DELETE FROM connections'", "ownvitals.connections.repository"),
    ("x = 'VACUUM (FULL) public.connections'", "ownvitals.connections.repository"),
    ("x = 'select the right connections from the list'", "ownvitals.web"),
    ('"""Docstrings may name ownvitals.crypto.envelope and cryptography."""', "ownvitals.web"),
]
REFUSED = [
    ("from ownvitals.crypto.envelope import seal", "ownvitals.web"),
    ("import ownvitals.crypto.envelope", "ownvitals.jobs.run"),
    ("import ownvitals.crypto.envelope as e", "ownvitals.keys_cli"),
    ("from ownvitals.crypto import envelope", "ownvitals.worker.loop"),
    ("from ownvitals.crypto import *", "ownvitals.worker.loop"),
    ("from . import envelope", "ownvitals.crypto.other"),
    ("from .crypto.envelope import open_sealed", "ownvitals.cli"),
    ("import importlib\nimportlib.import_module('ownvitals.crypto.envelope')", "ownvitals.cli"),
    # A cryptographic primitive outside the allowlist, in any form.
    ("from cryptography.hazmat.primitives.ciphers.aead import AESGCM", "ownvitals.web"),
    ("from cryptography.fernet import Fernet", "ownvitals.jobs.sync"),
    ("import cryptography", "ownvitals.admin.routes"),
    ("import cryptography.hazmat.primitives.hmac as h", "ownvitals.withings.client"),
    ("from cryptography import fernet", "ownvitals.crypto.other"),
    ("from Crypto.Cipher import AES", "ownvitals.web"),
    ("from Cryptodome.Cipher import AES", "ownvitals.web"),
    ("import nacl.secret", "ownvitals.web"),
    ("import importlib\nimportlib.import_module('cryptography.fernet')", "ownvitals.cli"),
    ("__import__('cryptography')", "ownvitals.cli"),
    # The check table and the connections table.
    ("from ownvitals.crypto.keks import connection_keks", "ownvitals.keys_cli"),
    ("from ownvitals.crypto.keks import metadata", "ownvitals.connections.repository"),
    ("from ownvitals.crypto import keks\nkeks.connection_keks", "ownvitals.keys_cli"),
    ("import ownvitals.crypto.keks as k\nk.connection_keks", "ownvitals.keys_cli"),
    ("x = 'DELETE FROM connection_keks'", "ownvitals.keys_cli"),
    ("x = 'VACUUM (FULL) connection_keks'", "ownvitals.keys_cli"),
    ('x = f"SELECT * FROM public.connection_keks WHERE key_id = {k}"', "ownvitals.web"),
    ("from ownvitals.connections.tables import connections", "ownvitals.auth.deps"),
    ("from ownvitals.connections import tables", "ownvitals.keys_cli"),
    ("import ownvitals.connections.tables", "ownvitals.web"),
    ("x = 'SELECT secret FROM connections WHERE id = :id'", "ownvitals.keys_cli"),
    ("x = 'UPDATE public.connections SET status = 1'", "ownvitals.auth.mfa"),
    ("x = 'VACUUM (FULL) public.connections'", "ownvitals.keys_cli"),
]


@pytest.mark.parametrize(("source", "module"), ALLOWED)
def test_allowed(source: str, module: str) -> None:
    assert scan_source(source, module) == []


@pytest.mark.parametrize(("source", "module"), REFUSED)
def test_refused(source: str, module: str) -> None:
    assert scan_source(source, module) != []


def test_a_planted_primitive_in_the_source_tree_fails_the_guard(tmp_path: Path) -> None:
    """The tree scan above, on a copy of one real module with a direct AES-GCM use planted."""
    web = (SRC / "ownvitals" / "web.py").read_text(encoding="utf-8")
    assert scan_source(web, "ownvitals.web") == []
    planted = web + (
        "\nfrom cryptography.hazmat.primitives.ciphers.aead import AESGCM\n"
        "def leak(key: bytes) -> bytes:\n"
        "    return AESGCM(key).encrypt(bytes(12), b'x', None)\n"
    )
    assert scan_source(planted, "ownvitals.web") == [
        (planted.count("\n") - 2, "imports a cryptographic primitive package")
    ]


# --- secret column types --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("column", "secret"),
    [
        ("access_token", True),
        ("refresh_token", True),
        ("client_secret", True),
        ("secret", True),
        ("totp_seed", True),
        ("webhook_pepper", True),
        ("password", True),
        ("wrapped_dek", True),
        ("api_credentials", True),
        ("Access_Token", True),
        ("token_generation", False),  # ADR 0004 section 1: a counter, not a token
        ("secret_id", False),
        ("seed_id", False),
        ("pepper_id", False),
        ("key_id", False),
        ("password_hash", False),
        ("token_hash", False),
        ("secret_nonce", False),
        ("dek_nonce", False),
        ("refresh_intent_at", False),
        ("check_value", False),
        ("tokenizer", False),  # whole words only
    ],
)
def test_which_column_names_hold_a_secret(column: str, secret: bool) -> None:
    assert holds_a_secret(column) is secret


def test_secret_columns_must_be_bytea() -> None:
    rows = [
        ("connections", "secret", "bytea"),
        ("connections", "wrapped_dek", "bytea"),
        ("connections", "token_generation", "bigint"),
        ("connections", "key_id", "text"),
        ("withings_apps", "client_secret", "text"),
        ("users", "totp_secret", "character varying"),
        ("t", "access_token", "jsonb"),
        ("t", "refresh_tokens", "text[]"),
    ]
    assert secret_column_problems(rows) == [
        "withings_apps.client_secret is text: a secret column holds ciphertext (bytea)",
        "users.totp_secret is character varying: a secret column holds ciphertext (bytea)",
        "t.access_token is jsonb: a secret column holds ciphertext (bytea)",
        "t.refresh_tokens is text[]: a secret column holds ciphertext (bytea)",
    ]
    assert NOT_SECRET_COLUMNS == {}  # #21 adds withings_apps.webhook_secrets with its reason


@pytest.fixture
def superuser(schema: None, database_url: str) -> Iterator[Connection]:
    """A superuser connection to the migrated schema."""
    engine = create_engine(database_url, isolation_level="AUTOCOMMIT")
    try:
        with engine.connect() as connection:
            yield connection
    finally:
        engine.dispose()


def test_the_migrated_schema_has_no_plaintext_secret_column(superuser: Connection) -> None:
    assert check_secret_columns(superuser) == []


def test_a_planted_plaintext_token_column_fails_the_guard(superuser: Connection) -> None:
    superuser.execute(
        text(
            "CREATE TABLE public.t_planted_secrets (id uuid, access_token text, "
            "client_secret bytea, token_generation bigint)"
        )
    )
    superuser.execute(text("CREATE VIEW public.v_planted_secrets AS SELECT 'x'::text AS seed"))
    try:
        assert check_secret_columns(superuser) == [
            "t_planted_secrets.access_token is text: a secret column holds ciphertext (bytea)",
            "v_planted_secrets.seed is text: a secret column holds ciphertext (bytea)",
        ]
    finally:
        superuser.execute(text("DROP VIEW public.v_planted_secrets"))
        superuser.execute(text("DROP TABLE public.t_planted_secrets"))
    assert check_secret_columns(superuser) == []
