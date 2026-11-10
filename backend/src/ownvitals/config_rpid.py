"""The WebAuthn relying-party id of ``ownvitals.config`` (ADR 0011 section 1): the check of
``webauthn_rp_id`` and the id in use. Pure: plain values in, a value or a ``ValueError`` out."""

from urllib.parse import urlsplit

from ownvitals.config_net import _is_ip


def check_rp_id(public_base_url: str, rp_id: str | None) -> None:
    """Refuse a set ``webauthn_rp_id`` that is not the public host or a parent domain of it."""
    host = urlsplit(public_base_url).hostname or ""
    if rp_id is None:
        return
    if _is_ip(host) or rp_id != rp_id.strip().lower() or not rp_id or _is_ip(rp_id):
        raise ValueError("webauthn_rp_id must be a lower-case domain name")
    if not (host == rp_id or host.endswith("." + rp_id)) or (
        "." not in rp_id and rp_id != "localhost"
    ):
        raise ValueError(
            "webauthn_rp_id must be the host of public_base_url or a parent domain of it"
        )


def rp_id_in_use(public_base_url: str, rp_id: str | None) -> str | None:
    """The set id, else the public host; ``None`` when that host is an IP address (passkeys off)."""
    if rp_id is not None:
        return rp_id
    host = urlsplit(public_base_url).hostname or ""
    return None if not host or _is_ip(host) else host
