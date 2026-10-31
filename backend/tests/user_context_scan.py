"""Guard 5 of ADR 0002 section 7: building or copying a ``UserContext`` outside its issuers.

``scan_source`` parses Python source and returns what it refuses:

- any reference to ``UserContext`` that is not a type annotation or an import used only for
  annotations (calls, also through an alias, subclassing, ``isinstance``, ``cast``, strings);
- any reference to the issuer token (``issuer_token``, ``_ISSUER``);
- ``dataclasses.replace`` (also imported as ``replace``), ``object.__setattr__`` and
  ``object.__new__``, ``x.__class__(...)`` and ``type(x)(...)``, in any module, whether or not
  it mentions ``UserContext``; ``functools.partial`` in a module that does. The one exception is
  the frozen-dataclass idiom ``object.__setattr__(self, ...)`` written directly in the
  ``__post_init__(self, ...)`` method of a class, where ``self`` is never rebound: it can only
  reach an instance of that class, never a ``UserContext`` (which lives in its own module and
  can't be subclassed or named elsewhere). Calling ``__post_init__`` explicitly (other than
  through ``super()``) is refused, so no other object can be passed as its ``self``;
- ``Depends(UserContext)``, ``Security(UserContext)``, and a bare ``Depends()`` or
  ``Security()`` (no dependency, or ``None``) on a ``UserContext`` parameter (default value
  or ``Annotated[UserContext, Depends()]``), so FastAPI would try to build one from the
  request: the mistake fails in tests, not at request time. ``Depends``, ``Security`` and
  ``Annotated`` are found under any import alias (``from fastapi import Depends as D``,
  ``import fastapi as f``, ``D = Depends``, ``D: Any = Depends``), and whole-string annotations
  and string ``TypeAlias`` values (``Ctx: TypeAlias = "Annotated[UserContext, Depends()]"``)
  are parsed.
  ``Depends(some_dependency)`` and ``Security(some_dependency)`` are fine: a context reaches
  a handler from the authentication dependency, which returns one built by its issuer.

This layer and the runtime barrier in ``ownvitals.context`` stop accidental misuse. Deliberately
malicious code inside the process is out of scope; the review gate catches it.
"""

import ast

NAME = "UserContext"
TOKEN_NAMES = {"issuer_token", "_ISSUER"}
CONTEXT_MODULE = "ownvitals.context"
# The only modules whose own source may build a context (ADR 0002 section 1): the issuer in the
# auth package (#10), the job issuer (#9, ADR 0005 section 4) and the class's own module. The
# rest of ``ownvitals.auth`` and ``ownvitals.jobs`` is scanned like any other code.
DEFINITION = "ownvitals/context.py"
ISSUER_MODULES = ("ownvitals/auth/deps.py", "ownvitals/jobs/issuer.py", DEFINITION)
# module -> function: refused everywhere / refused where the module handles a UserContext.
ALWAYS_REFUSED = {"dataclasses": "replace"}
REFUSED_WITH_CONTEXT = {"functools": "partial"}
REFUSED_BUILTIN_CALLS = {"object.__setattr__", "object.__new__", "object.__delattr__"}


def _dotted(node: ast.AST) -> str | None:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return None


