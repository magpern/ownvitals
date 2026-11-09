"""The session guard of ADR 0002 section 6: only allowlisted modules may open a ``user_session``
or a ``system_session``.

Each has its own allowlist, of packages (the package and everything under it) and exact modules:

- ``user_session`` (owned rows of one user): ``ownvitals.db`` (where it is defined and exported),
  ``ownvitals.auth`` (the web issuer and the user's own account data, ADR 0003) and
  ``ownvitals.connections`` (ADR 0004; its status writes during a run are the one exemption from
  the fence, ADR 0005 section 4). Of ``ownvitals.jobs`` (#9) exactly three modules:
  ``ownvitals.jobs.fence`` (the fenced helper: every write a worker makes for a job goes through
  it), ``ownvitals.jobs.service`` (the jobs API's own writes for the signed-in user, web only) and
  ``ownvitals.jobs.progress`` (the event stream's owner-scoped reads, web only). The exact
  module ``ownvitals.ecg_validate`` (#47) reads one named owner's ECG rows for the operator
  command; the pure ``ownvitals.ecg`` library stays outside the allowlist. The exact module
  ``ownvitals.views.findings_read`` (#190) is ADR 0006 section 11's one read function for findings
  and ECG estimates: the findings API, notifications, export, doctor PDF and AI context read
  through it, so none of them opens a session of its own for those rows. The exact module
  ``ownvitals.views.reads`` (#189) is the read API's one other reader (status, metrics, days,
  readings, nerve summary): it never reads those tables. The exact module
  ``ownvitals.views.ecg_read`` (#192) reads the signed-in user's stored ECG recordings (Withings'
  own values and the signal) for the ECG list, viewer and PDF, never the estimates, which come
  only through ``findings_read``. The exact module ``ownvitals.views.profile_store`` (#203)
  reads and writes the caller's own
  ``user_profiles`` row. The exact module
  ``ownvitals.derived.check_cli`` (#187) reads one named owner's daily values and findings for
  ``ownvitals analysis check`` (read only, counts and versions out). **The rest of
  ``ownvitals.jobs``, the worker and every job kind live outside the allowlist** and write
  through the fence.
- ``system_session`` (system tables, no user): ``ownvitals.db`` and ``ownvitals.auth`` (the
  account tables are system tables reached only through it, ADR 0003 section 1),
  ``ownvitals.jobs.queue`` (#9: the calls of the queue's ``SECURITY DEFINER`` functions, the
  maintenance context of ADR 0002 section 4; the session itself reaches no owned row), and the
  exact module ``ownvitals.crypto.keks`` (#8: the key ring records and compares its
  ``connection_keks`` check rows at startup, before any user is known, ADR 0004 section 3), and
  the exact module ``ownvitals.flags.routes`` (#12: the public settings read the stored banner
  and sign-up mode, a system table, without a user), the exact module
  ``ownvitals.notify.outbox`` (#194: web's outbox sender claims and marks rows only through the
  outbox's ``SECURITY DEFINER`` functions and holds no ``UserContext``, ADR 0007 section 3; the
  session itself reaches no owned row), the exact module ``ownvitals.ai.budget`` (#205: the
  reaper's ``ai_settle_lost``, a definer function returning a count), and the exact module
  ``ownvitals.worker.metrics`` (#167: the metrics refresh calls ``queue_stats()`` and the two
  metrics aggregates, definer functions returning counts and ages that refuse a user session;
  the session itself reaches no owned row). The admin area opens
  neither: it runs on its own audited maint session (``ownvitals.admin.db``).

A later package joins a list in its own PR, with its reason, and the tests in
``test_session_guard.py`` that pin both lists change with it. Anything else fails
``scan_source``: a reference to the guarded names (``_owned_session`` and the statement that sets
the user, ``SET_USER``, always) by any name, attribute, import alias or string; an import of
``ownvitals.db.session`` or anything in it; a name or attribute chain that reaches that module
(``ownvitals.db.session.X``, ``db.session.X`` after ``from ownvitals import db``, ``m.X`` after
``import ownvitals.db.session as m``); and any non-docstring string that sets ``app.user_id``
itself, in any letter case (Postgres ignores it). Relative imports are resolved to absolute
module names first (``from ..db import session`` in ``ownvitals.jobs.sync`` is ``from
ownvitals.db import session``), and one that climbs above the top-level package is refused. Like
the other guards it catches accidental use and makes the rest visible in review; it cannot stop
obfuscated code (``getattr`` with a computed name, ``importlib`` with a built string).
"""

