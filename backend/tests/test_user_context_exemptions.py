"""Guard 5, continued (#89, the Architect's fixes for the thirteenth #115 review): the two
exemptions pinned. ``self.context`` only as ``self.context.wrap_socket(...)`` in a method of a
class in ``mail.py``; ``ownvitals.context`` in an annotation only when it continues to a public
name; ``Literal``'s arguments only constants.

The review's forgeries run here to an admin context, and the scan must refuse each one's source.
"""

import inspect
import operator
import sys
import textwrap
import typing
from typing import Any, Literal, TypeAlias

import pytest

import ownvitals.context
from ctx import make_context
from ownvitals.context import require_context
from test_user_context_writers import body
from user_context_dunders import comment_findings
from user_context_scan import TOKEN_NAMES, scan_source

MAIL = "ownvitals/mail.py"
WEB = "ownvitals/web.py"
HOOK = "ownvitals/egress/hook.py"


def _admin(ctx: Any, m: Any) -> Any:
    tok = next(v for v in vars(m).values() if type(v) is object)
    kw = {"issuer": tok}
    return require_context(type(ctx)(ctx.user_id, "admin", **kw))


# B1: a value bound to the name ``self``.
def _mod(self: Any) -> Any:
    return self.context


def s1_method(ctx: Any) -> Any:
    return _admin(ctx, _mod(operator.itemgetter("ownvitals")(sys.modules)))


def s2_loop(ctx: Any) -> Any:
    m = None
    for k, self in list(sys.modules.items()):
        if k == "ownvitals":
            m = self.context
    return _admin(ctx, m)


def s3_module_level(ctx: Any) -> Any:
    self = operator.itemgetter("ownvitals")(sys.modules)
    return _admin(ctx, self.context)


def s4_lambda(ctx: Any) -> Any:
    return _admin(ctx, (lambda self: self.context)(operator.itemgetter("ownvitals")(sys.modules)))


# B2: the module as a type.
M: TypeAlias = ownvitals.context  # noqa: UP040  # pyright: ignore


def a8_type_alias(ctx: Any) -> Any:
    return _admin(ctx, M)


def _f(a: ownvitals.context) -> None: ...  # pyright: ignore


def a1_hints(ctx: Any) -> Any:
    return _admin(ctx, typing.get_type_hints(_f)["a"])


class _C:
    a: ownvitals.context  # pyright: ignore


def a2_class(ctx: Any) -> Any:
    return _admin(ctx, typing.get_type_hints(_C)["a"])


def _g(a: Literal[ownvitals.context]) -> None: ...  # pyright: ignore


def a5_literal(ctx: Any) -> Any:
    return _admin(ctx, typing.get_args(typing.get_type_hints(_g)["a"])[0])


FORGERIES = {
    f.__name__: f
    for f in (s1_method, s2_loop, s3_module_level, s4_lambda, a8_type_alias, a1_hints, a2_class)
}


@pytest.mark.parametrize("forgery", [*FORGERIES.values(), a5_literal], ids=[*FORGERIES, "a5"])
def test_each_forgery_runs_to_an_admin_context(forgery: Any) -> None:
    assert forgery(make_context(role="user")).role == "admin"


