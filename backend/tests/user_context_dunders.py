"""Guard 5's attribute rule (#89, the Architect's decisions on the eighth and ninth #115
reviews): dunder names are an allow-list, and an allowed dunder is read, never written.

A name of the form ``__x__`` is judged wherever the source writes it so that it can name an
attribute, a module global or a builtin. Each place is a **form**:

- ``read``: an attribute read (``f.__name__``), an f-string's included; ``write``: an attribute
  assigned, augmented or deleted (``f.__code__ = c``, ``del f.__name__``);
- ``name``: a name read (``__name__``); ``define``: a name bound (``__all__ = [...]``);
  ``write``: a name deleted;
- ``string``: an identifier-shaped segment of any ``str`` or ``bytes`` constant, f-string text
  included (``"0.__globals__"``, ``"f.__globals__"``, ``b"__globals__"``): standard-library
  calls (``string.Formatter().get_field``, ``operator.attrgetter``, ``pkgutil.resolve_name``,
  ``pydoc.locate``) look dotted and colon paths up themselves. As the name argument of
  ``setattr`` or ``delattr`` a segment is a ``write``; as that of ``getattr`` or ``hasattr`` its
  form is ``getattr:<the call>``, so an entry can allow exactly one call. A string that is a whole
  statement (a docstring) is inert and not judged;
- ``keyword``: a keyword argument; ``match``: a ``match`` class-pattern keyword (``case
  object(__globals__=g)`` reads the attribute); ``import``: a part of an imported name or its
  ``as`` name; ``global``: a ``global`` or ``nonlocal`` declaration.

``ALLOWED`` holds the exceptions: each with its reason, the forms it may take and ``only_in``
where it is not allowed everywhere. It is exactly what ``src`` uses today (the issuer modules
aside, which are not scanned), and a name used in one module is allowed only there
(``test_user_context_dunders`` pins all three). No entry allows ``write``. An exception lifts
only this rule: the other rules of guard 5 still judge the same name (``__init__`` only as
``super().__init__(...)``, ``object.__setattr__`` only in the frozen-dataclass idiom).

``string_findings`` applies the same segments to the issuer token's names and the module
``ownvitals.context``: ``"ownvitals.context:_ISSUER"`` names the token without a dunder.

Defining a method or a parameter with a dunder name is not naming an attribute, so ``def
__init__(self)`` stays allowed. A name built at run time (``"__glo" + "bals__"``) writes no
dunder: that is reflection, outside this rule's bound (``docs/dev/isolation.md``).
"""

import ast
import re
from collections.abc import Iterator
from dataclasses import dataclass

DUNDER = re.compile(r"__\w+__")
SEGMENT = re.compile(r"\w+")
# ``ownvitals.context`` followed by an attribute path (``.``) or ``pkgutil``'s ``:``.
CONTEXT_PATH = re.compile(r"\bownvitals\.context[.:]")
WRITE = "write"
ATTRIBUTE_SETTERS = {"setattr", "delattr"}
ATTRIBUTE_GETTERS = {"getattr", "hasattr"}


@dataclass(frozen=True)
class Allowed:
    reason: str
    forms: frozenset[str]
    only_in: frozenset[str] | None = None  # modules under ``src``; None means everywhere


def _forms(*forms: str) -> frozenset[str]:
    return frozenset(forms)


def _modules(*modules: str) -> frozenset[str]:
    return frozenset(f"ownvitals/{module}" for module in modules)


LOG = _modules("log.py")
ALLOWED: dict[str, Allowed] = {
    "__name__": Allowed(
        "a module's or a function's name, for loggers and `__main__` checks",
        _forms("read", "name"),
    ),
    "__main__": Allowed("the `if __name__ == '__main__'` entry points", _forms("string")),
    "__all__": Allowed("a package's public names", _forms("define")),
    "__init__": Allowed(
        "`super().__init__(...)`; guard 5's hook rule refuses every other use", _forms("read")
    ),
    "__aiter__": Allowed(
        "delegating `async for` to a wrapped stream", _forms("read"), _modules("egress/clients.py")
    ),
    "__cause__": Allowed("walking an exception chain", _forms("read"), LOG),
    "__context__": Allowed("walking an exception chain", _forms("read"), LOG),
    "__suppress_context__": Allowed(
        "`raise ... from` hides the context on purpose", _forms("read"), LOG
    ),
    "__dict__": Allowed("a log record's extra fields", _forms("read"), LOG),
    "__version__": Allowed(
        "the package version", _forms("define", "import", "name"), _modules("__init__.py", "web.py")
    ),
    "__code__": Allowed(
        "the egress hook recognises `socket.socketpair`'s own code",
        _forms("getattr:getattr(socket.socketpair, '__code__', None)"),
        _modules("egress/hook.py"),
    ),
    "__new__": Allowed(
        "the audit event name `socket.__new__`", _forms("string"), _modules("egress/hook.py")
    ),
    "__setattr__": Allowed(
        "`object.__setattr__(self, ...)` in a frozen dataclass's `__post_init__`; guard 5's "
        "idiom rule refuses every other use",
        _forms("read"),
        _modules("analysis/methods/series.py", "analysis/quality.py"),
    ),
    "__NAMES__": Allowed(
        "a placeholder in SQL text, replaced before the query runs",
        _forms("string"),
        _modules("db/queue_check.py"),
    ),
    "__SQL_TEXT_BUILTINS__": Allowed(
        "a placeholder in SQL text, replaced before the query runs",
        _forms("string"),
        _modules("db/queue_check.py"),
    ),
}