import ast
from dataclasses import dataclass


@dataclass(frozen=True)
class Allowlist:
    prefixes: tuple[str, ...]
    modules: tuple[str, ...] = ()

    def allows(self, module: str) -> bool:
        if module in self.modules:
            return True
        return any(module == p or module.startswith(p + ".") for p in self.prefixes)


# The exact modules of ``ownvitals.jobs`` (#9) that may open each session; see the docstring.
JOBS_USER_SESSION = ("ownvitals.jobs.fence", "ownvitals.jobs.service", "ownvitals.jobs.progress")
# The owner ECG report (#47): one module, not the pure ECG library.
ECG_USER_SESSION = ("ownvitals.ecg_validate",)
# The one findings read function (#190, ADR 0006 section 11): not the routes, not the package.
VIEWS_USER_SESSION = (
    "ownvitals.views.findings_read",
    "ownvitals.views.ecg_read",
    "ownvitals.views.reads",
    "ownvitals.views.profile_store",
)
# ``analysis check`` (#187): the owner's reproducibility check reads one named owner's daily
# values and findings for the operator command; the rest of ``ownvitals.derived`` writes only
# through the fence.
DERIVED_USER_SESSION = ("ownvitals.derived.check_cli",)
JOBS_SYSTEM_SESSION = ("ownvitals.jobs.queue",)
# The key ring's startup check (#8), the one module of ``ownvitals.crypto`` that may.
KEYS_SYSTEM_SESSION = ("ownvitals.crypto.keks",)
# The public settings (#12): the stored banner and sign-up mode, read without a user.
FLAGS_SYSTEM_SESSION = ("ownvitals.flags.routes",)
# The outbox's claim and mark (#194): calls of its definer functions, without a user.
NOTIFY_SYSTEM_SESSION = ("ownvitals.notify.outbox",)
# The AI reaper (#205): ``ai_settle_lost``, a definer function that returns a count; the session
# itself reaches no owned row.
AI_SYSTEM_SESSION = ("ownvitals.ai.budget",)
# The worker's metrics refresh (#167): the metrics aggregates and ``queue_stats()``, definer
# functions returning counts and ages; the session itself reaches no owned row.
METRICS_SYSTEM_SESSION = ("ownvitals.worker.metrics",)

USER_SESSION = Allowlist(
    prefixes=("ownvitals.db", "ownvitals.auth", "ownvitals.connections"),
    modules=(
        *JOBS_USER_SESSION,
        *ECG_USER_SESSION,
        *VIEWS_USER_SESSION,
        *DERIVED_USER_SESSION,
    ),
)
SYSTEM_SESSION = Allowlist(
    prefixes=("ownvitals.db", "ownvitals.auth"),
    modules=(
        *JOBS_SYSTEM_SESSION,
        *KEYS_SYSTEM_SESSION,
        *FLAGS_SYSTEM_SESSION,
        *NOTIFY_SYSTEM_SESSION,
        *AI_SYSTEM_SESSION,
        *METRICS_SYSTEM_SESSION,
    ),
)
# Each guarded name and who may use it; the private helper behind both is the session module's
# own (only ``ownvitals.db`` may name it).
GUARDED = {
    "user_session": USER_SESSION,
    "system_session": SYSTEM_SESSION,
    "_owned_session": Allowlist(prefixes=("ownvitals.db",)),
    "SET_USER": Allowlist(prefixes=("ownvitals.db",)),
}
SESSION_MODULE = "ownvitals.db.session"
# Modules that may import ``ownvitals.db.session`` itself (and so reach every name in it).
SESSION_MODULE_IMPORTERS = Allowlist(prefixes=("ownvitals.db",))
# Modules that may name ``app.user_id`` in a string: the session module sets it, and the role
# check reads it on every pool checkout.
SETS_USER_ID = Allowlist(prefixes=("ownvitals.db",))


