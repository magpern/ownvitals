"""Which modules can reach the network, computed from the locked environment (the guard of
``egress_scan``; roadmap Amendment 1, PR #55 round 2). Nothing is imported or executed: each
module is found on ``sys.path`` (``FileFinder``, ``BuiltinImporter``, ``FrozenImporter``) and its
source is read with ``ast``.

A module is **network-capable** when

- it is a seed (``SEEDS``: ``socket``, ``_socket``, ``ssl``, ``_ssl``, ``select``);
- or one of its **module-level** imports is network-capable (``try``/``except``, ``if``/``else``,
  ``with``, loops and class bodies are followed; ``if TYPE_CHECKING:`` and
  ``if __name__ == "__main__":`` blocks are not; importing ``a.b`` imports ``a``);
- or a **function body** in it imports a seed itself (``import socket`` inside a function). Other
  function-level imports are not followed: ``os.popen`` imports ``subprocess`` lazily, and
  following that would make nearly every module network-capable.

A reviewed **edge cut** (``egress_classified.EDGE_CUTS``) is never network-capable: its imports
are not a network path, for the reason given there. A module that cannot be found on this
platform (a POSIX-only module on Windows, an optional dependency that is not installed) is
skipped, not followed, and counted (``Closure.unresolved``). Parses are cached for the run.
"""

import ast
import sys
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from importlib.machinery import (
    BYTECODE_SUFFIXES,
    EXTENSION_SUFFIXES,
    SOURCE_SUFFIXES,
    BuiltinImporter,
    ExtensionFileLoader,
    FileFinder,
    FrozenImporter,
    SourceFileLoader,
    SourcelessFileLoader,
)
from pathlib import Path

SEEDS = frozenset({"socket", "_socket", "ssl", "_ssl", "select"})
SRC = Path(__file__).resolve().parent.parent / "src"
OWN = "ownvitals"
_DETAILS = [
    (ExtensionFileLoader, EXTENSION_SUFFIXES),
    (SourceFileLoader, SOURCE_SUFFIXES),
    (SourcelessFileLoader, BYTECODE_SUFFIXES),
]
_IMPORTERS = ("import_module", "__import__")
_SIMPLE = (ast.Expr, ast.Assign, ast.AnnAssign, ast.AugAssign, ast.Return)


@dataclass(frozen=True)
class Found:
    """Where a module is: its source (None for a builtin, an extension or a namespace package)
    and, for a package, where its submodules are."""

    source: Path | None
    search: tuple[str, ...] | None


@dataclass(frozen=True)
class Facts:
    imports: frozenset[str]  # modules (and ``module.name``) imported at module level
    seed_in_function: bool  # a function body imports a seed
    bindings: dict[str, str]  # module-level name bound by an import -> what it stands for


def under(name: str, prefixes: Iterable[str]) -> bool:
    return any(name == p or name.startswith(p + ".") for p in prefixes)


def type_checking(test: ast.expr) -> bool:
    """``TYPE_CHECKING`` or ``<anything>.TYPE_CHECKING``: in a library this is ``typing``'s."""
    return (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
        isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"
    )


def _is_main(test: ast.expr) -> bool:
    return (
        isinstance(test, ast.Compare)
        and isinstance(test.left, ast.Name)
        and test.left.id == "__name__"
    )


