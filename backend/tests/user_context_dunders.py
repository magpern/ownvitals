"""Guard 5's attribute rule (#89, the Architect's decisions on the eighth and ninth #115
reviews): dunder names are an allow-list, and an allowed dunder is read, never written.

A name of the form ``__x__`` is judged wherever the source writes it so that it can name an
attribute, a module global or a builtin. Each place is a **form**:

- ``read``: an attribute read (``f.__name__``), an f-string's included; ``write``: an attribute
  assigned, augmented or deleted (``f.__code__ = c``, ``del f.__name__``);
- ``name``: a name read (``__name__``); ``define``: a name bound (``__all__ = [...]``);
  ``write``: a name deleted;
- an identifier-shaped segment of any ``str`` or ``bytes`` constant, f-string text included
  (``"0.__globals__"``, ``"f.__globals__"``, ``b"__globals__"``): standard-library calls
  (``string.Formatter().get_field``, ``operator.attrgetter``, ``pkgutil.resolve_name``,
  ``pydoc.locate``) look dotted and colon paths up themselves. Its form is its exact position
  (``_string_forms``): ``write`` as the name argument of ``setattr``/``delattr``,
  ``getattr:<the call>``, ``compare:<the comparison>``, ``assign:<the targets>``, or ``string``
  anywhere else; so an entry allows one exact constant in one place, and the same constant passed
  on, sliced or partitioned is refused. A comparison with its function's own unrebound first
  parameter is pinned to that function (``compare:_audit(event):...``), and a pinned position is
  allowed once per module;
- ``docstring``: a segment of a string that is a whole statement: it never runs, but ``open``
  can read source text back, so it needs its own row. Docstrings and comments (``tokenize``) are
  also judged for the token's names and the context module (``comment_findings``);
- ``comment:<module>:<text>``: a segment of one exact comment in one module (``traceback`` and
  ``warnings`` read a comment back); it pins its own module, so ``only_in`` doesn't apply;
- ``docstring:<module>``: a docstring segment in one module outside ``only_in`` (the dunders
  ``withings/errors.py``'s docstring names); like a comment's form it pins its own module;
- ``parameter``: a parameter's name; ``define`` also covers an ``except ... as`` or ``match``
  capture;
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
import io
import re
import tokenize
from collections.abc import Iterator
from dataclasses import dataclass

DUNDER = re.compile(r"__\w+__")
SEGMENT = re.compile(r"\w+")
# The module ``ownvitals.context`` in any string, ``pkgutil``'s colon spellings included
# (``"ownvitals:context"``, ``"ownvitals.context:_ISSUER"``); ``"ownvitals.contexts"`` is
# another name.
CONTEXT_PATH = re.compile(r"\bownvitals[.:]context\b")
WRITE = "write"
COMMENT = "comment:"
DOCSTRING = "docstring"
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


def _comment(module: str, text: str) -> str:
    """The form of one exact comment in one module."""
    return f"comment:ownvitals/{module}:{text}"


def _docstring(module: str) -> str:
    """The form of a docstring segment in one module outside ``only_in``."""
    return f"{DOCSTRING}:ownvitals/{module}"


LOG = _modules("log.py")
# ``errors.py``'s module docstring: "no ``__cause__``, no ``__context__``" (ADR 0004 §8).
ERRORS_DOCSTRING = _docstring("withings/errors.py")
ALLOWED: dict[str, Allowed] = {
    "__name__": Allowed(
        "a module's or a function's name, for loggers and `__main__` checks",
        _forms("read", "name"),
    ),
    "__main__": Allowed(
        "the `if __name__ == '__main__'` entry points",
        _forms("compare:__name__ == '__main__'", "docstring"),
    ),
    "__all__": Allowed("a package's public names", _forms("define")),
    "__init__": Allowed(
        "`super().__init__(...)`; guard 5's hook rule refuses every other use", _forms("read")
    ),
    "__aiter__": Allowed(
        "delegating `async for` to a wrapped stream", _forms("read"), _modules("egress/clients.py")
    ),
    "__cause__": Allowed(
        "walking an exception chain",
        _forms(
            "read",
            "docstring",
            ERRORS_DOCSTRING,
            _comment(
                "log.py", "# hidden on purpose, so it is not followed. __cause__ still is, so a"
            ),
            _comment(
                "mail.py",
                "# Raised after the except block: no __cause__ and no __context__"
                " to carry the text.",
            ),
        ),
        LOG,
    ),
    "__context__": Allowed(
        "walking an exception chain",
        _forms(
            "read",
            "docstring",
            ERRORS_DOCSTRING,
            _comment(
                "crypto/envelope.py",
                "# Raised after the block, so DecryptFailed carries no __context__.",
            ),
            _comment(
                "egress/core.py",
                "# Outside the except block: no __context__"
                " (the ValueError's text holds URL parts).",
            ),
            _comment(
                "mail.py",
                "# Raised after the except block: no __cause__ and no __context__"
                " to carry the text.",
            ),
        ),
        LOG,
    ),
    "__call__": Allowed(
        "a comment on a settings source",
        _forms(_comment("config.py", "# unused: __call__ reads the files directly")),
        _modules("config.py"),
    ),
    "__str__": Allowed("a docstring naming the method", _forms("docstring"), LOG),
    "__suppress_context__": Allowed(
        "`raise ... from` hides the context on purpose",
        _forms(
            "read",
            _comment(
                "log.py",
                "# ``raise ... from None`` (or ``from x``) sets __suppress_context__:"
                " the context was",
            ),
        ),
        LOG,
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
        "the audit event name `socket.__new__`",
        _forms("compare:_audit(event):event == 'socket.__new__'", "docstring"),
        _modules("egress/hook.py"),
    ),
    "__setattr__": Allowed(
        "`object.__setattr__(self, ...)` in a frozen dataclass's `__post_init__`; guard 5's "
        "idiom rule refuses every other use",
        _forms("read"),
        _modules("analysis/methods/series.py", "analysis/quality.py"),
    ),
    "__slots__": Allowed(
        "a class declares its fields",
        _forms("define"),
        _modules("admin/db.py"),
    ),
    "__NAMES__": Allowed(
        "a placeholder in SQL text, replaced before the query runs",
        _forms("assign:QUEUE_QUERY"),
        _modules("db/queue_check.py"),
    ),
    "__SQL_TEXT_BUILTINS__": Allowed(
        "a placeholder in SQL text, replaced before the query runs",
        _forms("assign:QUEUE_QUERY"),
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


_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)
_FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef)


def own_nodes(function: ast.FunctionDef | ast.AsyncFunctionDef) -> list[ast.AST]:
    """The nodes of ``function``'s body, not those of functions, lambdas or classes inside it."""
    found: list[ast.AST] = []
    pending: list[ast.AST] = list(function.body)
    while pending:
        node = pending.pop()
        if isinstance(node, _SCOPES):
            continue
        found.append(node)
        pending.extend(ast.iter_child_nodes(node))
    return found


