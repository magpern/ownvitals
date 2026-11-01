"""Guard 8 of ADR 0003 section 1: only ``ownvitals.auth`` touches the account tables.

Purpose and limits. Like ADR 0002's AST guard for ``UserContext``, this catches *accidental*
misuse and makes misuse visible in review; it does not stop deliberately malicious or
obfuscated code. Known limits: rebinding through arbitrary variables or containers, computed
``getattr`` names, ``sys.modules`` lookups, ``exec``/``eval``, SQL assembled at run time from
non-constant parts (a name, a call, a file, config or the database: only the constant pieces of
an f-string or ``+`` chain are read), SQL in ``bytes`` literals (only ``str`` constants are
scanned), the two exemptions below, and anything else a static scan of one file cannot follow.
The review gate (AGENTS.md) catches those.

Exemptions, the only way to pass a string the matcher flags:

- docstrings (a string constant that is the first statement of a module, class or function body)
  are not scanned;
- any other string is exempt when the line where it starts ends with the comment
  ``# guard8: prose``. The marker counts only as a real comment token (found with ``tokenize``):
  text inside a string literal, however many lines it spans, exempts nothing, and neither does a
  comment inside an f-string's replacement field (``f"{ # guard8: prose\n x }"``, legal since
  Python 3.12): that comment sits inside the string, not after it, so it is ignored.
  ``scan_source_with_exemptions`` also returns how many strings were exempted this way (each
  string expression counts once, so two on one marked line are 2), and the whole-tree test pins
  that number, so a new exemption is visible in review.

``scan_source`` parses Python source (a module outside ``ownvitals/auth/``) and returns what it
refuses. Outside the package only the public API of ``ownvitals.auth`` may be used. In
ownvitals that API is ``PUBLIC_ENTRY_POINTS``: named functions of a few submodules, imported
with ``from <submodule> import <name>``. (Classbeat re-exported them lazily from the package
with PEP 562; the egress guard refuses dynamic imports, and an eager package would load the
web's sign-in and mail modules into the worker, so #10 names the entry points instead.)

- any import of a ``ownvitals.auth`` submodule (``from ownvitals.auth.sessions import ...``,
  ``import ownvitals.auth.purge as p``, relative forms, ``importlib.import_module`` and
  ``__import__`` of one, absolute or relative with a package), except a ``from`` import of a
  name listed for that submodule in ``PUBLIC_ENTRY_POINTS``;
- any imported name that is a table, ``metadata``, a submodule name or ``*`` from
  ``ownvitals.auth``; attribute access to such a name on ``ownvitals.auth``, on an alias of it,
  on ``<alias of ownvitals>.auth`` (``import ownvitals as c; c.auth.tables``), or directly on
  ``importlib.import_module("ownvitals.auth")`` (also when assigned to a name); and
  ``getattr(<such a root>, "<such a name>")``;
- ``vars(<root>)``, ``<root>.__dict__`` and ``pkgutil.resolve_name("ownvitals.auth...")``;
- ``table(...)`` and ``Table(...)`` (sqlalchemy) naming an auth table, as first argument or as
  ``name=``;
- ``__import__("ownvitals.auth")`` returns the top-level ``ownvitals`` package, so
  ``__import__("ownvitals.auth").auth.tables`` is followed like ``import ownvitals``;
- any string expression that is SQL: a constant, an f-string (each expression is a
  placeholder) or a ``+`` chain of strings, taken as one text, so a verb and a table in
  different pieces still count. It must contain an SQL statement verb (select, insert, update,
  delete, copy, truncate, alter, merge, with, lock, drop, create, grant, revoke) as a whole word
  and an auth table name (optionally double-quoted, with any schema qualifier) right after an
  SQL keyword (from, join, into, update, table, copy, using, truncate, lock; after ``ON`` for
  grant/revoke; comments skipped; ``ONLY`` and ``IF EXISTS`` allowed; no space is needed before
  a quoted name) or later in a ``FROM a, b`` list. Without a verb, a fragment is still refused
  when it starts with the keyword and table (``" FROM users"``) or when the table is followed by
  a clause keyword or ``;`` (``" FROM users WHERE id = :id"``). Prose such as
  "copied from the sessions of the user" or "copied from sessions" (no statement verb) is not
  flagged; "update the sessions" is not either (the table does not follow the keyword).

The worker's purge and the CLI call functions in ``ownvitals.auth``; they never get here.
"""

