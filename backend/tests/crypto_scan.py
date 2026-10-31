"""Guards of ADR 0004: who may use the envelope module, cryptographic primitives and the key and
connection tables, and which column types a secret may have.

Like the session guard (``session_scan``) the source scan catches accidental misuse and makes
misuse visible in review; it cannot stop obfuscated code (computed names, ``exec``,
``sys.modules``).

``scan_source(source, module)`` returns what ``module`` (a dotted name under ``ownvitals``) may
not do:

- import ``ownvitals.crypto.envelope`` (any form: ``import``, ``from ... import``, relative,
  ``from ownvitals.crypto import envelope``, or the dotted name in a string, as
  ``importlib.import_module`` takes it), or a cryptographic primitive package (``cryptography``
  and the other names of ``PRIMITIVE_PACKAGES``, in the same forms), unless it is in
  ``ownvitals.connections`` or ``ownvitals.auth``, or is ``ownvitals.crypto.keks`` or the
  envelope itself (ADR 0004 section 2). The module that holds the Withings app secret joins in
  M1;
- import the ``connection_keks`` table or ``metadata`` from ``ownvitals.crypto.keks``, reach them
  as an attribute of ``keks``, or hold an SQL string naming ``connection_keks`` (only
  ``ownvitals.crypto.keks`` may);
- import ``ownvitals.connections.tables`` or its ``connections`` table, or hold an SQL string
  naming the ``connections`` table, outside ``ownvitals.connections`` (ADR 0004 section 5; the
  table arrives with #13).

``secret_column_problems(columns)`` (and ``check_secret_columns`` on a live database) returns
the columns of ``public`` whose name says they hold a secret but whose type is not ``bytea``,
the type of a ciphertext. A name holds a secret when one of its ``_``-separated words is in
``SECRET_WORDS``, unless its last word says it is about a secret rather than one
(``NOT_A_SECRET_LAST_WORDS``: ``token_generation``, ``secret_id``, ``password_hash``,
``secret_nonce``), or the column is listed with its reason in ``NOT_SECRET_COLUMNS``. A ``bytea``
column passes on its type alone: the check catches a plaintext column type, not a plaintext
value written to a ``bytea`` column (that is the envelope's tests and review).
"""

import ast
import re
from collections.abc import Iterable

from sqlalchemy import text
from sqlalchemy.engine import Connection

ENVELOPE = "ownvitals.crypto.envelope"
KEKS = "ownvitals.crypto.keks"
CONNECTION_TABLES = "ownvitals.connections.tables"
# Who may import the envelope or a primitive package: packages, and exact modules.
CRYPTO_PACKAGES = ("ownvitals.connections", "ownvitals.auth")
CRYPTO_MODULES = (KEKS, ENVELOPE)
PRIMITIVE_PACKAGES = ("cryptography", "Crypto", "Cryptodome", "nacl")

_SQL = r"\b(?:select|insert|update|delete|copy|truncate|alter|lock|drop|vacuum|grant|revoke)\b"
_KEYWORD = r"\b(?:from|join|into|update|table|copy|truncate|lock|vacuum|on|\(full\))\s+"
_SCHEMA = r"(?:\"?public\"?\s*\.\s*)?\"?"


def _sql_naming(table: str) -> re.Pattern[str]:
    return re.compile(rf"(?is)(?=.*{_SQL}).*{_KEYWORD}(?:\(full\)\s+)?{_SCHEMA}{table}\"?(?![\w$])")


KEK_SQL = _sql_naming("connection_keks")
CONNECTIONS_SQL = _sql_naming("connections")

SECRET_WORDS = frozenset(
    {
        "token",
        "tokens",
        "secret",
        "secrets",
        "password",
        "passwords",
        "passwd",
        "passphrase",
        "seed",
        "seeds",
        "pepper",
        "peppers",
        "kek",
        "keks",
        "dek",
        "otp",
        "totp",
        "credential",
        "credentials",
        "apikey",
        "privkey",
    }
)
NOT_A_SECRET_LAST_WORDS = frozenset(
    {"id", "ids", "hash", "hmac", "generation", "nonce", "at", "count", "kind", "version"}
)
CIPHERTEXT_TYPE = "bytea"
# (table, column) -> why a column with a secret word in its name holds no secret. M1's
# ``withings_apps.webhook_secrets`` (HMACs and ids only, ADR 0004 section 4) is added by #21.
NOT_SECRET_COLUMNS: dict[tuple[str, str], str] = {}


def _inside(module: str, package: str) -> bool:
    return module == package or module.startswith(package + ".")


def _is_primitive(name: str) -> bool:
    return any(_inside(name, package) for package in PRIMITIVE_PACKAGES)


