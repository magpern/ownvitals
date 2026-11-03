"""Where the desktop's place comes from (ADR 0011 section 6). ``none`` (the default) is "Place
unknown"; ``cloudflare`` reads the visitor-location headers only from a listed peer, refuses a
duplicate, strips control and bidirectional characters and caps the length; ``geoip_file`` is
refused by the settings. No mode makes a call: the suite's network guard refuses any outbound
connection. Synthetic addresses (documentation ranges) only."""

import pytest
from pydantic import ValidationError
from starlette.requests import Request

from authtools import (  # noqa: F401 - fixtures
    Env,
    auth_schema_fixture,
    clean_auth_fixture,
    make_settings,
    running_env,
)
from ownvitals.auth import geo
from qrtools import QrBrowser, phone, scan, start

URL = "postgresql+psycopg://ownvitals_app@127.0.0.1:5432/ownvitals"
CF = {"geo_source": "cloudflare", "cf_connecting_ip_proxies": "127.0.0.1/32"}


def request(peer: str, headers: list[tuple[str, str]]) -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/auth/qr/start",
            "headers": [(k.lower().encode(), v.encode()) for k, v in headers],
            "client": (peer, 40000),
        }
    )


STOCKHOLM = [("CF-IPCity", "Stockholm"), ("CF-IPCountry", "SE")]


def test_none_is_place_unknown_whatever_the_headers() -> None:
    settings = make_settings(URL, cf_connecting_ip_proxies="127.0.0.1/32")
    assert settings.geo_source == "none"
    assert geo.place(request("127.0.0.1", STOCKHOLM), settings) is None
    assert geo.shown(None) == "Place unknown"


def test_cloudflare_from_a_listed_peer() -> None:
    settings = make_settings(URL, **CF)
    assert geo.place(request("127.0.0.1", STOCKHOLM), settings) == "Stockholm, SE"
    assert geo.place(request("127.0.0.1", [("CF-IPCountry", "SE")]), settings) == "SE"
    assert geo.place(request("127.0.0.1", [("CF-IPCity", "Stockholm")]), settings) is None


@pytest.mark.parametrize(
    ("peer", "overrides"),
    [
        ("192.0.2.10", {}),  # not a proxy at all
        ("127.0.0.1", {"cf_connecting_ip_proxies": ""}),  # trusted, but not a Cloudflare proxy
        ("192.0.2.10", {"trusted_proxies": "", "cf_connecting_ip_proxies": "192.0.2.10/32"}),
    ],
)
def test_cloudflare_headers_from_any_other_peer_are_ignored(
    peer: str, overrides: dict[str, str]
) -> None:
    settings = make_settings(URL, **{**CF, **overrides})
    assert geo.place(request(peer, STOCKHOLM), settings) is None


def test_a_duplicate_header_is_refused() -> None:
    settings = make_settings(URL, **CF)
    twice_city = [*STOCKHOLM, ("CF-IPCity", "Malmo")]
    twice_country = [*STOCKHOLM, ("CF-IPCountry", "NO")]
    assert geo.place(request("127.0.0.1", twice_city), settings) is None
    assert geo.place(request("127.0.0.1", twice_country), settings) is None


def test_control_and_bidi_characters_are_stripped_and_the_length_capped() -> None:
    settings = make_settings(URL, **CF)
    city = "Sto\u202eckh\x00olm\u200b \t "
    assert geo.place(
        request("127.0.0.1", [("CF-IPCity", city), ("CF-IPCountry", "se")]), settings
    ) == ("Stockholm, SE")
    long = geo.place(
        request("127.0.0.1", [("CF-IPCity", "x" * 500), ("CF-IPCountry", "SE")]), settings
    )
    assert long is not None
    assert len(long) <= 64
    assert long.endswith(", SE")
    malmo = [("CF-IPCity", "Malm\u00f6"), ("CF-IPCountry", "SE")]  # sent as UTF-8 bytes
    assert geo.place(request("127.0.0.1", malmo), settings) == "Malm\u00f6, SE"
    for bad in ("Sweden", "S", "", "S-E"):
        assert geo.place(request("127.0.0.1", [("CF-IPCountry", bad)]), settings) is None, bad


def test_geoip_file_is_refused_until_the_owner_decides() -> None:
    with pytest.raises(ValidationError, match="geoip_file"):
        make_settings(URL, geo_source="geoip_file")


def test_the_qr_stream_limits_can_only_be_tightened() -> None:
    for name, value in (("qr_streams_per_ip", 6), ("qr_streams_per_process", 101)):
        with pytest.raises(ValidationError):
            make_settings(URL, **{name: value})


async def test_the_place_reaches_the_approval_screen(
    clean_auth: None, app_database_url: str
) -> None:
    async with running_env(app_database_url, **CF) as env:
        mobile = await phone(env)
        desktop = QrBrowser(env)
        desktop.extra = dict(STOCKHOLM)  # SWAG forwards Cloudflare's headers; the peer is listed
        shown = await scan(mobile, await start(desktop))
        assert shown.json()["desktop"]["place"] == "Stockholm, SE"
        plain = QrBrowser(env, "198.51.100.33")
        shown = await scan(mobile, await start(plain))
        assert shown.json()["desktop"]["place"] == "Place unknown"
        assert (
            await env.scalar("SELECT count(*) FROM qr_logins WHERE desktop_place = 'Stockholm, SE'")
            == 1
        )