def _spellings(tree: ast.AST, functions: dict[str, str]) -> set[str]:
    """Local spellings of the given ``module -> function`` pairs."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in functions:
                    names.add(f"{alias.asname or alias.name}.{functions[alias.name]}")
        elif isinstance(node, ast.ImportFrom) and node.module in functions:
            for alias in node.names:
                if alias.name == functions[node.module]:
                    names.add(alias.asname or alias.name)
    return names


FASTAPI_MODULES = {"fastapi", "fastapi.params", "fastapi.param_functions"}
TYPING_MODULES = {"typing", "typing_extensions"}
DEPENDENCY_CALLS = {"Depends", "Security"}


class _Spellings:
    """Names a module gives to ``Depends``/``Security`` and ``Annotated``, aliases included."""

    def __init__(self, tree: ast.AST) -> None:
        self.dependencies: set[str] = set()
        self.annotated: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    bound = alias.asname or alias.name
                    if alias.name in FASTAPI_MODULES:
                        self.dependencies |= {f"{bound}.{name}" for name in DEPENDENCY_CALLS}
                    if alias.name in TYPING_MODULES:
                        self.annotated.add(f"{bound}.Annotated")
            elif isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    bound = alias.asname or alias.name
                    if node.module in FASTAPI_MODULES and alias.name in DEPENDENCY_CALLS:
                        self.dependencies.add(bound)
                    if node.module in TYPING_MODULES and alias.name == "Annotated":
                        self.annotated.add(bound)
        # `D = Depends`, `E = D`, `D: Any = Depends`: follow simple (annotated) assignments.
        for _ in range(3):
            for node in ast.walk(tree):
                if isinstance(node, ast.Assign | ast.AnnAssign) and node.value is not None:
                    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                    if len(targets) != 1:
                        continue
                    target, value = targets[0], _dotted(node.value)
                    if isinstance(target, ast.Name) and value is not None:
                        if self._is(value, self.dependencies, DEPENDENCY_CALLS):
                            self.dependencies.add(target.id)
                        if self._is(value, self.annotated, {"Annotated"}):
                            self.annotated.add(target.id)

    @staticmethod
    def _is(dotted: str | None, names: set[str], tails: set[str]) -> bool:
        # The last component alone also counts, so `x.Depends` is never missed.
        return dotted is not None and (dotted in names or dotted.split(".")[-1] in tails)

    def is_dependency(self, node: ast.AST) -> bool:
        """``Depends(...)`` or ``Security(...)``, under any spelling."""
        return isinstance(node, ast.Call) and self._is(
            _dotted(node.func), self.dependencies, DEPENDENCY_CALLS
        )

    def is_annotated(self, node: ast.AST) -> bool:
        return isinstance(node, ast.Subscript) and self._is(
            _dotted(node.value), self.annotated, {"Annotated"}
        )


def _is_bare(node: ast.AST) -> bool:
    """``Depends()``, ``Depends(None)``, ``Depends(dependency=None)``, ``Security(scopes=[..])``.

    FastAPI would then call the annotated class itself.
    """
    if not isinstance(node, ast.Call):
        return False
    given = next((k.value for k in node.keywords if k.arg == "dependency"), None)
    if node.args:
        given = node.args[0]
    return given is None or (isinstance(given, ast.Constant) and given.value is None)


def _mentions(node: ast.AST, aliases: set[str]) -> bool:
    """True when the subtree names the class (also as a string annotation)."""
    for sub in ast.walk(node):
        if isinstance(sub, ast.Name) and sub.id in aliases:
            return True
        if isinstance(sub, ast.Attribute) and sub.attr == NAME:
            return True
        if isinstance(sub, ast.Constant) and isinstance(sub.value, str) and NAME in sub.value:
            return True
    return False


def _parsed(annotation: ast.AST) -> ast.AST:
    """A whole-string annotation (``ctx: "Annotated[UserContext, Depends()]"``) as an expression."""
    if isinstance(annotation, ast.Constant) and isinstance(annotation.value, str):
        try:
            parsed = ast.parse(annotation.value.strip(), mode="eval").body
        except SyntaxError:
            return annotation
        for sub in ast.walk(parsed):
            ast.copy_location(sub, annotation)  # report the annotation's own line
        return parsed
    return annotation


def _annotated_findings(
    root: ast.AST, aliases: set[str], spellings: _Spellings
) -> list[tuple[int, str]]:
    found: list[tuple[int, str]] = []
    for node in ast.walk(root):
        if (
            isinstance(node, ast.Subscript)
            and spellings.is_annotated(node)
            and isinstance(node.slice, ast.Tuple)
            and node.slice.elts
            and _mentions(node.slice.elts[0], aliases)
            and any(spellings.is_dependency(sub) and _is_bare(sub) for sub in node.slice.elts[1:])
        ):
            found.append((node.lineno, "Annotated[UserContext, Depends()]"))
    return found


def _depends_findings(tree: ast.AST, aliases: set[str]) -> list[tuple[int, str]]:
    spellings = _Spellings(tree)
    found = _annotated_findings(tree, aliases, spellings)
    for node in ast.walk(tree):
        if spellings.is_dependency(node):
            assert isinstance(node, ast.Call)
            if any(
                _mentions(arg, aliases) for arg in [*node.args, *(k.value for k in node.keywords)]
            ):
                found.append((node.lineno, "Depends(UserContext)"))
        elif isinstance(node, ast.arguments):
            params = [*node.posonlyargs, *node.args, *node.kwonlyargs]
            defaults = [*([None] * (len(node.posonlyargs + node.args) - len(node.defaults))),
                        *node.defaults, *node.kw_defaults]  # fmt: skip
            for param, default in zip(params, defaults, strict=True):
                if param.annotation is None:
                    continue
                annotation = _parsed(param.annotation)
                found += _annotated_findings(annotation, aliases, spellings)
                if (
                    default is not None
                    and spellings.is_dependency(default)
                    and _is_bare(default)
                    and _mentions(annotation, aliases)
                ):
                    found.append((param.lineno, "Depends() on a UserContext parameter"))
        elif isinstance(node, ast.AnnAssign | ast.FunctionDef | ast.AsyncFunctionDef):
            annotation = node.annotation if isinstance(node, ast.AnnAssign) else node.returns
            if annotation is not None:
                found += _annotated_findings(_parsed(annotation), aliases, spellings)
            if (
                isinstance(node, ast.AnnAssign)
                and node.value is not None
                and (_dotted(node.annotation) or "").split(".")[-1] == "TypeAlias"
            ):
                # ``Ctx: TypeAlias = "Annotated[UserContext, Depends()]"``: the string is a type.
                found += _annotated_findings(_parsed(node.value), aliases, spellings)
    return found


_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)


def _own_nodes(function: ast.FunctionDef | ast.AsyncFunctionDef) -> list[ast.AST]:
    """The nodes of ``function``'s body, not those of functions, lambdas or classes inside it."""
    found: list[ast.AST] = []
    pending: list[ast.AST] = list(function.body)
    while pending:
        node = pending.pop()
        if isinstance(node, _SCOPES):
            continue  # its own scope: its ``self`` may be anything
        found.append(node)
        pending.extend(ast.iter_child_nodes(node))
    return found