ROUTES = {
    "S1 a function's self": textwrap.dedent(inspect.getsource(_mod) + inspect.getsource(s1_method)),
    "S2 a loop's self": textwrap.dedent(inspect.getsource(s2_loop)),
    "S3 a module-level self": "import operator, sys\nself = operator.itemgetter('ownvitals')(sys"
    ".modules)\nm = self.context\n",
    "S4 a lambda's self": textwrap.dedent(inspect.getsource(s4_lambda)),
    "A8 a TypeAlias": "import ownvitals.context\nfrom typing import TypeAlias\n"
    "M: TypeAlias = ownvitals.context\nm = M\n",
    "A1 a parameter": "import ownvitals.context\ndef f(a: ownvitals.context): ...\n",
    "A2 a class attribute": "import ownvitals.context\nclass C:\n    a: ownvitals.context\n",
    "A5 Literal": "import ownvitals.context\nfrom typing import Literal\n"
    "def f(a: Literal[ownvitals.context]): ...\n",
    "a private name in it": "import ownvitals.context\ndef f(a: ownvitals.context._X): ...\n",
    "a token name in it": "import ownvitals.context\n"
    "def f(a: ownvitals.context.issuer_token): ...\n",
    "an enum member in Literal": "from typing import Literal\ndef f(a: Literal[Color.RED]): ...\n",
    # ``wrap_socket`` outside its one form.
    "wrap_socket outside a class": "def send(self, s):\n    return self.context.wrap_socket(s)\n",
    "wrap_socket with self rebound": "class M:\n    def send(self, s):\n        self = s\n"
    "        return self.context.wrap_socket(s)\n",
    "wrap_socket in a static method": "class M:\n    @staticmethod\n    def send(self, s):\n"
    "        return self.context.wrap_socket(s)\n",
    "wrap_socket in a nested lambda": "class M:\n    def send(self, s):\n"
    "        return (lambda self: self.context.wrap_socket(s))(s)\n",
    "wrap_socket with self rebound from a nested function": "class M:\n    def send(self, s):\n"
    "        def swap():\n            nonlocal self\n            self = s\n"
    "        return self.context.wrap_socket(s)\n",
    "wrap_socket on another first parameter": "class M:\n    def send(s, self):\n"
    "        return self.context.wrap_socket(s)\n",
    "another method of self.context": "class M:\n    def send(self, s):\n"
    "        return self.context.load_cert_chain(s)\n",
    "self.context without wrap_socket": "class M:\n    def send(self, s):\n"
    "        return self.context\n",
    # The thirteenth review's N1: X1, X4, X6, X7.
    "another comment with __cause__": "x = 1  # __cause__\n",
    "locate('ownvitals')": body("return locate('ownvitals')"),
    "bytes naming context": body("return getattr(o, b'context'.decode())"),
    "global event in _audit": "def _audit(event, args):\n    def swap():\n"
    "        global event\n    if event == 'socket.__new__':\n        return args\n",
}


@pytest.mark.parametrize("source", ROUTES.values(), ids=ROUTES.keys())
def test_the_scan_refuses_each_route(source: str) -> None:
    for module in (MAIL, WEB, HOOK, None):
        assert scan_source(source, module) != [], module


NEAR_MISSES = {
    "an annotation through the module": "import ownvitals.context\n"
    "def f(a: ownvitals.context.UserContext): ...\n",
    "a TypeAlias to a public name": "import ownvitals.context\nfrom typing import TypeAlias\n"
    "C: TypeAlias = ownvitals.context.UserContext\n",
    "constants in Literal": "from typing import Literal\ndef f(a: Literal['a', 1, None]): ...\n",
}


@pytest.mark.parametrize("source", NEAR_MISSES.values(), ids=NEAR_MISSES.keys())
def test_each_near_miss_stays_allowed(source: str) -> None:
    assert scan_source(source) == []


def test_wrap_socket_passes_only_in_mail_py() -> None:
    source = "class M:\n    def send(self, s):\n        return self.context.wrap_socket(s)\n"
    assert scan_source(source, MAIL) == []
    assert scan_source(source, WEB) != []


# X2: source that ``tokenize`` refuses fails closed.
def test_a_tokenize_failure_fails_closed() -> None:
    assert comment_findings("x = (\n", TOKEN_NAMES) != []


# The fourteenth review's B2: the pinned ``self.context.wrap_socket(...)`` called unbound on the
# package, and ``AttributeError.obj`` hands back the context module. The package's name is no
# longer written as a string, so none of these has the package to pass.
class _Conn:
    def go(self, s: Any) -> Any:
        return self.context.wrap_socket(s)  # pyright: ignore[reportAttributeAccessIssue]


def u1_unbound(ctx: Any) -> Any:
    try:
        _Conn.go(operator.itemgetter("ownvitals")(sys.modules), None)
    except AttributeError as e:
        return _admin(ctx, e.obj)


def test_u1_runs_to_an_admin_context() -> None:
    assert u1_unbound(make_context(role="user")).role == "admin"


