import copy
import dataclasses
import pickle
import uuid
from types import SimpleNamespace
from typing import Annotated

import httpx
import pytest
from fastapi import Body, Depends, FastAPI, Query
from pydantic import BaseModel, TypeAdapter

from ctx import make_context
from ownvitals.context import (  # pyright: ignore[reportPrivateUsage]
    _ISSUER,
    UserContext,
    issuer_token,
    require_context,
)

USER = uuid.uuid4()
CONNECTION = uuid.uuid4()


def build() -> UserContext:
    return make_context(USER, "user", connections={("withings", CONNECTION)})


def test_a_context_is_immutable_and_hashable() -> None:
    ctx, same = build(), build()
    assert hash(ctx) == hash(same)
    assert ctx == same
    assert len({ctx, same}) == 1
    assert ctx.connection("withings") == CONNECTION
    assert ctx.connection("google") is None
    with pytest.raises(dataclasses.FrozenInstanceError):
        ctx.role = "admin"  # pyright: ignore[reportAttributeAccessIssue]
    with pytest.raises((AttributeError, TypeError)):  # slots + frozen
        ctx.extra = 1  # pyright: ignore[reportAttributeAccessIssue]


def test_defaults_are_empty() -> None:
    ctx = make_context(USER, "admin")
    assert ctx.connections == frozenset()
    assert isinstance(hash(ctx), int)


def test_the_issuer_token_is_the_module_private_one() -> None:
    assert issuer_token() is _ISSUER


FORGERIES = {
    "direct call": lambda ctx: UserContext(user_id=uuid.uuid4(), role="admin"),
    "type(ctx)(...)": lambda ctx: type(ctx)(user_id=uuid.uuid4(), role="admin"),
    "ctx.__class__(...)": lambda ctx: ctx.__class__(uuid.uuid4(), "admin"),
    "dataclasses.replace": lambda ctx: dataclasses.replace(ctx, user_id=uuid.uuid4()),
    "wrong token": lambda ctx: UserContext(uuid.uuid4(), "admin", issuer=object()),
    "token None": lambda ctx: UserContext(uuid.uuid4(), "admin", issuer=None),
}


@pytest.mark.parametrize("forge", FORGERIES.values(), ids=FORGERIES.keys())
def test_a_context_cannot_be_built_or_copied_without_the_issuer_token(forge) -> None:
    with pytest.raises(PermissionError, match="issued by"):
        forge(build())


def test_the_token_is_not_a_field_and_not_stored() -> None:
    ctx = build()
    assert "issuer" not in {f.name for f in dataclasses.fields(ctx)}
    assert not hasattr(ctx, "issuer")


def test_re_calling_init_changes_nothing() -> None:
    ctx = build()
    before = (ctx.user_id, ctx.role, ctx.connections)
    other = uuid.uuid4()
    for token in (None, object(), _ISSUER):  # even with the real token: already initialised
        with pytest.raises(PermissionError):
            ctx.__init__(other, "admin", issuer=token)  # type: ignore[misc]  # pyright: ignore
        assert (ctx.user_id, ctx.role, ctx.connections) == before
    with pytest.raises(PermissionError):  # an invalid call is refused for the token first
        ctx.__init__("not-a-uuid", "root")  # type: ignore[misc]  # pyright: ignore
    assert (ctx.user_id, ctx.role, ctx.connections) == before


def test_a_context_cannot_be_pickled_or_copied_by_value() -> None:
    ctx = build()
    with pytest.raises(TypeError, match="pickled"):
        pickle.dumps(ctx)
    with pytest.raises(TypeError):
        copy.copy(ctx)
    with pytest.raises(TypeError):
        copy.deepcopy(ctx)
    with pytest.raises(TypeError, match="restored"):
        ctx.__setstate__({})  # pyright: ignore[reportAttributeAccessIssue]


class Sub(UserContext):
    pass


class LookAlike:
    def __init__(self) -> None:
        self.user_id = uuid.uuid4()
        self.role = "user"


def test_require_context_accepts_only_the_exact_class() -> None:
    ctx = build()
    assert require_context(ctx) is ctx
    subclass = Sub(uuid.uuid4(), "user", issuer=_ISSUER)
    for bad in (
        LookAlike(),
        SimpleNamespace(user_id=uuid.uuid4()),
        subclass,
        uuid.uuid4(),
        {},
        None,
    ):
        with pytest.raises(TypeError, match="UserContext is required"):
            require_context(bad)


@pytest.mark.parametrize(
    ("kwargs", "error"),
    [
        ({"user_id": str(USER), "role": "user"}, TypeError),
        ({"user_id": USER, "role": "root"}, ValueError),
        ({"user_id": USER, "role": "user", "connections": {"withings": CONNECTION}}, TypeError),
        ({"user_id": USER, "role": "user", "entitlements": frozenset()}, TypeError),
        (
            {
                "user_id": USER,
                "role": "user",
                "connections": frozenset({("withings", CONNECTION), ("withings", uuid.uuid4())}),
            },
            ValueError,
        ),
    ],
)
def test_invalid_contexts_are_refused(kwargs: dict[str, object], error: type[Exception]) -> None:
    with pytest.raises(error):
        UserContext(issuer=_ISSUER, **kwargs)  # pyright: ignore[reportArgumentType]


def test_pydantic_cannot_build_a_context_from_data() -> None:
    body = {"user_id": str(uuid.uuid4()), "role": "admin"}
    with pytest.raises(TypeError, match="use the auth dependency"):
        TypeAdapter(UserContext)
    with pytest.raises(TypeError, match="use the auth dependency"):
        TypeAdapter(UserContext).validate_python(body)  # pragma: no cover - never reached

    with pytest.raises(TypeError, match="use the auth dependency"):

        class Model(BaseModel):
            ctx: UserContext

    with pytest.raises(TypeError, match="use the auth dependency"):
        TypeAdapter(list[UserContext])


def test_a_route_cannot_take_a_context_from_the_request() -> None:
    app = FastAPI()
    with pytest.raises(TypeError, match="use the auth dependency"):

        @app.post("/x")
        async def bare(ctx: UserContext) -> dict[str, str]:  # pragma: no cover
            return {"user": str(ctx.user_id)}

    with pytest.raises(TypeError, match="use the auth dependency"):

        @app.post("/y")
        async def body(ctx: Annotated[UserContext, Body()]) -> None:  # pragma: no cover
            ...

    with pytest.raises(TypeError, match="use the auth dependency"):

        @app.get("/z")
        async def query(ctx: Annotated[UserContext, Query()]) -> None:  # pragma: no cover
            ...

    assert not [r for r in app.routes if getattr(r, "path", "") in ("/x", "/y", "/z")]


async def test_a_dependency_is_the_way_to_get_a_context() -> None:
    ctx = build()
    app = FastAPI()

    def current() -> UserContext:
        return ctx  # stands in for the auth dependency (#10)

    @app.get("/me")
    async def me(who: Annotated[UserContext, Depends(current)]) -> dict[str, str]:
        return {"user": str(who.user_id)}

    # The body is ignored: the context comes from the dependency only.
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/me", params={"who": str(uuid.uuid4())})
    assert response.json() == {"user": str(USER)}