def _rebinds_self(node: ast.AST) -> bool:
    """Any binding of the name ``self``: assignment, ``for``, ``with``, walrus, ``del``,
    ``except ... as``, ``import ... as``, a ``match`` capture."""
    if isinstance(node, ast.Name):
        return node.id == "self" and not isinstance(node.ctx, ast.Load)
    if isinstance(node, ast.ExceptHandler | ast.MatchAs | ast.MatchStar):
        return node.name == "self"
    if isinstance(node, ast.MatchMapping):
        return node.rest == "self"
    if isinstance(node, ast.alias):
        return (node.asname or node.name.split(".")[0]) == "self"
    return False


def _post_init_setattrs(tree: ast.AST) -> set[int]:
    """Ids of the ``object.__setattr__(self, ...)`` calls of the frozen-dataclass idiom."""
    allowed: set[int] = set()
    for cls in ast.walk(tree):
        if not isinstance(cls, ast.ClassDef):
            continue
        for method in cls.body:
            if not (
                isinstance(method, ast.FunctionDef)
                and method.name == "__post_init__"
                and not method.decorator_list  # no staticmethod or classmethod
                and method.args.args
                and method.args.args[0].arg == "self"
                and not method.args.posonlyargs
            ):
                continue
            nodes = _own_nodes(method)
            if any(_rebinds_self(n) for n in nodes):
                continue
            allowed |= {
                id(n)
                for n in nodes
                if isinstance(n, ast.Call)
                and _dotted(n.func) == "object.__setattr__"
                and n.args
                and isinstance(n.args[0], ast.Name)
                and n.args[0].id == "self"
            }
    return allowed


