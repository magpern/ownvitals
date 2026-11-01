"""Guard 5 of ADR 0002 section 7: building or copying a ``UserContext`` outside its issuers.

``scan_source`` parses Python source and returns what it refuses:

- any reference to ``UserContext`` that is not a type annotation or an import used only for
  annotations (calls, also through an alias, subclassing, ``isinstance``, ``cast``, strings);
- any reference to the issuer token (``issuer_token``, ``_ISSUER``);
- ``dataclasses.replace`` (also imported as ``replace``), ``object.__setattr__``,
  ``object.__delattr__`` and ``object.__new__`` (also taken as a value), ``x.__class__(...)`` and
  ``type(x)(...)``, in any module, whether or not it mentions ``UserContext``;
  ``functools.partial`` in a module that does;
- assigning ``__class__`` (``x.__class__ = K``, ``del x.__class__``) and the strings
  ``"__class__"``, ``"__init__"``, ``"__post_init__"``, ``"__new__"``, ``"__setattr__"``,
  ``"__delattr__"``, ``"__set__"`` and ``"__delete__"`` (so ``setattr(x, "__class__", K)``,
  ``getattr(K, "__post_init__")``, ``vars(K)["__init__"]`` and ``getattr(d, "__set__")`` are
  refused);
- naming ``__init__``, ``__post_init__`` or ``__new__`` at all, except ``super().__init__(...)``,
  ``super().__post_init__(...)`` and ``super().__new__(...)`` (zero-argument ``super``) written
  directly in a method of the same name. ``K.__post_init__(ctx)``, ``hook = K.__post_init__``
  and ``K.__init__(ctx, ...)`` would run a class's own code with a real context as ``self``: a
  frozen dataclass's generated ``__init__`` sets its fields with ``object.__setattr__``, so one
  with the same field names rewrites a ``UserContext`` in place. ``K.__new__(K)`` with ``K =
  type(ctx)`` makes a blank context without the issuer token;
- naming ``__setattr__``, ``__delattr__``, ``__set__`` or ``__delete__`` at all, with no
  exception: ``super(type(ctx), ctx).__setattr__`` (``super`` with or without arguments)
  reaches ``object.__setattr__`` past the frozen class's own, as does
  ``type(ctx).__mro__[-1].__setattr__``, and a slot descriptor's ``__set__`` or ``__delete__``
  (``type(ctx).role.__set__(ctx, "admin")``, also after ``d = K.role``) writes a field directly.

- (#89, ``user_context_rules``) ``__globals__``, ``__module__``, the module object
  ``ownvitals.context`` (imports, the package attribute, the strings for ``sys.modules`` and
  ``importlib``), an ``issuer=`` keyword or a ``**`` argument spelling ``"issuer"``, and
  ``copyreg``: the other routes to the issuer token or to a blank instance; and ``eval``,
  ``exec`` and ``compile``. Annotations follow the allow-list type grammar of
  ``user_context_types`` (strings in type positions parsed into the tree first).

One exception (Architect, 2026-10-31): the frozen-dataclass idiom ``object.__setattr__(self,
"field", value)`` written directly in the undecorated ``__post_init__(self, ...)`` method of a
class, with the bare ``self`` as the first argument, a string literal that does not start with
``__`` and is not one of ``UserContext``'s field names (``CONTEXT_FIELDS``, #89) as the second,
and ``self`` never rebound there. Named, that method runs only from the dataclass's own
``__init__``; it can still be reached without its name (``for f in vars(A).values(): f(ctx)``),
which is why a ``UserContext`` field name is never exempt.
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

This layer and the runtime barrier in ``ownvitals.context`` stop accidental misuse and make the
obvious forgeries fail CI. They do not stop deliberate obfuscation (a name built at run time, for
example ``getattr(K, "__post_" + "init__")``, ``"__SETATTR__".lower()`` or
``b"__setattr__".decode()``; class-level patching such as ``type(ctx).role = property(...)``;
``inspect``): ADR 0002 section 1 leaves deliberately malicious code inside the process to the
review gate. ``docs/dev/isolation.md`` lists these limits.
"""

import ast

from user_context_rules import CONTEXT_FIELDS, MODULE_STRINGS, NEVER_NAMED
from user_context_rules import findings as token_route_findings
from user_context_types import Spellings, metadata, splice_strings, type_findings

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
# Methods that run a class's own code on whatever is passed as ``self``; see the docstring.
HOOKS = {"__init__", "__post_init__"}
# Named only as ``super().<name>(...)`` written directly in a method of the same name.
SUPER_ONLY = {*HOOKS, "__new__"}
# Never named: each writes or deletes a field past the frozen class's own ``__setattr__``.
WRITERS = {"__setattr__", "__delattr__", "__set__", "__delete__"}
REFUSED_STRINGS = {"__class__", *SUPER_ONLY, *WRITERS}


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
            allowed |= {id(n) for n in nodes if _sets_own_field(n)}
    return allowed


