"""``CF-Connecting-IP`` behind SWAG (ownvitals ADR 0003 section 8, #10): the header counts only on
a request whose peer is a trusted proxy that is also listed in ``cf_connecting_ip_proxies``, only
as one header holding one address, and never when that address is itself a trusted proxy.
Otherwise ``X-Forwarded-For`` and the peer decide, as in classbeat (``test_auth_ratelimit.py``)."""

import ipaddress
from types import SimpleNamespace
from typing import Any

import pytest

from authtools import make_settings
from ownvitals.auth.clientip import client_ip
from ownvitals.auth.deps import request_ip

SWAG = [ipaddress.ip_network("192.168.1.151/32")]
OTHER_PROXY = ipaddress.ip_network("10.0.0.0/8")
TRUSTED = [*SWAG, OTHER_PROXY]
XFF = ["198.51.100.4"]


def test_swag_relays_the_cloudflare_client_address() -> None:
    ip = client_ip("192.168.1.151", XFF, TRUSTED, cf_connecting_ip=["203.0.113.7"], cf_proxies=SWAG)
    assert ip == "203.0.113.7"
    mapped = client_ip(
        "192.168.1.151", [], TRUSTED, cf_connecting_ip=["::ffff:203.0.113.7"], cf_proxies=SWAG
    )
    assert mapped == "203.0.113.7"


@pytest.mark.parametrize(
    ("peer", "header", "cf_proxies", "expected"),
    [
        # a peer that is not SWAG: the header is the client's own choice
        ("203.0.113.50", ["6.6.6.6"], SWAG, "203.0.113.50"),
        # a trusted proxy that is not listed for the header: X-Forwarded-For decides
        ("10.0.0.5", ["6.6.6.6"], SWAG, "198.51.100.4"),
        # the header counts nowhere until cf_connecting_ip_proxies is set (the default)
        ("192.168.1.151", ["6.6.6.6"], [], "198.51.100.4"),
        # two headers, a list, garbage or empty: X-Forwarded-For decides
        ("192.168.1.151", ["6.6.6.6", "7.7.7.7"], SWAG, "198.51.100.4"),
        ("192.168.1.151", ["6.6.6.6, 7.7.7.7"], SWAG, "198.51.100.4"),
        ("192.168.1.151", ["not-an-ip"], SWAG, "198.51.100.4"),
        ("192.168.1.151", [""], SWAG, "198.51.100.4"),
        # an address that is itself a trusted proxy is not a client
        ("192.168.1.151", ["10.9.9.9"], SWAG, "198.51.100.4"),
        ("192.168.1.151", ["192.168.1.151"], SWAG, "198.51.100.4"),
    ],
)
def test_the_header_is_ignored_unless_it_comes_from_swag_and_is_one_address(
    peer: str, header: list[str], cf_proxies: list[Any], expected: str
) -> None:
    assert client_ip(peer, XFF, TRUSTED, cf_connecting_ip=header, cf_proxies=cf_proxies) == expected


def test_a_cf_proxy_that_is_not_a_trusted_proxy_does_not_count() -> None:
    """Listing SWAG for the header without trusting it changes nothing: the peer is the client."""
    ip = client_ip(
        "192.168.1.151", XFF, [OTHER_PROXY], cf_connecting_ip=["6.6.6.6"], cf_proxies=SWAG
    )
    assert ip == "192.168.1.151"


def _request(settings: object, peer: str, headers: dict[str, list[str]]) -> Any:
    return SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(settings=settings)),
        client=SimpleNamespace(host=peer),
        headers=SimpleNamespace(getlist=lambda name: headers.get(name.lower(), [])),
    )


async def test_both_lists_are_read_from_the_settings(app_database_url: str) -> None:
    settings = make_settings(
        app_database_url,
        trusted_proxies="192.168.1.151",
        cf_connecting_ip_proxies="192.168.1.151",
    )
    headers = {"x-forwarded-for": ["198.51.100.4"], "cf-connecting-ip": ["203.0.113.7"]}
    assert request_ip(_request(settings, "192.168.1.151", headers)) == "203.0.113.7"
    assert request_ip(_request(settings, "203.0.113.50", headers)) == "203.0.113.50"
    loopback = make_settings(app_database_url)  # the defaults: loopback trusted, no CF proxy
    assert request_ip(_request(loopback, "127.0.0.1", headers)) == "198.51.100.4"
