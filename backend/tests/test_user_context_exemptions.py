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