def _docstrings(tree: ast.AST) -> set[int]:
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                ids.add(id(body[0].value))
    return ids


def _in_session_module(dotted: str) -> bool:
    return dotted == SESSION_MODULE or dotted.startswith(SESSION_MODULE + ".")


def _absolute(node: ast.ImportFrom, module: str, is_package: bool) -> str | None:
    """The absolute module a ``from ... import`` reads from; ``None`` above the top level."""
    if not node.level:
        return node.module or ""
    package = module.split(".") if is_package else module.split(".")[:-1]
    if node.level > len(package):
        return None
    base = package[: len(package) - node.level + 1]
    return ".".join([*base, node.module] if node.module else base)


def _bindings(tree: ast.AST, module: str, is_package: bool) -> dict[str, str]:
    """Local names bound by imports -> the absolute dotted name each stands for."""
    bound: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    bound[alias.asname] = alias.name
                else:
                    top = alias.name.split(".")[0]
                    bound[top] = top
        elif isinstance(node, ast.ImportFrom):
            base = _absolute(node, module, is_package)
            for alias in node.names:
                if base is not None and alias.name != "*":
                    bound[alias.asname or alias.name] = ".".join(filter(None, (base, alias.name)))
    return bound


def _resolved(node: ast.AST, bound: dict[str, str]) -> str | None:
    """``a.b.c`` with its first name replaced by what an import bound it to."""
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    return ".".join([bound.get(node.id, node.id), *reversed(parts)])


def _reaches_session_module(node: ast.AST, bound: dict[str, str]) -> bool:
    """A name bound to the session module or to something in it; for an attribute chain, the
    link that reaches the module itself (so ``ownvitals.db.session.X`` is reported once)."""
    if not isinstance(node, ast.Name | ast.Attribute):
        return False
    dotted = _resolved(node, bound)
    if dotted is None:
        return False
    return (
        dotted == SESSION_MODULE if isinstance(node, ast.Attribute) else _in_session_module(dotted)
    )


def scan_source(source: str, module: str, *, is_package: bool = False) -> list[tuple[int, str]]:
    """What ``module`` may not do; empty when it uses only what its allowlists give it.

    ``is_package``: the source is the package's own ``__init__.py`` (for relative imports)."""
    refused = {name for name, allowlist in GUARDED.items() if not allowlist.allows(module)}
    may_import_module = SESSION_MODULE_IMPORTERS.allows(module)
    may_name_user_id = SETS_USER_ID.allows(module)
    tree = ast.parse(source)
    docs = _docstrings(tree)
    bound = _bindings(tree, module, is_package)
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        line = getattr(node, "lineno", 0)
        if isinstance(node, ast.Name) and node.id in refused:
            found.append((line, f"uses {node.id}"))
        elif isinstance(node, ast.Attribute) and node.attr in refused:
            found.append((line, f"uses {node.attr}"))
        elif not may_import_module and _reaches_session_module(node, bound):
            found.append((line, f"reaches {SESSION_MODULE}"))
        elif isinstance(node, ast.alias) and (
            node.name.rsplit(".", 1)[-1] in refused
            or (_in_session_module(node.name) and not may_import_module)
        ):
            found.append((line, f"imports {node.name}"))
        elif isinstance(node, ast.ImportFrom) and not may_import_module:
            base = _absolute(node, module, is_package)
            if base is None:
                found.append((line, "relative import above the top-level package"))
            elif _in_session_module(base):
                found.append((line, f"imports from {SESSION_MODULE}"))
            elif base == "ownvitals.db" and any(a.name == "session" for a in node.names):
                found.append((line, f"imports {SESSION_MODULE}"))
        elif (
            isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docs
        ):
            if node.value in refused or (node.value == SESSION_MODULE and not may_import_module):
                found.append((line, f"names {node.value!r}"))
            # Postgres compares setting names case-insensitively: ``APP.USER_ID`` is the same.
            elif "app.user_id" in node.value.lower() and not may_name_user_id:
                found.append((line, "sets app.user_id itself"))
    return found