def _explicit_post_init(node: ast.Call) -> bool:
    """``x.__post_init__(...)``, but not ``super().__post_init__(...)``."""
    func = node.func
    if not (isinstance(func, ast.Attribute) and func.attr == "__post_init__"):
        return False
    receiver = func.value
    return not (
        isinstance(receiver, ast.Call)
        and isinstance(receiver.func, ast.Name)
        and receiver.func.id == "super"
    )


def _forging_call(node: ast.Call) -> str | None:
    func = node.func
    if isinstance(func, ast.Attribute) and func.attr == "__class__":
        return "call of x.__class__(...)"
    if (
        isinstance(func, ast.Call)
        and isinstance(func.func, ast.Name)
        and func.func.id == "type"
        and len(func.args) == 1
    ):
        return "call of type(x)(...)"
    return None


def scan_source(source: str) -> list[tuple[int, str]]:
    tree = ast.parse(source)
    annotation_nodes: set[int] = set()

    def mark(annotation: ast.AST | None) -> None:
        if annotation is not None:
            annotation_nodes.update(id(n) for n in ast.walk(annotation))

    for node in ast.walk(tree):
        if isinstance(node, ast.arg | ast.AnnAssign):
            mark(node.annotation)
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            mark(node.returns)

    aliases = {NAME}  # local names bound to the class by an import
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == CONTEXT_MODULE:
            for alias in node.names:
                if alias.name == "*":
                    found.append((node.lineno, "star import of ownvitals.context"))
                elif alias.name == NAME and alias.asname:
                    aliases.add(alias.asname)
                elif alias.name in TOKEN_NAMES:
                    found.append((node.lineno, f"import of {alias.name}"))

    found += _depends_findings(tree, aliases)
    refused = _spellings(tree, ALWAYS_REFUSED) | (
        _spellings(tree, REFUSED_WITH_CONTEXT) if NAME in source else set()
    )
    post_init_setattrs = _post_init_setattrs(tree)
    called = {id(n.func) for n in ast.walk(tree) if isinstance(n, ast.Call)}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        callee = _dotted(node.func)
        if callee in REFUSED_BUILTIN_CALLS and id(node) in post_init_setattrs:
            continue  # the frozen-dataclass idiom on its own instance (see the module docstring)
        if callee in refused or callee in REFUSED_BUILTIN_CALLS:
            found.append((node.lineno, f"call to {callee}"))
        elif _explicit_post_init(node):
            found.append((node.lineno, "explicit call of __post_init__"))
        elif (why := _forging_call(node)) is not None:
            found.append((node.lineno, why))

    for node in ast.walk(tree):
        if id(node) in annotation_nodes:
            continue
        if isinstance(node, ast.Name) and (node.id in aliases or node.id in TOKEN_NAMES):
            found.append((node.lineno, f"reference to {node.id}"))
        elif isinstance(node, ast.Attribute) and (node.attr == NAME or node.attr in TOKEN_NAMES):
            found.append((node.lineno, f"reference to {_dotted(node) or node.attr}"))
        elif isinstance(node, ast.Constant) and (node.value == NAME or node.value in TOKEN_NAMES):
            # A string such as getattr(module, "UserContext"); string annotations are skipped.
            found.append((node.lineno, f"string reference to {node.value}"))
        elif (
            isinstance(node, ast.Attribute)
            and _dotted(node) in REFUSED_BUILTIN_CALLS
            and id(node) not in called
        ):
            # ``s = object.__setattr__`` and a call through ``s`` later: refused where it is taken.
            found.append((node.lineno, f"reference to {_dotted(node)}"))
    return sorted(set(found))
