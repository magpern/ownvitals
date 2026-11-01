# Guard 5's attribute rule

Part of guard 5 (`tests/user_context_scan.py`), split from [isolation.md](isolation.md), which
holds the guard's threat model, its scope (the bound every review classifies findings against),
the type grammar for annotations and the Known limits.

**Dunder names follow an allow-list: guard 5's attribute rule** (#89, Architect's decisions on
the eighth and ninth #115 reviews, `tests/user_context_dunders.py`). Every route the reviews found to the
token's module, class or setter named a dunder (`__globals__`, `__module__`, `__setattr__`,
`__dict__`), and refusing them one spelling at a time let each new syntax reopen them. So a name
of the form `__x__` is refused wherever the source can name an attribute, a module global or a
builtin with it:

- an attribute (`f.__globals__`), an f-string's included;
- a name (`__import__`, `__builtins__`, `__module__` in a class body), a `global` or
  `nonlocal` declaration, a parameter (`lambda __name__: ...`), and an `except ... as` or
  `match` capture;
- every identifier-shaped segment of every `str` and `bytes` constant (split on `.`, `:`, `/`
  and any other character that can't be in an identifier), f-string text included. Several
  standard-library calls look a dotted or colon path up themselves:
  `string.Formatter().get_field("0.__globals__", ...)`, `"{0.__module__}".format(x)`,
  `operator.attrgetter("f.__globals__")`, `getattr(f, b"__globals__".decode())`;
- a docstring (a string that is a whole statement) or a comment: neither runs, but source text
  can be read back (`traceback.extract_stack()[-1].line`, `warnings.formatwarning`, `open`), so
  a dunder in one needs a row in the table like any other use: `docstring` for a docstring
  (limited by `only_in`), `docstring:<module>` for a docstring in one module outside `only_in`
  (that form pins its own module; `withings/errors.py` uses it), and for a comment its exact
  module and text (`comment:<module>:<text>`). The readers
  of source text are refused outright: `inspect`, `pydoc`, `linecache` and `tokenize` (any
  import form, name, attribute, or the module's name as a string), `__doc__`, and
  `importlib.util` (`find_spec(...).origin` finds a module's file), which is allowed only as
  `import importlib.util` and the one call `importlib.util.find_spec('uvloop')` in
  `egress/hook.py`. Reading a file with `open` stays allowed (`src` reads its key and SQL files);
  that is Known limit (c);
- a keyword argument, and a `match` class-pattern keyword: `case object(__globals__=g)` reads
  the attribute and binds it;
- an imported name (`from ownvitals import __version__`).

The same segments are checked against the issuer token's names and the module, in every
string, docstrings and comments included (comments through `tokenize`): a string with a segment `_ISSUER` or `issuer_token`, or naming the
module `ownvitals.context` with `.` or `:` on either side (`"ownvitals.context"`,
`"ownvitals:context"`, `"ownvitals.context:_ISSUER"`), is refused
(`pkgutil.resolve_name("ownvitals:context")`, `pydoc.locate("ownvitals.context._ISSUER")`). A
dotted logger name such as `"ownvitals.db.engine"`, another name (`"ownvitals.contexts"`) or SQL
text stays allowed.

The module is refused as an attribute of its package too, however the package is reached
(#89, the eleventh and twelfth #115 reviews):

- the name of the package (`import ownvitals`, or the root that `import ownvitals.db` binds) may
  be loaded only as the root of an attribute chain whose next part isn't `context`
  (`ownvitals.db.engine`). Bound to another name (`o = ownvitals`), passed (`vars(ownvitals)`,
  `dir(ownvitals)`, `getattr(ownvitals, ...)`) or subscripted, it is refused;
- the attribute `context` is refused everywhere outside type annotations
  (`importlib.import_module(".", "ownvitals").context`, `p.context`, `sys.modules[...].context`)
  except in one form: `self.context.wrap_socket(...)` in `ownvitals/mail.py`, in a method's own
  body, where `self` is the first parameter of an undecorated method in a class body and is never
  rebound (nor named by `nonlocal`/`global`). Any other variable called `self` (a function's, a
  loop's, a module-level one, a lambda's) is refused;
- in a type annotation (a `TypeAlias` value included), `ownvitals.context` is allowed only when
  it continues to a public name of the module (`ownvitals.context.UserContext`: no leading `_`,
  no token name), never on its own (`def f(a: ownvitals.context)`, `M: TypeAlias =
  ownvitals.context`: `get_type_hints` or the alias hands the module out);
- a string, docstrings and comments aside, with `context` as an identifier segment is refused:
  `operator.attrgetter("context")`, `getattr(o, "context")`, `k == "context"`, `"..context"`.
  `src` has none;
- the bare `"ownvitals"` is refused as any argument of a lookup (`import_module`,
  `resolve_name`, `locate`, `find_spec`, `.get`, `.pop`, `.setdefault`, `package=` included)
  and as a subscript key (`sys.modules["ownvitals"]`);
- a `str` or `bytes` constant exactly `"ownvitals"` (f-string parts included) is refused except
  four named pins, each once in its module: `version("ownvitals")` in `__init__.py`,
  `ISSUER = "ownvitals"` in `auth/totp.py`, the `'User-Agent'` value in `egress/code_exchange.py`,
  and `title="ownvitals"` in `web.py`. There is no general exemption for that string.
  `"ownvitals.web"` is another string;
- `import_module` with a relative name (a leading `.`) is refused, even a literal one.

A plain `import ownvitals.context` stays allowed for annotations
(`c: ownvitals.context.UserContext`).

**A dunder inside a string is pinned to its exact use.** A string's form is its syntactic
position: the name argument of one exact `getattr` call, a direct operand of one exact comparison,
the value of one assignment (the SQL placeholders' `QUEUE_QUERY = ...`), or anywhere else. A
comparison whose other operand is its function's own first parameter, never rebound there, is
pinned to that function too: `"socket.__new__"` is allowed only as `event == "socket.__new__"` in
`_audit(event, args)`, with no default and not rebound from a nested function or class
(`nonlocal event`), so an object written next to the string (a recording `__eq__`, a
`MagicMock`) can't receive it. The table allows each such dunder in one position, once per
module, so the same constant passed on, sliced, partitioned
(`"socket.__new__".partition(".")[2]` fed to `setattr`) or compared a second time is refused.
Calling the real `_audit` with a recording object writes no dunder: that is Known limit (b).

**An allowed dunder is read, never written.** Assigning, augmenting or deleting one as an
attribute (`require_context.__code__ = (lambda c: c).__code__` makes `require_context` accept
anything), deleting it as a name, and naming it as the name argument of `setattr` or `delattr`
are refused whatever the table says. Each entry also lists the forms it may take (an attribute
read, a name read, a module-level definition such as `__all__ = [...]`, a string in its one
position, an import), and `__code__` is allowed only as the one call `getattr(socket.socketpair, "__code__",
None)` in `egress/hook.py`. `super().__init__(...)` and the `object.__setattr__` idiom are
reads of the attribute, judged by the rules in [isolation.md](isolation.md).

A comparison is no exception: `x.__module__ == r` calls `r.__eq__` with the name when `str`'s
returns `NotImplemented`, and `x.__module__ in r` calls `r.__contains__` (a list compares each
element), so the other operand receives the value. Defining a dunder method or parameter
(`def __eq__(self, other)`) names no attribute and stays allowed; assigning one in a class body
(`__eq__ = list.append`) does not.

The exceptions, each with its reason, are exactly what `src` uses today (the issuer modules
aside, which are not scanned); a name used in one module is allowed only there.
`test_user_context_dunders.py` fails if an entry is unused, takes a form `src` doesn't use, or
`src` uses one elsewhere:

| Name | Forms | Where | Why |
|---|---|---|---|
| `__name__` | attribute and name read | everywhere | a module's or a function's name, for loggers and `__main__` checks |
| `__main__` | the comparison `__name__ == "__main__"`; docstring | everywhere | the `if __name__ == "__main__"` entry points |
| `__all__` | definition | everywhere | a package's public names |
| `__init__` | attribute read | everywhere | `super().__init__(...)`; the hook rule in isolation.md refuses any other use |
| `__aiter__` | attribute read | `egress/clients.py` | delegating `async for` to a wrapped stream |
| `__cause__`, `__context__`, `__suppress_context__` | attribute read; docstring for the first two in `log.py`; `docstring:ownvitals/withings/errors.py` for `__cause__` and `__context__` (that module's docstring: the client raises these after its `except` blocks, no `__cause__`, no `__context__`, ADR 0004 §8); the exact comments that name them in `log.py`, `mail.py`, `crypto/envelope.py` and `egress/core.py` | `log.py` for the read and the plain docstring (`docstring:<module>` and comments pin their own modules) | walking an exception chain |
| `__str__` | docstring | `log.py` | a docstring naming the method |
| `__call__` | the one comment `# unused: __call__ reads the files directly` | `config.py` | a comment on a settings source |
| `__dict__` | attribute read | `log.py` | a log record's extra fields |
| `__version__` | definition, import, name read | `__init__.py`, `web.py` | the package version |
| `__code__` | `getattr(socket.socketpair, "__code__", None)` only | `egress/hook.py` | recognising `socket.socketpair`'s own code |
| `__new__` | the comparison `event == "socket.__new__"` in `_audit(event, ...)`; docstring | `egress/hook.py` | the audit event name `"socket.__new__"` |
| `__setattr__` | attribute read | `analysis/methods/series.py`, `analysis/quality.py` | the frozen-dataclass idiom in `__post_init__`; the idiom rule in isolation.md refuses any other use |
| `__NAMES__`, `__SQL_TEXT_BUILTINS__` | the assignment `QUEUE_QUERY = ...` | `db/queue_check.py` | placeholders in SQL text, replaced before the query runs |

An exception lifts only this rule: the rules in [isolation.md](isolation.md) still judge the same name. A new use of a
dunder in `src` needs the Architect and a new row. Because `__module__` is not on the list,
`egress.hook` checks the running loop by its class (`type(loop) is asyncio.SelectorEventLoop`),
not by its module's name.