def is_dunder(name: str) -> bool:
    return DUNDER.fullmatch(name) is not None


def _text(node: ast.AST) -> str | None:
    """A ``str`` constant, or a ``bytes`` one decoded as ASCII (other bytes dropped)."""
    if isinstance(node, ast.Constant):
        if isinstance(node.value, str):
            return node.value
        if isinstance(node.value, bytes):
            return node.value.decode("ascii", "ignore")
    return None


def _string_forms(tree: ast.AST) -> tuple[set[int], dict[int, str]]:
    """The ids of the inert strings (a whole statement), and the form of each string that is
    the name argument of ``setattr``/``delattr`` (``write``) or ``getattr``/``hasattr``."""
    inert: set[int] = set()
    forms: dict[int, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Expr) and _text(node.value) is not None:
            inert.add(id(node.value))
        elif isinstance(node, ast.Call) and len(node.args) >= 2:
            func = node.func
            called = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
            if called in ATTRIBUTE_SETTERS:
                forms[id(node.args[1])] = WRITE
            elif called in ATTRIBUTE_GETTERS:
                forms[id(node.args[1])] = f"getattr:{ast.unparse(node)}"
    return inert, forms


def strings(tree: ast.AST) -> Iterator[tuple[ast.AST, str]]:
    """Every judged ``str``/``bytes`` constant with its text (docstrings and other inert
    statement strings aside)."""
    inert, _ = _string_forms(tree)
    for node in ast.walk(tree):
        text = _text(node)
        if text is not None and id(node) not in inert:
            yield node, text


def _named(node: ast.AST) -> list[tuple[str, str]]:
    """The names ``node`` writes where they can name an attribute, a global or a builtin, each
    with its form."""
    if isinstance(node, ast.Attribute):
        return [(node.attr, "read" if isinstance(node.ctx, ast.Load) else WRITE)]
    if isinstance(node, ast.Name):
        form = {ast.Load: "name", ast.Store: "define"}.get(type(node.ctx), WRITE)
        return [(node.id, form)]
    if isinstance(node, ast.keyword) and node.arg is not None:
        return [(node.arg, "keyword")]
    if isinstance(node, ast.MatchClass):
        return [(name, "match") for name in node.kwd_attrs]
    if isinstance(node, ast.alias):
        names = [*node.name.split("."), *([node.asname] if node.asname else [])]
        return [(name, "import") for name in names]
    if isinstance(node, ast.Global | ast.Nonlocal):
        return [(name, "global") for name in node.names]
    return []


def named_dunders(tree: ast.AST) -> list[tuple[int, str, str]]:
    """Every dunder ``tree`` names, allowed or not, with its line and form."""
    _, string_forms = _string_forms(tree)
    found: list[tuple[int, str, str]] = []
    for node in ast.walk(tree):
        for name, form in _named(node):
            if is_dunder(name):
                found.append((getattr(node, "lineno", 0), name, form))
    for node, text in strings(tree):
        form = string_forms.get(id(node), "string")
        found += [
            (getattr(node, "lineno", 0), segment, form)
            for segment in SEGMENT.findall(text)
            if is_dunder(segment)
        ]
    return found


def allowed_in(name: str, form: str, module: str | None) -> bool:
    entry = ALLOWED.get(name)
    return (
        form != WRITE
        and entry is not None
        and form in entry.forms
        and (entry.only_in is None or module in entry.only_in)
    )


def dunder_findings(tree: ast.AST, module: str | None) -> list[tuple[int, str]]:
    """Every dunder ``tree`` names outside ``ALLOWED`` (``module`` is the path under ``src``,
    ``ownvitals/log.py``; None for source that is no module of ``src``)."""
    return [
        (line, f"the dunder {name} as {form.split(':')[0]} (not allow-listed)")
        for line, name, form in named_dunders(tree)
        if not allowed_in(name, form, module)
    ]


def string_findings(tree: ast.AST, token_names: set[str]) -> list[tuple[int, str]]:
    """Strings with a segment that is one of the token's names, or a path into
    ``ownvitals.context`` (``"ownvitals.context:_ISSUER"``, ``"ownvitals.context.require"``)."""
    found: list[tuple[int, str]] = []
    for node, text in strings(tree):
        line = getattr(node, "lineno", 0)
        if any(segment in token_names for segment in SEGMENT.findall(text)):
            found.append((line, "a string naming the issuer token"))
        if CONTEXT_PATH.search(text):
            found.append((line, "a string with a path into ownvitals.context"))
    return found


def beyond_the_allow_list(found: list[tuple[int, str]]) -> list[tuple[int, str]]:
    """``found`` without this rule's findings: what the other rules of guard 5 refuse."""
    return [(line, why) for line, why in found if not why.endswith("(not allow-listed)")]