import ast
import io
import re
import tokenize

TABLES_MODULE = "ownvitals.auth.tables"
AUTH_PACKAGE = "ownvitals.auth"
# The public API of ``ownvitals.auth`` (#10): the only names code outside the package may import
# from its submodules, each with ``from ownvitals.auth.<module> import <name>``.
# ``test_auth_tables_guard.py`` checks each exists and none is a table or ``metadata``.
PUBLIC_ENTRY_POINTS: dict[str, frozenset[str]] = {
    # the app: middleware, routers and state (ownvitals.web)
    "ownvitals.auth.setup": frozenset({"attach_state", "detach_state", "install"}),
    # the jobs API: the signed-in user and the event stream's session check
    "ownvitals.auth.deps": frozenset({"Probe", "require_user", "session_probe"}),
    # the worker: the issuer's account check and the purge
    "ownvitals.auth.owners": frozenset({"ACCOUNTS"}),
    "ownvitals.auth.purge": frozenset({"purge_expired"}),
    # the operator CLI
    "ownvitals.auth.operator": frozenset(
        {"AccountError", "create_invite", "grant_admin", "mfa_reset", "revoke_admin"}
    ),
}
TABLE_NAMES = (
    "users",
    "identities",
    "sessions",
    "known_devices",
    "pending_signups",
    "auth_tokens",
    "oauth_flows",
    "invites",
    "mfa_totp",
    "mfa_recovery_codes",
    "rate_limits",
    "auth_events",
)
SUBMODULES = frozenset(
    {
        "accounts",
        "admin_routes",
        "clientip",
        "cookies",
        "deps",
        "devices",
        "email_routes",
        "events",
        "flowtools",
        "hooks",
        "identity_routes",
        "keys",
        "mails",
        "mfa",
        "mfa_routes",
        "middleware",
        "notices",
        "notifier",
        "operator",
        "owners",
        "oauth_client",
        "oauth_handoff",
        "oauth_idtoken",
        "oauth_outcomes",
        "oauth_providers",
        "oauth_routes",
        "oauth_store",
        "passwords",
        "purge",
        "ratelimit",
        "reset_routes",
        "routes",
        "sessions",
        "setup",
        "signin_routes",
        "signup_routes",
        "tables",
        "tokens",
        "totp",
        "totp_keys",
    }
)
FORBIDDEN_NAMES = frozenset(TABLE_NAMES) | {"metadata"} | SUBMODULES
# The matcher of the first version of this guard, unchanged: an SQL word (statement verb, FROM,
# JOIN or INTO) and an auth table name as whole words anywhere in the same text. Everything it
# flagged stays flagged; the rules below only add to it. It also flags prose such as "copied from
# sessions": such a string is exempted with ``# guard8: prose`` (see the module docstring).
BASE_VERBS = re.compile(
    r"\b(?:select|insert|update|delete|copy|truncate|alter|merge|with|lock|from|join|into)\b",
    re.IGNORECASE,
)
BASE_TABLE = re.compile(
    r"(?<![\w$])(?:\"?public\"?\s*\.\s*)?\"?(?P<name>" + "|".join(TABLE_NAMES) + r")\"?(?![\w$])",
    re.IGNORECASE,
)
# Added rules: a statement verb and the table right after a keyword or in a list; GRANT/REVOKE.
SQL_VERBS = re.compile(
    r"\b(?:select|insert|update|delete|copy|truncate|alter|merge|with|lock|drop|create|grant"
    r"|revoke)\b",
    re.IGNORECASE,
)
_GRANTS = re.compile(r"\b(?:grant|revoke)\b", re.IGNORECASE)
# A table name, optionally quoted and qualified with any schema: public.users, "s"."users".
_TABLE = r"(?:(?:\"[^\"]+\"|\w+)\s*\.\s*)?\"?(?P<name>" + "|".join(TABLE_NAMES) + r")\"?(?![\w$])"
_KEYWORD = r"(?<![\w$])(?:from|join|into|update|table|copy|using|truncate|lock)"
_GAP = r"(?:\s+|(?=\"))"  # Postgres reads FROM"users" too
_MODIFIERS = r"(?:(?:only|if\s+exists)\s+)*"
_AFTER = _GAP + _MODIFIERS + _TABLE
# A table name right after an SQL keyword.
SQL_AFTER_KEYWORD = re.compile(_KEYWORD + _AFTER, re.IGNORECASE)
# GRANT/REVOKE ... ON users
SQL_AFTER_ON = re.compile(r"(?<![\w$])on" + _AFTER, re.IGNORECASE)
# A table name later in a FROM list: ``FROM a, b x, users``.
SQL_IN_FROM_LIST = re.compile(
    r"(?<![\w$])from\s+(?:[\w\".$]+(?:\s+(?:as\s+)?[\w\"]+)?\s*,\s*)+" + _TABLE,
    re.IGNORECASE,
)
# Without a statement verb (a fragment of a statement built from pieces): the keyword and table
# at the very start, or followed by SQL continuation (a clause keyword or ``;``), not by prose.
_CONTINUATION = (
    r"(?:\s+(?:as\s+)?[\w\"]+)?\s+(?:where|on|set|values|order\s+by|group\s+by|limit|returning"
    r"|join|inner|left|right|cross|natural|full|using|select)\b|\s*;"
)
SQL_FRAGMENT_START = re.compile(_KEYWORD + _AFTER, re.IGNORECASE)
SQL_FRAGMENT_CONTINUED = re.compile(
    _KEYWORD + _AFTER + r"(?=" + _CONTINUATION + r")", re.IGNORECASE
)
_SQL_COMMENT = re.compile(r"/\*.*?\*/|--[^\n]*", re.DOTALL)
PLACEHOLDER = " {} "