def _resolve(node: ast.ImportFrom, module: str) -> str:
    if not node.level:
        return node.module or ""
    parts = module.split(".")
    base = parts[: len(parts) - node.level]
    return ".".join([*base, node.module] if node.module else base)


def _strings(tree: ast.AST) -> list[tuple[int, str]]:
    docstrings: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                docstrings.add(id(body[0].value))
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            text = "".join(
                v.value if isinstance(v, ast.Constant) and isinstance(v.value, str) else "?"
                for v in node.values
            )
            found.append((node.lineno, text))
        elif (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstrings
        ):
            found.append((node.lineno, node.value))
    return found


def may_use_crypto(module: str) -> bool:
    return module in CRYPTO_MODULES or any(_inside(module, p) for p in CRYPTO_PACKAGES)


def scan_source(source: str, module: str) -> list[tuple[int, str]]:
    tree = ast.parse(source)
    may_crypto = may_use_crypto(module)
    may_keks_table = module == KEKS
    may_connections_table = _inside(module, "ownvitals.connections")
    keks_aliases: set[str] = set()
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if not may_crypto and alias.name == ENVELOPE:
                    found.append((node.lineno, "imports the envelope module"))
                if not may_crypto and _is_primitive(alias.name):
                    found.append((node.lineno, "imports a cryptographic primitive package"))
                if alias.name == CONNECTION_TABLES and not may_connections_table:
                    found.append((node.lineno, "imports the connections table module"))
                if alias.name == KEKS:
                    keks_aliases.add(alias.asname or alias.name)
        elif isinstance(node, ast.ImportFrom):
            source_module = _resolve(node, module)
            names = {alias.name for alias in node.names}
            if not may_crypto and (
                source_module == ENVELOPE
                or (source_module == "ownvitals.crypto" and names & {"envelope", "*"})
            ):
                found.append((node.lineno, "imports the envelope module"))
            if not may_crypto and _is_primitive(source_module):
                found.append((node.lineno, "imports a cryptographic primitive package"))
            if not may_connections_table and (
                source_module == CONNECTION_TABLES
                or (source_module == "ownvitals.connections" and names & {"tables", "*"})
            ):
                found.append((node.lineno, "imports the connections table module"))
            if (
                source_module == KEKS
                and not may_keks_table
                and names
                & {
                    "connection_keks",
                    "metadata",
                    "*",
                }
            ):
                found.append((node.lineno, "imports the connection_keks table"))
            if source_module == "ownvitals.crypto" and "keks" in names:
                keks_aliases |= {a.asname or a.name for a in node.names if a.name == "keks"}
        elif (
            isinstance(node, ast.Attribute)
            and node.attr in ("connection_keks", "metadata")
            and isinstance(node.value, ast.Name)
            and node.value.id in keks_aliases
            and not may_keks_table
        ):
            found.append((node.lineno, "uses the connection_keks table"))
    for line, value in _strings(tree):
        if not may_crypto and ENVELOPE in value:
            found.append((line, "names the envelope module"))
        if not may_crypto and _is_primitive(value.strip()):
            found.append((line, "names a cryptographic primitive package"))
        if not may_keks_table and KEK_SQL.search(value):
            found.append((line, "SQL on connection_keks"))
        if not may_connections_table and CONNECTIONS_SQL.search(value):
            found.append((line, "SQL on connections"))
    return found


def holds_a_secret(column: str) -> bool:
    """Whether a column's name says it holds a secret (see the module docstring)."""
    words = column.lower().split("_")
    if words[-1] in NOT_A_SECRET_LAST_WORDS:
        return False
    return any(word in SECRET_WORDS for word in words)


def secret_column_problems(columns: Iterable[tuple[str, str, str]]) -> list[str]:
    """``(table, column, data_type)`` rows -> the secret columns that are not ``bytea``."""
    return [
        f"{table}.{column} is {data_type}: a secret column holds ciphertext ({CIPHERTEXT_TYPE})"
        for table, column, data_type in columns
        if holds_a_secret(column)
        and (table, column) not in NOT_SECRET_COLUMNS
        and data_type != CIPHERTEXT_TYPE
    ]


def check_secret_columns(conn: Connection) -> list[str]:
    """``secret_column_problems`` over every column of every table, view and partition in
    ``public``, read from the catalog (``information_schema`` would hide the columns the
    connecting role has no privilege on). A domain counts as its own name, so a secret column
    typed by a domain fails even over ``bytea``."""
    rows = conn.execute(
        text(
            "SELECT c.relname AS table_name, a.attname AS column_name, "
            "format_type(a.atttypid, NULL) AS data_type "
            "FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p', 'v', 'm', 'f') "
            "AND a.attnum > 0 AND NOT a.attisdropped ORDER BY c.relname, a.attnum"
        )
    )
    return secret_column_problems((r.table_name, r.column_name, r.data_type) for r in rows)