def binds(node: ast.AST, name: str) -> bool:
    """Any binding of ``name``: assignment, ``for``, ``with``, walrus, ``del``, ``except ...
    as``, ``import ... as``, a ``match`` capture, a ``global``/``nonlocal`` declaration."""
    if isinstance(node, ast.Name):
        return node.id == name and not isinstance(node.ctx, ast.Load)
    if isinstance(node, ast.ExceptHandler | ast.MatchAs | ast.MatchStar):
        return node.name == name
    if isinstance(node, ast.MatchMapping):
        return node.rest == name
    if isinstance(node, ast.alias):
        return (node.asname or node.name.split(".")[0]) == name
    if isinstance(node, ast.Global | ast.Nonlocal):
        return name in node.names
    return False


def _own_parameter_comparisons(tree: ast.AST) -> dict[int, str]:
    """``compare:<function>(<parameter>):<comparison>`` for each comparison whose operands are
    string constants and the function's own first parameter, never rebound in the function
    (``event == "socket.__new__"`` in ``_audit(event, args)``): the other operand is then
    whatever the function's callers pass, not an object written next to the string."""
    forms: dict[int, str] = {}
    for function in ast.walk(tree):
        if not isinstance(function, _FUNCTIONS):
            continue
        positional = [*function.args.posonlyargs, *function.args.args]
        if not positional:
            continue
        first = positional[0].arg
        nodes = own_nodes(function)
        if any(binds(node, first) for node in nodes):
            continue
        if len(function.args.defaults) >= len(positional):
            continue  # a default: the function can run on an object written next to it
        if any(
            isinstance(node, ast.Nonlocal | ast.Global) and first in node.names
            for node in ast.walk(function)
        ):
            continue  # rebound from a nested function or class
        for node in nodes:
            operands = [node.left, *node.comparators] if isinstance(node, ast.Compare) else []
            if operands and all(
                _text(o) is not None or (isinstance(o, ast.Name) and o.id == first)
                for o in operands
            ):
                forms[id(node)] = f"compare:{function.name}({first}):{ast.unparse(node)}"
    return forms


