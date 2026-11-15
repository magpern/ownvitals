"""What counts as a passed Turnstile check (ADR 0018 section 3). Pure: no I/O.

Turnstile's ``siteverify`` answers JSON with ``success``, ``error-codes``, ``hostname`` and
``action`` (Cloudflare's server-side validation documentation). A check passes only when all of
these hold, and fails closed otherwise:

- the HTTP status was 200 and the body is a JSON object;
- ``success`` is exactly ``true`` (not a truthy string or number);
- ``hostname`` is the host of ``public_base_url`` (a token solved on another site is refused);
- ``action`` is the form's own (``signup``, ``signin`` or ``reset``), so a token solved on one
  form does not pass another.

An expired or reused token comes back as ``success: false`` (``timeout-or-duplicate``), so it
fails like any other.
"""

import json
from typing import Final, Literal

Action = Literal["signup", "signin", "reset"]
ACTIONS: Final = frozenset({"signup", "signin", "reset"})
_MAX_ANSWER_BYTES: Final = 16 * 1024


def passed(status: int, body: bytes, *, hostname: str, action: Action) -> bool:
    """Whether a siteverify answer (``status``, raw ``body``) passes for ``hostname`` and
    ``action``. Anything unexpected is a failure."""
    if status != 200 or len(body) > _MAX_ANSWER_BYTES or action not in ACTIONS:
        return False
    try:
        answer = json.loads(body)
    except ValueError:  # not JSON, or not UTF-8
        return False
    if not isinstance(answer, dict):
        return False
    return (
        answer.get("success") is True
        and isinstance(answer.get("hostname"), str)
        and answer["hostname"].lower() == hostname.lower()
        and answer.get("action") == action
    )
