"""The client address and its rate-limit bucket (ADR 0003 sections 7 and 8).

Trusted proxies are SWAG and Cloudflare, not Caddy (ADR 0003 section 8): the client address is
the peer, unless the peer is in ``trusted_proxies`` (default loopback only). Behind a trusted
proxy it is

- the ``CF-Connecting-IP`` header, only when the peer is also in ``cf_connecting_ip_proxies``
  (SWAG, which sets or overwrites it only for connections from Cloudflare's ranges) and the
  header is exactly one IP address; otherwise
- the right-most ``X-Forwarded-For`` entry that is not itself a trusted proxy.

Anything malformed falls back to the peer, so a client cannot choose its bucket.
"""

import ipaddress
from collections.abc import Sequence

type Address = ipaddress.IPv4Address | ipaddress.IPv6Address
type Network = ipaddress.IPv4Network | ipaddress.IPv6Network


def parse_address(value: str | None) -> Address | None:
    """Parse ``value``; an IPv4-mapped IPv6 address (``::ffff:a.b.c.d``) becomes the IPv4 one."""
    if not value:
        return None
    try:
        address = ipaddress.ip_address(value.strip().partition("%")[0])  # drop an IPv6 scope id
    except ValueError:
        return None
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return address.ipv4_mapped
    return address


def _trusted(address: Address, proxies: Sequence[Network]) -> bool:
    return any(address.version == net.version and address in net for net in proxies)


def client_ip(
    peer: str | None,
    forwarded_for: Sequence[str],
    trusted_proxies: Sequence[Network],
    *,
    cf_connecting_ip: Sequence[str] = (),
    cf_proxies: Sequence[Network] = (),
) -> str:
    """The client's address, as text (``"unknown"`` if there is none).

    It is the peer, unless the peer is a trusted proxy (see the module doc). ``cf_connecting_ip``
    is every ``CF-Connecting-IP`` header of the request: it counts only from a peer in both
    ``trusted_proxies`` and ``cf_proxies``, and only as a single header holding one address that
    is not itself a trusted proxy. An ``X-Forwarded-For`` entry that is not an IP address ends
    the walk and the peer is used.
    """
    peer_address = parse_address(peer)
    if peer_address is None:
        return "unknown"
    if not _trusted(peer_address, trusted_proxies):
        return str(peer_address)
    if _trusted(peer_address, cf_proxies) and len(cf_connecting_ip) == 1:
        relayed = parse_address(cf_connecting_ip[0])
        if relayed is not None and not _trusted(relayed, trusted_proxies):
            return str(relayed)
    entries = [item.strip() for header in forwarded_for for item in header.split(",")]
    for entry in reversed([e for e in entries if e]):
        address = parse_address(entry)
        if address is None:
            return str(peer_address)
        if not _trusted(address, trusted_proxies):
            return str(address)
    return str(peer_address)


def from_cloudflare_proxy(
    peer: str | None, trusted_proxies: Sequence[Network], cf_proxies: Sequence[Network]
) -> bool:
    """Whether the request's peer is a proxy whose Cloudflare headers count: in both
    ``trusted_proxies`` and ``cf_proxies``, the rule of ``CF-Connecting-IP`` above. The visitor
    location headers of ADR 0011 section 6 follow it too."""
    address = parse_address(peer)
    return (
        address is not None and _trusted(address, trusted_proxies) and _trusted(address, cf_proxies)
    )


def same_network(first: str | None, second: str | None) -> bool:
    """Two addresses on one network for the QR approval screen (ADR 0011 section 7): equal
    IPv4 addresses (one NAT), or IPv6 addresses in the same /64. Unknown is never the same."""
    a, b = parse_address(first), parse_address(second)
    if a is None or b is None or a.version != b.version:
        return False
    return bucket_ip(str(a)) == bucket_ip(str(b))


def bucket_ip(ip: str) -> str:
    """The rate-limit key of an address: IPv6 by its /64 prefix, IPv4 as it is."""
    address = parse_address(ip)
    if address is None:
        return ip
    if isinstance(address, ipaddress.IPv6Address):
        return str(ipaddress.ip_network(f"{address}/64", strict=False))
    return str(address)
