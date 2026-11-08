"""Host, origin and network helpers of ``ownvitals.config``'s validators and properties."""

import ipaddress
from urllib.parse import urlsplit

# Hosts where unencrypted SMTP (``smtp_tls = none``) is allowed: loopback and the compose service.
PLAIN_SMTP_HOSTS = frozenset({"localhost", "mailpit"})


def _is_local_host(host: str) -> bool:
    if host.lower() in PLAIN_SMTP_HOSTS:
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return False
    return True


def public_origin(url: str) -> str:
    """``scheme://host[:port]`` of ``url``, lower case, as a browser sends it in ``Origin``."""
    parts = urlsplit(url)
    host = parts.hostname or ""
    if ":" in host:
        host = f"[{host}]"
    default = {"http": 80, "https": 443}.get(parts.scheme.lower())
    port = f":{parts.port}" if parts.port is not None and parts.port != default else ""
    return f"{parts.scheme.lower()}://{host.lower()}{port}"


def parse_networks(
    value: str, name: str = "trusted_proxies"
) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    """Parse a comma-separated list of addresses and CIDR ranges (``name`` is the setting)."""
    networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for item in value.split(","):
        if item.strip():
            try:
                networks.append(ipaddress.ip_network(item.strip(), strict=False))
            except ValueError:
                raise ValueError(f"{name} must list IP addresses or CIDR ranges") from None
    return tuple(networks)