def sql_table_in(text: str) -> str | None:
    """The auth table an SQL-looking string names, or None.

    ``text`` is a whole string: one constant, an f-string with a placeholder for each
    expression, or a ``+`` chain of strings joined the same way.
    """
    if BASE_VERBS.search(text) and (base := BASE_TABLE.search(text)):
        return base.group("name")
    text = _SQL_COMMENT.sub(" ", text)
    if SQL_VERBS.search(text):
        match = SQL_AFTER_KEYWORD.search(text) or SQL_IN_FROM_LIST.search(text)
        if match is None and _GRANTS.search(text):
            match = SQL_AFTER_ON.search(text)
    else:
        match = SQL_FRAGMENT_START.match(text.lstrip()) or SQL_FRAGMENT_CONTINUED.search(text)
    return match.group("name") if match else None


def _string_text(node: ast.AST) -> tuple[str, bool]:
    """``(text, has_string)`` of a string expression: constants, f-strings and ``+`` chains."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value, True
    if isinstance(node, ast.JoinedStr):
        parts = [
            v.value if isinstance(v, ast.Constant) and isinstance(v.value, str) else PLACEHOLDER
            for v in node.values
        ]
        return "".join(parts), True
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, left_str = _string_text(node.left)
        right, right_str = _string_text(node.right)
        return left + right, left_str or right_str
    return PLACEHOLDER, False


def _dotted(node: ast.AST) -> str | None:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return None


def _is_auth_submodule(name: str) -> bool:
    return name.startswith(AUTH_PACKAGE + ".")


def _module_of(node: ast.ImportFrom) -> str:
    module = node.module or ""
    if node.level and (module == "auth" or module.startswith("auth.")):
        return "ownvitals." + module  # a relative import from a top-level ownvitals module
    return module


def _import_target(node: ast.AST) -> tuple[str, str] | None:
    """``(function, module)`` of ``import_module("m")`` / ``__import__("m", ...)``, else None."""
    if not isinstance(node, ast.Call) or not node.args:
        return None
    name = (_dotted(node.func) or "").rsplit(".", 1)[-1]
    first = node.args[0]
    is_import = name in ("import_module", "__import__")
    if is_import and isinstance(first, ast.Constant) and isinstance(first.value, str):
        return name, first.value
    return None


def _is_import_call(node: ast.AST, target: str = AUTH_PACKAGE) -> bool:
    """``importlib.import_module("ownvitals.auth")`` or ``__import__("ownvitals.auth", ...)``."""
    found = _import_target(node)
    return found is not None and found[1] == target


class _Roots:
    """Local names bound to the ``ownvitals`` package and to ``ownvitals.auth``."""

    def __init__(self, tree: ast.AST) -> None:
        self.package: set[str] = set()
        self.auth: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "ownvitals":
                        self.package.add(alias.asname or "ownvitals")
                    elif alias.name == AUTH_PACKAGE and alias.asname:
                        self.auth.add(alias.asname)
            elif isinstance(node, ast.ImportFrom):
                module = _module_of(node)
                from_package = module == "ownvitals" or bool(node.level and not module)
                for alias in node.names:
                    if alias.name == "ownvitals" and (node.level or not module):
                        self.package.add(alias.asname or "ownvitals")
                    elif alias.name == "auth" and from_package:
                        self.auth.add(alias.asname or "auth")
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                names = [t.id for t in node.targets if isinstance(t, ast.Name)]
                if self.is_auth(node.value):
                    self.auth.update(names)
                if self.is_package(node.value):
                    self.package.update(names)

    def is_package(self, node: ast.AST) -> bool:
        """True when ``node`` evaluates to the top-level ``ownvitals`` package."""
        found = _import_target(node)
        if found is not None:
            function, module = found
            # __import__("a.b") without a fromlist returns the top-level package "a".
            return module == "ownvitals" or (function == "__import__" and module == AUTH_PACKAGE)
        return isinstance(node, ast.Name) and node.id in self.package

    def is_auth(self, node: ast.AST) -> bool:
        """True when ``node`` evaluates to the ``ownvitals.auth`` package."""
        if _is_import_call(node):  # __import__ is also kept here: conservative
            return True
        if isinstance(node, ast.Attribute) and node.attr == "auth" and self.is_package(node.value):
            return True
        dotted = _dotted(node)
        if dotted is None:
            return False
        head, _, rest = dotted.partition(".")
        if head in self.package:
            dotted = "ownvitals" + ("." + rest if rest else "")
        return dotted == AUTH_PACKAGE or dotted in self.auth


EXEMPTION = re.compile(r"#\s*guard8:\s*prose\s*$")


def _marked_lines(source: str) -> set[int]:
    """Line numbers that end with a real ``# guard8: prose`` comment token."""
    found: set[int] = set()
    inside_fstring = 0  # f-strings nest through their replacement fields
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type == tokenize.FSTRING_START:
            inside_fstring += 1
        elif token.type == tokenize.FSTRING_END:
            inside_fstring -= 1
        elif (
            token.type == tokenize.COMMENT and not inside_fstring and EXEMPTION.match(token.string)
        ):
            found.add(token.start[0])
    return found


