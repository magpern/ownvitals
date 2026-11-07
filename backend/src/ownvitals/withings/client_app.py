"""What the Withings API client (``client.py``) calls with: the app (``WithingsApp``, PLAN §4.1),
the form fields of a call and the signature of a signed call. Pure: no I/O."""

from __future__ import annotations

import hashlib
import hmac
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date
from typing import Final
from urllib.parse import urlsplit

from ownvitals import egress

DEFAULT_BASE_URL: Final = f"https://{egress.WITHINGS_API_HOST}"


_FIELD = re.compile(r"[a-z0-9_]+")


@dataclass(frozen=True)
class WithingsApp:
    """A Withings developer app (PLAN §4.1). ``client_secret`` never shows in a ``repr``."""

    client_id: str
    client_secret: str = field(repr=False)
    base_url: str = DEFAULT_BASE_URL

    def __post_init__(self) -> None:
        parts = urlsplit(self.base_url)
        if (
            parts.scheme != "https"
            or parts.hostname not in egress.WITHINGS_HOSTS
            or parts.port not in (None, egress.HTTPS_PORT)
            or parts.username is not None
            or parts.path not in ("", "/")
            or parts.query
            or parts.fragment
        ):
            raise ValueError("base_url must be https://<a Withings API host> with no path")
        if not self.client_id or not self.client_secret:
            raise ValueError("client_id and client_secret are required")


def _int(value: int) -> str:
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError("an integer parameter is not an integer")
    return str(value)


def _params(**values: object) -> dict[str, str]:
    """The form fields that are set: integers, ``date`` as YYYY-MM-DD, field and type lists
    comma-joined, strings as they are."""
    form: dict[str, str] = {}
    for name, value in values.items():
        if value is None:
            continue
        if isinstance(value, str):
            form[name] = value
        elif isinstance(value, date):
            form[name] = value.isoformat()
        elif isinstance(value, int):
            form[name] = _int(value)
        elif isinstance(value, Iterable):
            items = [str(v) if isinstance(v, str) else _int(v) for v in value]  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType]
            if name == "data_fields" and not all(_FIELD.fullmatch(i) for i in items):
                raise ValueError("a data field name is malformed")
            form[name] = ",".join(items)
        else:
            raise TypeError(f"unsupported value for {name}")
    return form


def signature(secret: str, *values: str) -> str:
    """Withings' signature of a signed call: HMAC-SHA256 with the client secret over the values
    of ``action``, ``client_id`` and ``nonce`` (or ``timestamp``) in key order, comma-joined."""
    return hmac.new(secret.encode(), ",".join(values).encode(), hashlib.sha256).hexdigest()
