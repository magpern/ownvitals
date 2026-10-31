"""The session guard of ADR 0002 section 6: only allowlisted modules may open a ``user_session``
or a ``system_session``.

Each has its own allowlist, of packages (the package and everything under it) and exact modules:

- ``user_session`` (owned rows of one user): ``ownvitals.db`` (where it is defined and exported),
  ``ownvitals.auth`` (the web issuer and the user's own account data, ADR 0003) and
  ``ownvitals.connections`` (ADR 0004; its status writes during a run are the one exemption from
  the fence, ADR 0005 section 4). Every write a worker makes for a job goes through the fenced
  helper of ``ownvitals.jobs`` (#9), whose exact modules join the list in that PR; **job kinds
  live outside the allowlist** and write through the fence.
- ``system_session`` (system tables, no user): ``ownvitals.db`` and ``ownvitals.auth`` (the
  account tables are system tables reached only through it, ADR 0003 section 1). The KEK check
  (#8), the queue's definer functions (#9) and the flags and admin area (#12) add their exact
  modules in their own PRs.

A later package joins a list in its own PR, with its reason, and the tests in
``test_session_guard.py`` that pin both lists change with it. Anything else fails
``scan_source``: a reference to the guarded names (``_owned_session`` always) by any name,
attribute, import alias or string, an import of ``ownvitals.db.session``, and any non-docstring
string that sets ``app.user_id`` itself. Like the other guards it catches accidental use and
makes the rest visible in review; it cannot stop obfuscated code.
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


USER_SESSION = Allowlist(prefixes=("ownvitals.db", "ownvitals.auth", "ownvitals.connections"))
SYSTEM_SESSION = Allowlist(prefixes=("ownvitals.db", "ownvitals.auth"))
# Each guarded name and who may use it; the private helper behind both is the session module's
# own (only ``ownvitals.db`` may name it).
GUARDED = {
    "user_session": USER_SESSION,
    "system_session": SYSTEM_SESSION,
    "_owned_session": Allowlist(prefixes=("ownvitals.db",)),
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


def scan_source(source: str, module: str) -> list[tuple[int, str]]:
    """What ``module`` may not do; empty when it uses only what its allowlists give it."""
    refused = {name for name, allowlist in GUARDED.items() if not allowlist.allows(module)}
    may_import_module = SESSION_MODULE_IMPORTERS.allows(module)
    may_name_user_id = SETS_USER_ID.allows(module)
    tree = ast.parse(source)
    docs = _docstrings(tree)
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        line = getattr(node, "lineno", 0)
        if isinstance(node, ast.Name) and node.id in refused:
            found.append((line, f"uses {node.id}"))
        elif isinstance(node, ast.Attribute) and node.attr in refused:
            found.append((line, f"uses {node.attr}"))
        elif isinstance(node, ast.alias) and (
            node.name.rsplit(".", 1)[-1] in refused
            or (node.name == SESSION_MODULE and not may_import_module)
        ):
            found.append((line, f"imports {node.name}"))
        elif isinstance(node, ast.ImportFrom) and not may_import_module:
            if node.module == SESSION_MODULE:
                found.append((line, f"imports from {SESSION_MODULE}"))
            elif node.module == "ownvitals.db" and any(a.name == "session" for a in node.names):
                found.append((line, f"imports {SESSION_MODULE}"))
        elif (
            isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docs
        ):
            if node.value in refused or (node.value == SESSION_MODULE and not may_import_module):
                found.append((line, f"names {node.value!r}"))
            elif "app.user_id" in node.value and not may_name_user_id:
                found.append((line, "sets app.user_id itself"))
    return found
