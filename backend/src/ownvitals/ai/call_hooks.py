"""What the owner's model evaluation adds to one model call (ADR 0014 section 9; #527). Pure.

``call_model(..., hooks=CallHooks(...))`` is the only difference between the evaluation's call and a
user's: the ``ai_audit`` row's kind is the evaluation's, ``allowed`` is asked immediately before the
reservation and before every later attempt (the evaluation puts "every account of this database is
marked synthetic" there; ``False`` means no request is sent and nothing stays reserved), and
``observe`` sees the bounded and the reported input tokens of each answer (the evaluation's largest
actual-to-bounded ratio). Production passes no hooks.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from ownvitals.ai.audit import AuditKind
from ownvitals.ai.responses import Reply


@dataclass(frozen=True, slots=True)
class CallHooks:
    audit_kind: AuditKind
    allowed: Callable[[], Awaitable[bool]]
    observe: Callable[[int, Reply], None]