def statements(
    body: Iterable[ast.stmt], in_function: bool = False
) -> Iterator[tuple[ast.stmt, bool]]:
    """``(statement, inside a function)`` for every statement that can run, in a module or in
    a function body; not ``if TYPE_CHECKING:`` or ``if __name__ == "__main__":`` blocks."""
    for statement in body:
        yield statement, in_function
        if isinstance(statement, ast.FunctionDef | ast.AsyncFunctionDef):
            yield from statements(statement.body, True)
        elif isinstance(statement, ast.If):
            if not (type_checking(statement.test) or _is_main(statement.test)):
                yield from statements(statement.body, in_function)
            yield from statements(statement.orelse, in_function)
        elif isinstance(statement, ast.Try | ast.TryStar):
            for block in (statement.body, statement.orelse, statement.finalbody):
                yield from statements(block, in_function)
            for handler in statement.handlers:
                yield from statements(handler.body, in_function)
        elif isinstance(statement, ast.With | ast.AsyncWith | ast.ClassDef):
            yield from statements(statement.body, in_function)
        elif isinstance(statement, ast.For | ast.AsyncFor | ast.While):
            yield from statements(statement.body, in_function)
            yield from statements(statement.orelse, in_function)
        elif isinstance(statement, ast.Match):
            for case in statement.cases:
                yield from statements(case.body, in_function)


def resolve_from(node: ast.ImportFrom, module: str, is_package: bool) -> str:
    if not node.level:
        return node.module or ""
    base = module.split(".") if is_package else module.split(".")[:-1]
    base = base[: len(base) - node.level + 1]
    return ".".join([*base, node.module] if node.module else base)


def constant_import(node: ast.Call) -> str | None:
    """The module of ``import_module("x")`` or ``__import__("x")`` with a constant name."""
    func = node.func
    name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
    if name in _IMPORTERS and node.args:
        first = node.args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            return first.value
    return None


def imported(node: ast.stmt, module: str, is_package: bool) -> list[tuple[str, str, str]]:
    """``(imported name, bound local name, what it stands for)`` for an import statement; a
    ``from`` source is listed with no local name."""
    if isinstance(node, ast.Import):
        return [
            (a.name, a.asname, a.name) if a.asname else (a.name, a.name.split(".")[0], "")
            for a in node.names
        ]
    if isinstance(node, ast.ImportFrom):
        source = resolve_from(node, module, is_package)
        found = [(source, "", "")]
        found += [
            (f"{source}.{a.name}", a.asname or a.name, f"{source}.{a.name}")
            for a in node.names
            if a.name != "*"
        ]
        return found
    return []


def facts_of(data: bytes, filename: str, module: str, is_package: bool) -> Facts:
    tree = ast.parse(data, filename=filename)
    dynamic = b"import_module" in data or b"__import__" in data
    imports: set[str] = set()
    bindings: dict[str, str] = {}
    seeded = False
    for statement, in_function in statements(tree.body):
        names = imported(statement, module, is_package)
        if dynamic and isinstance(statement, _SIMPLE):
            calls = (n for n in ast.walk(statement) if isinstance(n, ast.Call))
            names += [(name, "", "") for c in calls if (name := constant_import(c))]
        for name, bound, target in names:
            if in_function:
                seeded = seeded or under(name, SEEDS)
                continue
            imports.add(name)
            if bound:
                bindings[bound] = target or bound
    return Facts(frozenset(imports), seeded, bindings)


_FACTS: dict[Path, Facts] = {}  # parses of the run, shared by every Closure


