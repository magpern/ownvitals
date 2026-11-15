"""The WebAuthn relying-party id of ``ownvitals.config`` (ADR 0011 section 1): the check of
``webauthn_rp_id`` and the id in use. Pure: plain values in, a value or a ``ValueError`` out.

An address is never a relying-party id. Besides what ``ipaddress`` parses, a browser's URL parser
(WHATWG "ends in a number") reads a host whose last label is a decimal or ``0x`` number as IPv4:
``0x7f.0x1`` is ``127.0.0.1``. Such a host counts as an address here too. ``localhost`` (and its
subdomains) and the other loopback names are for local development only and are refused in
``prod``, whatever their case and with or without trailing dots (``LOCALHOST``, ``localhost.``).
"""

import re
from urllib.parse import urlsplit

from ownvitals.config_net import _is_ip

_HEX_NUMBER = re.compile(r"0[xX][0-9a-fA-F]*")


def ends_in_a_number(host: str) -> bool:
    """WHATWG URL "ends in a number": the last label (a trailing dot aside) is a number."""
    labels = host.split(".")
    if labels[-1] == "" and len(labels) > 1:
        labels.pop()
    last = labels[-1]
    return bool(last) and (
        (last.isascii() and last.isdigit()) or _HEX_NUMBER.fullmatch(last) is not None
    )


def _is_address(host: str) -> bool:
    return _is_ip(host) or ends_in_a_number(host)


# Names that resolve to the loopback interface besides ``localhost`` and ``*.localhost`` (RFC 6761
# section 6.3): the usual ``/etc/hosts`` aliases.
_LOOPBACK_NAMES = frozenset(
    {
        "localhost.localdomain",
        "ip6-localhost",
        "ip6-loopback",
        "localhost6",
        "localhost6.localdomain6",
    }
)


def _is_localhost(rp_id: str) -> bool:
    """A loopback name, compared case-blind and with trailing dots stripped, as a resolver does."""
    name = rp_id.lower().rstrip(".")
    return name == "localhost" or name.endswith(".localhost") or name in _LOOPBACK_NAMES


def check_rp_id(public_base_url: str, rp_id: str | None, environment: str = "dev") -> None:
    """Refuse a set ``webauthn_rp_id`` that is not the public host or a parent domain of it, and
    in ``prod`` a ``localhost`` id, set or taken from the public host."""
    if environment == "prod":
        in_use = rp_id_in_use(public_base_url, rp_id)
        if in_use is not None and _is_localhost(in_use):
            raise ValueError("the relying-party id must not be localhost when environment is prod")
    host = urlsplit(public_base_url).hostname or ""
    if rp_id is None:
        return
    if _is_address(host) or rp_id != rp_id.strip().lower() or not rp_id or _is_address(rp_id):
        raise ValueError("webauthn_rp_id must be a lower-case domain name")
    if not (host == rp_id or host.endswith("." + rp_id)) or (
        "." not in rp_id and rp_id != "localhost"
    ):
        raise ValueError(
            "webauthn_rp_id must be the host of public_base_url or a parent domain of it"
        )


def rp_id_in_use(public_base_url: str, rp_id: str | None) -> str | None:
    """The set id, else the public host; ``None`` when that host is an address (passkeys off)."""
    if rp_id is not None:
        return rp_id
    host = urlsplit(public_base_url).hostname or ""
    return None if not host or _is_address(host) else host