def _sets_own_field(node: ast.AST) -> bool:
    """``object.__setattr__(self, "field", value)``: the bare ``self``, a literal field name that
    is not a dunder (never ``"__class__"``), no keywords and no ``*args``."""
    return (
        isinstance(node, ast.Call)
        and _dotted(node.func) == "object.__setattr__"
        and len(node.args) == 3
        and not node.keywords
        and isinstance(node.args[0], ast.Name)
        and node.args[0].id == "self"
        and isinstance(node.args[1], ast.Constant)
        and isinstance(node.args[1].value, str)
        and not node.args[1].value.startswith("__")
        and node.args[1].value not in CONTEXT_FIELDS
        and not isinstance(node.args[2], ast.Starred)
    )


def _super_hook_calls(tree: ast.AST) -> set[int]:
    """Ids of the attribute in ``super().__init__(...)``, ``super().__post_init__(...)`` and
    ``super().__new__(...)`` written directly in a method of the same name: the only uses of
    those names allowed."""
    allowed: set[int] = set()
    for function in ast.walk(tree):
        if not (
            isinstance(function, ast.FunctionDef | ast.AsyncFunctionDef)
            and function.name in SUPER_ONLY
        ):
            continue
        for node in _own_nodes(function):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == function.name
                and isinstance(node.func.value, ast.Call)
                and isinstance(node.func.value.func, ast.Name)
                and node.func.value.func.id == "super"
                and not node.func.value.args
                and not node.func.value.keywords
            ):
                allowed.add(id(node.func))
    return allowed


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
    # Annotations follow the type grammar of ``user_context_types``: strings in a type position
    # are parsed into the tree first, so every rule below judges them with the module's imports.
    # ``Annotated``'s metadata is code, evaluated with the annotation: every rule applies to it.
    types = Spellings(tree)
    roots, splice_refused, type_strings = splice_strings(tree, types)
    annotation_nodes: set[int] = set()
    for root in roots:
        annotation_nodes.update(id(n) for n in ast.walk(root))
    for root in roots:
        for element in metadata(root, types):
            annotation_nodes.difference_update(id(n) for n in ast.walk(element))

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
    found += token_route_findings(tree, annotation_nodes)
    found += splice_refused
    for line, value in type_strings:  # the string rules, on each string as written
        if value in REFUSED_STRINGS | TOKEN_NAMES | NEVER_NAMED | MODULE_STRINGS:
            found.append((line, f"string {value!r} in an annotation"))
    for root in roots:
        found += type_findings(root, types, TOKEN_NAMES)
    refused = _spellings(tree, ALWAYS_REFUSED) | (
        _spellings(tree, REFUSED_WITH_CONTEXT) if NAME in source else set()
    )
    post_init_setattrs = _post_init_setattrs(tree)
    super_hooks = _super_hook_calls(tree)
    called = {id(n.func) for n in ast.walk(tree) if isinstance(n, ast.Call)}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        callee = _dotted(node.func)
        if callee in REFUSED_BUILTIN_CALLS and id(node) in post_init_setattrs:
            continue  # the frozen-dataclass idiom on its own instance (see the module docstring)
        if callee in refused or callee in REFUSED_BUILTIN_CALLS:
            found.append((node.lineno, f"call to {callee}"))
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
        elif isinstance(node, ast.Attribute) and _dotted(node) in REFUSED_BUILTIN_CALLS:
            # A call is judged by the loop above (the frozen-dataclass idiom is exempt there).
            # ``s = object.__setattr__`` and a call through ``s`` later: refused where it is taken.
            if id(node) not in called:
                found.append((node.lineno, f"reference to {_dotted(node)}"))
        elif (
            isinstance(node, ast.Attribute)
            and node.attr in SUPER_ONLY
            and id(node) not in super_hooks
        ):
            found.append((node.lineno, f"explicit use of {node.attr}"))
        elif isinstance(node, ast.Name) and node.id in SUPER_ONLY:
            found.append((node.lineno, f"explicit use of {node.id}"))
        elif isinstance(node, ast.Attribute) and node.attr in WRITERS:
            found.append((node.lineno, f"use of {node.attr}"))
        elif isinstance(node, ast.Name) and node.id in WRITERS:
            found.append((node.lineno, f"use of {node.id}"))
        elif (
            isinstance(node, ast.Attribute)
            and node.attr == "__class__"
            and not isinstance(node.ctx, ast.Load)
        ):
            found.append((node.lineno, "assignment to __class__"))
        elif (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and node.value in REFUSED_STRINGS
        ):
            found.append((node.lineno, f"string {node.value!r}"))
    return sorted(set(found))