class Closure:
    """The finder and the capability of every module seen, for one run (``CLOSURE`` in
    ``egress_scan``); a test builds its own with planted modules or other cuts."""

    def __init__(
        self, cuts: Iterable[str] = (), extra_paths: Iterable[str] = (), src: Path = SRC
    ) -> None:
        self.cuts = frozenset(cuts)
        self.src = src
        self._paths = [*extra_paths, *(p for p in sys.path if p and Path(p).is_dir())]
        self._finders: dict[str, FileFinder] = {}
        self._found: dict[str, Found | None] = {}
        self._toward: dict[str, str | None] = {}  # capable module -> next hop to the network
        self._not_capable: set[str] = set()
        self.unresolved: set[str] = set()

    # --- finding modules, without importing them ---------------------------------------------

    def _find_in(self, name: str, locations: Iterable[str]) -> Found | None:
        portions: list[str] = []
        for location in locations:
            finder = self._finders.get(location)
            if finder is None:
                finder = self._finders[location] = FileFinder(location, *_DETAILS)
            spec = finder.find_spec(name)
            if spec is None:
                continue
            if spec.loader is not None:
                origin = spec.origin or ""
                source = Path(origin) if origin.endswith(".py") else None
                search = spec.submodule_search_locations
                return Found(source, None if search is None else tuple(search))
            portions.extend(spec.submodule_search_locations or [])
        return Found(None, tuple(portions)) if portions else None

    def find(self, name: str) -> Found | None:
        if name in self._found:
            return self._found[name]
        parent = name.rpartition(".")[0]
        found: Found | None = None
        if not name or name.startswith(".") or ".." in name:
            pass
        elif under(name, (OWN,)):
            path = self.src.joinpath(*name.split("."))
            if (path / "__init__.py").is_file():
                found = Found(path / "__init__.py", (str(path),))
            elif path.with_suffix(".py").is_file():
                found = Found(path.with_suffix(".py"), None)
        elif parent:
            above = self.find(parent)
            if above is not None and above.search:
                found = self._find_in(name, above.search)
        elif BuiltinImporter.find_spec(name) is not None:
            found = Found(None, None)
        else:
            found = self._find_in(name, self._paths)
            if found is None and FrozenImporter.find_spec(name) is not None:
                found = Found(None, None)
        self._found[name] = found
        return found

    def is_module(self, name: str) -> bool:
        return self.find(name) is not None

    # --- facts and capability --------------------------------------------------------------

    def facts(self, name: str) -> Facts | None:
        found = self.find(name)
        if found is None or found.source is None:
            return None
        facts = _FACTS.get(found.source)
        if facts is None:
            data = found.source.read_bytes()
            facts = facts_of(data, str(found.source), name, found.search is not None)
            _FACTS[found.source] = facts
        return facts

    def binding(self, module: str, name: str) -> str | None:
        """What ``module.name`` stands for when the module bound it by an import."""
        facts = self.facts(module)
        return None if facts is None else facts.bindings.get(name)

    def edges(self, name: str) -> list[str]:
        """The modules importing ``name`` imports at module level, its parent package first;
        a name that is not a module is dropped, an unresolvable one is counted."""
        parent = name.rpartition(".")[0]
        out = [parent] if parent else []
        facts = self.facts(name)
        for item in sorted(() if facts is None else facts.imports):
            parts = item.split(".")
            for i in range(1, len(parts) + 1):
                candidate = ".".join(parts[:i])
                if self.find(candidate) is None:
                    if i == 1:
                        self.unresolved.add(candidate)
                    break
                out.append(candidate)
        return out

    def _source(self, name: str) -> bool:
        if name in self.cuts:
            return False
        facts = self.facts(name)
        return name in SEEDS or (facts is not None and facts.seed_in_function)

    def capable(self, name: str) -> bool:
        """Whether importing ``name`` (a module) gives a path to the network. A depth-first
        search that stops at the first network path; a search that finds none marks everything
        it reached as not capable (all of it is inside the reached set)."""
        if name in self._toward:
            return True
        if name in self._not_capable or name in self.cuts or self.find(name) is None:
            return False
        previous: dict[str, str | None] = {name: None}
        stack = [name]
        while stack:
            current = stack.pop()
            if current in self._toward or self._source(current):
                hop = self._toward.get(current)
                at: str | None = current
                while at is not None:
                    self._toward.setdefault(at, hop)
                    hop, at = at, previous[at]
                return True
            if current in self._not_capable or current in self.cuts:
                continue
            for edge in self.edges(current):
                if edge not in previous:
                    previous[edge] = current
                    stack.append(edge)
        self._not_capable.update(previous)
        return False

    def path(self, name: str) -> list[str]:
        """One import path from ``name`` to the network (empty if there is none)."""
        if not self.capable(name):
            return []
        trail = [name]
        while (hop := self._toward.get(trail[-1])) is not None and len(trail) < 64:
            trail.append(hop)
        return trail