def _docstrings(tree: ast.AST) -> set[int]:
    """``id()`` of every docstring constant: the first statement of a module, class or def."""
    found: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            first = node.body[0] if node.body else None
            if (
                isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)
            ):
                found.add(id(first.value))
    return found


def scan_source(source: str) -> list[tuple[int, str]]:
    return scan_source_with_exemptions(source)[0]


def scan_source_with_exemptions(source: str) -> tuple[list[tuple[int, str]], int]:
    """``(findings, exemptions)``: what is refused, and how many strings the matcher flagged
    that a ``# guard8: prose`` comment exempted."""
    tree = ast.parse(source)
    marked = _marked_lines(source)
    docstrings = _docstrings(tree)
    exempted: list[ast.AST] = []
    found: list[tuple[int, str]] = []
    roots = _Roots(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if _is_auth_submodule(alias.name):
                    found.append((node.lineno, f"import of {alias.name}"))
        elif isinstance(node, ast.ImportFrom):
            module = _module_of(node)
            public = PUBLIC_ENTRY_POINTS.get(module) if not node.level else None
            if public is not None:
                for alias in node.names:
                    if alias.name not in public:
                        found.append((node.lineno, f"import of {alias.name} from {module}"))
            elif _is_auth_submodule(module):
                found.append((node.lineno, f"import from {module}"))
            elif module == AUTH_PACKAGE:
                for alias in node.names:
                    if alias.name == "*" or alias.name in FORBIDDEN_NAMES:
                        found.append((node.lineno, f"import of {alias.name} from {AUTH_PACKAGE}"))
        elif isinstance(node, ast.Attribute):
            if node.attr in FORBIDDEN_NAMES and roots.is_auth(node.value):
                found.append((node.lineno, f"reference to {node.attr} on ownvitals.auth"))
            elif node.attr == "__dict__" and roots.is_auth(node.value):
                found.append((node.lineno, "__dict__ of ownvitals.auth"))
        elif isinstance(node, ast.Call):
            found += _call_findings(node, roots)
        elif (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and (node.value == TABLES_MODULE or _is_auth_submodule(node.value))
        ):
            found.append((node.lineno, f"string reference to {node.value}"))
        if (
            isinstance(node, ast.Constant | ast.JoinedStr | ast.BinOp)
            and id(node) not in docstrings
        ):
            text, has_string = _string_text(node)
            if has_string and (table := sql_table_in(text)):
                if node.lineno in marked:
                    exempted.append(node)
                else:
                    found.append((node.lineno, f"raw SQL naming table {table}"))
    # One count per string expression: skip parts nested inside another exempted string.
    nested = {id(sub) for node in exempted for sub in ast.walk(node) if sub is not node}
    return sorted(set(found)), sum(1 for node in exempted if id(node) not in nested)


def _str_args(node: ast.Call) -> list[str]:
    values = [*node.args, *(k.value for k in node.keywords)]
    return [v.value for v in values if isinstance(v, ast.Constant) and isinstance(v.value, str)]


def _table_name_arg(node: ast.Call) -> ast.AST | None:
    if node.args:
        return node.args[0]
    return next((k.value for k in node.keywords if k.arg == "name"), None)


def _call_findings(node: ast.Call, roots: _Roots) -> list[tuple[int, str]]:
    callee = _dotted(node.func) or ""
    name = callee.rsplit(".", 1)[-1]
    strings = _str_args(node)
    found: list[tuple[int, str]] = []
    if (
        name == "getattr"
        and len(node.args) > 1
        and roots.is_auth(node.args[0])
        and isinstance(node.args[1], ast.Constant)
        and node.args[1].value in FORBIDDEN_NAMES
    ):
        found.append((node.lineno, f"getattr of {node.args[1].value} on ownvitals.auth"))
    elif name == "vars" and node.args and roots.is_auth(node.args[0]):
        found.append((node.lineno, "vars() of ownvitals.auth"))
    elif name == "resolve_name" and any(v.startswith(AUTH_PACKAGE) for v in strings):
        found.append((node.lineno, "pkgutil.resolve_name of ownvitals.auth"))
    elif name in ("import_module", "__import__"):
        first = strings[0] if strings else ""
        package = strings[1] if len(strings) > 1 else ""
        if _is_auth_submodule(first) or (
            first.startswith(".") and package.startswith(AUTH_PACKAGE)
        ):
            found.append((node.lineno, f"dynamic import of {first}"))
    elif name in ("table", "Table"):
        arg = _table_name_arg(node)
        if isinstance(arg, ast.Constant) and arg.value in TABLE_NAMES:
            found.append((node.lineno, f"sqlalchemy {name}({arg.value!r})"))
    return found
