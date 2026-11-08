"""The pure rules of the activity list (``auth/activity_model.py``) and the event origin
(``auth/event_origin.py``), without a database (#251). Synthetic values only."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
from starlette.requests import Request

from ownvitals.auth import activity_model as model
from ownvitals.auth import event_origin, geo
from ownvitals.auth.useragent import BROWSERS, SYSTEMS

AT = datetime(2026, 11, 8, 12, 0, 0, 123456, tzinfo=UTC)


def _row(kind: str, detail: Any = None, **origin: Any) -> model.StoredEvent:
    values = {"browser": None, "os": None, "place": None, **origin}
    return model.StoredEvent(uuid4(), kind, AT, detail, **values)


@pytest.mark.parametrize(
    ("kind", "detail", "shown", "method"),
    [
        ("sign_in", None, "sign_in", "password"),
        ("sign_in", {"provider": "google"}, "sign_in", "google"),
        ("sign_in", {"provider": "myspace"}, "sign_in", "password"),
        ("passkey_signin", {"passkey": "id"}, "sign_in", "passkey"),
        ("qr_login_completed", {"qr": "id", "session": "id"}, "sign_in", "qr"),
        ("sign_in_failed", None, "sign_in_failed", None),
        ("passkey_signin_failed", {"reason": "unknown"}, "sign_in_failed", "passkey"),
        ("passkey_signin_failed", {"purpose": "reauth"}, "reauth_failed", "passkey"),
        ("reauth_failed", None, "reauth_failed", "password"),
        ("reauth_failed", {"provider": "google", "why": "x"}, "reauth_failed", "google"),
        ("sign_out", None, "sign_out", None),
        ("session_signed_out", None, "session_signed_out", None),
        ("logout_everywhere", {"sessions": 3}, "logout_everywhere", None),
        ("passkey_registered", {"passkey": "id"}, "passkey_added", None),
        ("identity_linked", {"provider": "google"}, "google_linked", "google"),
        ("identity_unlinked", None, "google_unlinked", "google"),
        ("mfa_failed", {"purpose": "reauth"}, "mfa_failed", None),
    ],
)
def test_kind_and_method(kind: str, detail: Any, shown: str, method: str | None) -> None:
    item = model.entry(_row(kind, detail))
    assert item is not None
    assert (item.kind, item.method) == (shown, method)


def test_every_shown_code_is_listed_and_every_method_is_known() -> None:
    assert "reauth_failed" in model.SHOWN_KINDS  # also the passkey re-auth failure
    for kind in model.STORED_KINDS:
        for detail in (None, {"provider": "google"}, {"purpose": "reauth"}, "not-a-dict", []):
            item = model.entry(_row(kind, detail))
            assert item is not None
            assert item.kind in model.SHOWN_KINDS
            assert item.method is None or item.method in model.METHODS


@pytest.mark.parametrize(
    "kind",
    ["connections_wiped", "qr_login_started", "step_up", "admin_granted", "account_deleted", ""],
)
def test_other_kinds_are_not_shown(kind: str) -> None:
    assert model.entry(_row(kind)) is None


def test_the_origin_is_kept_only_as_allowlisted_names() -> None:
    item = model.entry(_row("sign_out", browser="Edge", os="macOS", place="Malmö, SE"))
    assert item is not None
    assert (item.browser, item.os, item.place) == ("Edge", "macOS", "Malmö, SE")
    odd = model.entry(_row("sign_out", browser="Netscape 4", os="BeOS", place="‮X, SE"))
    assert odd is not None
    assert (odd.browser, odd.os, odd.place) == ("other", "other", "X, SE")
    missing = model.entry(_row("sign_out"))
    assert missing is not None
    assert (missing.browser, missing.os, missing.place) == (None, None, geo.UNKNOWN)


def test_the_operators_reset_shows_no_origin() -> None:
    item = model.entry(_row("mfa_reset", browser="Firefox", os="Linux", place="Oslo, NO"))
    assert item is not None
    assert (item.browser, item.os, item.place) == (None, None, geo.UNKNOWN)


def test_nothing_else_of_the_detail_reaches_the_entry() -> None:
    detail = {"provider": "google", "session": "s-1", "email": "a@example.com", "token": "t"}
    item = model.entry(_row("sign_in", detail))
    assert item is not None
    assert set(item.__slots__) == {"kind", "at", "method", "browser", "os", "place"}  # pyright: ignore[reportAttributeAccessIssue]
    assert "s-1" not in repr(item)
    assert "a@example.com" not in repr(item)


def test_the_cursor_round_trips_and_refuses_anything_else() -> None:
    event_id = UUID("0123456789abcdef0123456789abcdef")
    for at in (AT, AT + timedelta(microseconds=1), datetime(1970, 1, 1, tzinfo=UTC)):
        assert model.decode_cursor(model.encode_cursor(at, event_id)) == (at, event_id)
    for bad in (
        "",
        "1",
        "1_",
        "_" + "0" * 32,
        "-1_" + "0" * 32,
        "1_" + "0" * 31,
        "1_" + "G" * 32,
        "1_" + "0" * 32 + "\n",
        "9" * 19 + "_" + "0" * 32,  # past datetime.max
        "1_" + "0" * 32 + "0" * 40,
    ):
        assert model.decode_cursor(bad) is None


# --- event_origin --------------------------------------------------------------------------------


def _request(headers: list[tuple[bytes, bytes]], *, geo_source: str = "none") -> Request:
    settings = SimpleNamespace(
        geo_source=geo_source,
        trusted_proxy_networks=(),
        cf_connecting_ip_networks=(),
    )
    app = SimpleNamespace(state=SimpleNamespace(settings=settings))
    scope = {"type": "http", "headers": headers, "client": ("127.0.0.1", 1), "app": app}
    return Request(scope)


def test_the_origin_holds_only_the_families_and_the_place() -> None:
    agent = b"Mozilla/5.0 (Macintosh; Intel Mac OS X 15_0) Version/18.0 Safari/605.1.15"
    origin = event_origin.origin_of(_request([(b"user-agent", agent)]))
    assert origin == event_origin.EventOrigin("Safari", "macOS", None)
    assert origin.browser in BROWSERS
    assert origin.os in SYSTEMS


def test_no_or_a_repeated_agent_header_is_other() -> None:
    assert event_origin.origin_of(_request([])) == event_origin.EventOrigin("other", "other", None)
    twice = [(b"user-agent", b"Firefox/140.0"), (b"user-agent", b"Chrome/140.0")]
    assert event_origin.origin_of(_request(twice)).browser == "other"


def test_the_place_comes_only_from_a_trusted_proxy() -> None:
    headers = [(b"cf-ipcity", b"Lund"), (b"cf-ipcountry", b"SE")]
    untrusted = event_origin.origin_of(_request(headers, geo_source="cloudflare"))
    assert untrusted.place is None  # the peer is no trusted Cloudflare proxy


def test_outside_a_request_there_is_no_origin() -> None:
    assert event_origin.current_origin() is None


async def test_the_middleware_sets_the_request_only_while_it_runs() -> None:
    seen: list[event_origin.EventOrigin | None] = []

    async def app(scope: Any, receive: Any, send: Any) -> None:
        seen.append(event_origin.current_origin())

    middleware = event_origin.EventOriginMiddleware(app)
    req = _request([(b"user-agent", b"Mozilla/5.0 (X11; Linux x86_64) Firefox/140.0")])
    await middleware(req.scope, None, None)  # pyright: ignore[reportArgumentType]
    await middleware({"type": "lifespan"}, None, None)  # pyright: ignore[reportArgumentType]
    assert seen == [event_origin.EventOrigin("Firefox", "Linux", None), None]
    assert event_origin.current_origin() is None
