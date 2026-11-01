"""The client address and its rate-limit bucket (ADR 0003 section 9)."""

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
    peer: str | None, forwarded_for: Sequence[str], trusted_proxies: Sequence[Network]
) -> str:
    """The client's address, as text (``"unknown"`` if there is none).

    It is the peer, unless the peer is a trusted proxy: then it is the right-most
    ``X-Forwarded-For`` entry that is not itself a trusted proxy. An entry that is not an IP
    address ends the walk and the peer is used, so a client cannot choose its bucket.
    """
    peer_address = parse_address(peer)
    if peer_address is None:
        return "unknown"
    if not _trusted(peer_address, trusted_proxies):
        return str(peer_address)
    entries = [item.strip() for header in forwarded_for for item in header.split(",")]
    for entry in reversed([e for e in entries if e]):
        address = parse_address(entry)
        if address is None:
            return str(peer_address)
        if not _trusted(address, trusted_proxies):
            return str(address)
    return str(peer_address)


def bucket_ip(ip: str) -> str:
    """The rate-limit key of an address: IPv6 by its /64 prefix, IPv4 as it is."""
    address = parse_address(ip)
    if address is None:
        return ip
    if isinstance(address, ipaddress.IPv6Address):
        return str(ipaddress.ip_network(f"{address}/64", strict=False))
    return str(address)