def _string_forms(tree: ast.AST) -> tuple[set[int], dict[int, str], dict[int, int]]:
    """The ids of the inert strings (a whole statement), the form of every other string, and
    the node each pinned string is anchored to (its call, comparison or assignment):

    - ``write``: the name argument of ``setattr``/``delattr``;
    - ``getattr:<the call>``: the name argument of ``getattr``/``hasattr``;
    - ``compare:<function>(<parameter>):<the comparison>``: a direct operand of a comparison
      with a function's own unrebound first parameter (``_own_parameter_comparisons``);
    - ``compare:<the comparison>``: a direct operand of any other comparison;
    - ``assign:<the targets>``: anywhere in the value of an assignment (``_SQL = "..."``);
    - ``string``: anywhere else (passed on, sliced, partitioned).

    So an entry pins a string to one exact constant in one syntactic position, and
    ``dunder_findings`` allows that position once per module."""
    inert: set[int] = set()
    forms: dict[int, str] = {}
    anchors: dict[int, int] = {}

    def pin(sub: ast.AST, form: str, anchor: ast.AST) -> None:
        if id(sub) not in forms:
            forms[id(sub)] = form
            anchors[id(sub)] = id(anchor)

    # The most specific position first: ``setdefault`` keeps the first form a string gets.
    for node in ast.walk(tree):
        if isinstance(node, ast.Expr) and _text(node.value) is not None:
            inert.add(id(node.value))
        elif isinstance(node, ast.Call) and len(node.args) >= 2:
            func = node.func
            called = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
            if called in ATTRIBUTE_SETTERS:
                forms[id(node.args[1])] = WRITE
                anchors[id(node.args[1])] = id(node)
            elif called in ATTRIBUTE_GETTERS:
                pin(node.args[1], f"getattr:{ast.unparse(node)}", node)
    own = _own_parameter_comparisons(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare):
            form = own.get(id(node), f"compare:{ast.unparse(node)}")
            for operand in (node.left, *node.comparators):
                if _text(operand) is not None:
                    pin(operand, form, node)
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            targets = " = ".join(ast.unparse(t) for t in node.targets)
            for sub in ast.walk(node.value):
                if _text(sub) is not None:
                    pin(sub, f"assign:{targets}", node)
    return inert, forms, anchors


def strings(tree: ast.AST, *, inert_too: bool = False) -> Iterator[tuple[ast.AST, str]]:
    """Every ``str``/``bytes`` constant with its text; docstrings and other strings that are a
    whole statement only with ``inert_too``."""
    inert, _, _ = _string_forms(tree)
    for node in ast.walk(tree):
        text = _text(node)
        if text is not None and (inert_too or id(node) not in inert):
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
    if isinstance(node, ast.arg):
        return [(node.arg, "parameter")]
    if isinstance(node, ast.ExceptHandler | ast.MatchAs | ast.MatchStar) and node.name:
        return [(node.name, "define")]
    if isinstance(node, ast.MatchMapping) and node.rest:
        return [(node.rest, "define")]
    return []


def _anchored_dunders(tree: ast.AST) -> list[tuple[int, str, str, int | None]]:
    """Every dunder ``tree`` names with its line, form and, for a pinned string, its anchor."""
    inert, string_forms, anchors = _string_forms(tree)
    found: list[tuple[int, str, str, int | None]] = []
    for node in ast.walk(tree):
        for name, form in _named(node):
            if is_dunder(name):
                found.append((getattr(node, "lineno", 0), name, form, None))
    for node, text in strings(tree, inert_too=True):
        form = "docstring" if id(node) in inert else string_forms.get(id(node), "string")
        found += [
            (getattr(node, "lineno", 0), segment, form, anchors.get(id(node)))
            for segment in SEGMENT.findall(text)
            if is_dunder(segment)
        ]
    return found