CONN = "class Conn:\n    def go(self, s):\n        return self.context.wrap_socket(s)\n"
U_ROUTES = {
    "U1 an unbound call": CONN + textwrap.dedent(inspect.getsource(u1_unbound)),
    "U2 a loop's key": CONN + "for k, v in list(sys.modules.items()):\n"
    "    if k == 'ownvitals':\n        try:\n            Conn.go(v, None)\n"
    "        except AttributeError as e:\n            m = e.obj\n",
    "U5 a class decorator": "import operator, sys\ndef grab(cls):\n    try:\n"
    "        cls.go(operator.itemgetter('ownvitals')(sys.modules), None)\n"
    "    except AttributeError as e:\n        cls.m = e.obj\n    return cls\n"
    "@grab\n" + CONN,
    "U6 a staticmethod after the body": CONN + "Conn.go = staticmethod(Conn.go)\n"
    "Conn().go(operator.itemgetter('ownvitals')(sys.modules), None)\n",
    "a default on self": "class Conn:\n"
    "    def go(self=operator.itemgetter('ownvitals')(sys.modules), s=None):\n"
    "        return self.context.wrap_socket(s)\n",
    "bytes": "m = sys.modules[b'ownvitals'.decode()]\n",
    "an f-string": "m = operator.itemgetter(f'ownvitals')(sys.modules)\n",
    "version elsewhere in its module": "x = version('ownvitals')\ny = 'ownvitals'\n",
    "version twice": "x = version('ownvitals')\ny = version('ownvitals')\n",
    "version with another argument": "x = version('ownvitals', 'ownvitals')\n",
}


@pytest.mark.parametrize("source", U_ROUTES.values(), ids=U_ROUTES.keys())
def test_the_package_name_is_never_a_string(source: str) -> None:
    for module in (MAIL, WEB, HOOK, "ownvitals/__init__.py", None):
        assert scan_source(source, module) != [], module


# Each pinned ``"ownvitals"`` passes in its own module only.
PINNED_NAMES = {
    "ownvitals/__init__.py": "from importlib.metadata import version\nv = version('ownvitals')\n",
    "ownvitals/auth/totp.py": "ISSUER = 'ownvitals'\n",
    "ownvitals/egress/code_exchange.py": "H = {'Accept': 'x', 'User-Agent': 'ownvitals'}\n",
    "ownvitals/web.py": "app = FastAPI(title='ownvitals')\n",
}


@pytest.mark.parametrize("module", PINNED_NAMES)
def test_a_pinned_package_name_passes_only_in_its_module(module: str) -> None:
    for other in (*PINNED_NAMES, MAIL, None):
        assert (scan_source(PINNED_NAMES[module], other) == []) == (other == module), other


def test_a_dotted_name_under_the_package_stays_allowed() -> None:
    assert scan_source("import logging\nlog = logging.getLogger('ownvitals.web')\n", MAIL) == []


# The fourteenth review's N1 (Y6): ``self`` rebound by every binding form refuses the pin.
REBINDINGS = {
    "walrus": "(self := s)",
    "for": "for self in [s]:\n            pass",
    "with": "with s as self:\n            pass",
    "except": "try:\n            pass\n        except Exception as self:\n            pass",
    "match": "match s:\n            case self:\n                pass",
    "del": "del self",
}


@pytest.mark.parametrize("rebinding", REBINDINGS.values(), ids=REBINDINGS.keys())
def test_wrap_socket_with_self_rebound_is_refused(rebinding: str) -> None:
    source = (
        f"class M:\n    def send(self, s):\n        {rebinding}\n"
        "        return self.context.wrap_socket(s)\n"
    )
    assert scan_source(source, MAIL) != []


# The fourteenth review's B1: ``errors.py``'s docstring names two dunders, pinned to its module.
ERRORS_DOCSTRING = '"""Raised after the except blocks: no ``__cause__``, no ``__context__``."""\n'


def test_the_errors_docstring_passes_only_in_errors_py() -> None:
    assert scan_source(ERRORS_DOCSTRING, "ownvitals/withings/errors.py") == []
    for other in (WEB, MAIL, None):
        assert scan_source(ERRORS_DOCSTRING, other) != [], other
    assert scan_source('"""no ``__str__``"""\n', "ownvitals/withings/errors.py") != []