def named_dunders(tree: ast.AST) -> list[tuple[int, str, str]]:
    """Every dunder ``tree`` names, allowed or not, with its line and form (``docstring`` for
    a string that is a whole statement: ``open`` and ``linecache`` could read it back)."""
    return [(line, name, form) for line, name, form, _ in _anchored_dunders(tree)]


def allowed_in(name: str, form: str, module: str | None) -> bool:
    """A comment's form names its module and text (``comment:<module>:<text>``), so it pins the
    module itself; a docstring is also allowed where its entry has ``docstring:<module>``; every
    other form is checked against ``only_in``."""
    entry = ALLOWED.get(name)
    if form == DOCSTRING and entry is not None and f"{DOCSTRING}:{module}" in entry.forms:
        return True
    return (
        form != WRITE
        and entry is not None
        and form in entry.forms
        and (entry.only_in is None or module in entry.only_in or form.startswith(COMMENT))
    )


def dunder_findings(tree: ast.AST, module: str | None) -> list[tuple[int, str]]:
    """Every dunder ``tree`` names outside ``ALLOWED`` (``module`` is the path under ``src``,
    ``ownvitals/log.py``; None for source that is no module of ``src``)."""
    found: list[tuple[int, str]] = []
    seen: dict[tuple[str, str], int] = {}
    for line, name, form, anchor in _anchored_dunders(tree):
        if not allowed_in(name, form, module):
            found.append((line, f"the dunder {name} as {form.split(':')[0]} (not allow-listed)"))
        elif anchor is not None and seen.setdefault((name, form), anchor) != anchor:
            # A pinned position is allowed once: a second copy of it is another use.
            found.append((line, f"the dunder {name} pinned once, here again (not allow-listed)"))
    return found


def string_findings(tree: ast.AST, token_names: set[str]) -> list[tuple[int, str]]:
    """Strings, docstrings included (``inspect`` reads them back), with a segment that is one of
    the token's names, or naming the module ``ownvitals.context`` (``"ownvitals:context"``,
    ``"ownvitals.context:_ISSUER"``)."""
    found: list[tuple[int, str]] = []
    for node, text in strings(tree, inert_too=True):
        line = getattr(node, "lineno", 0)
        if any(segment in token_names for segment in SEGMENT.findall(text)):
            found.append((line, "a string naming the issuer token"))
        if CONTEXT_PATH.search(text):
            found.append((line, "a string with a path into ownvitals.context"))
    return found


def _comments(source: str) -> list[tuple[int, str]] | None:
    """Each comment with its line, or None when ``tokenize`` refuses the source."""
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
    except (tokenize.TokenError, SyntaxError):
        return None
    return [(t.start[0], t.string) for t in tokens if t.type == tokenize.COMMENT]


def comment_dunders(source: str, module: str | None) -> list[tuple[int, str, str]]:
    """Every dunder a comment names, with its line and its form ``comment:<module>:<text>``:
    ``traceback``, ``warnings`` or ``linecache`` read a comment back as a value."""
    return [
        (line, segment, f"{COMMENT}{module}:{text}")
        for line, text in _comments(source) or []
        for segment in SEGMENT.findall(text)
        if is_dunder(segment)
    ]


def comment_findings(
    source: str, token_names: set[str], module: str | None = None
) -> list[tuple[int, str]]:
    """Comments (``tokenize``) naming the issuer token, the module ``ownvitals.context``, or a
    dunder outside its one pinned comment."""
    comments = _comments(source)
    if comments is None:
        return [(0, "source that tokenize refuses")]
    found: list[tuple[int, str]] = []
    for line, text in comments:
        if any(segment in token_names for segment in SEGMENT.findall(text)):
            found.append((line, "a comment naming the issuer token"))
        if CONTEXT_PATH.search(text):
            found.append((line, "a comment naming ownvitals.context"))
    found += [
        (line, f"the dunder {name} in a comment (not allow-listed)")
        for line, name, form in comment_dunders(source, module)
        if not allowed_in(name, form, module)
    ]
    return found


def beyond_the_allow_list(found: list[tuple[int, str]]) -> list[tuple[int, str]]:
    """``found`` without this rule's findings: what the other rules of guard 5 refuse."""
    return [(line, why) for line, why in found if not why.endswith("(not allow-listed)")]
